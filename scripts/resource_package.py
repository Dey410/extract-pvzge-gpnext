#!/usr/bin/env python3
"""Assemble one ASTC runtime payload and reopen its ZIP for complete validation."""
import argparse
import csv
import hashlib
import json
import re
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath

from scripts.transcode_textures_astc import AstcError, TextureJob, _validate_astc_file, plan_metadata

HOST_ABI = 1
MANIFEST_PATH = '__gardendless/package/resource-manifest.json'
INDEX_PATH = '__gardendless/package/file-index.json'
METADATA = {MANIFEST_PATH, INDEX_PATH}
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024 * 1024
MAX_FILES = 200_000
MANIFEST_FIELDS = {'schemaVersion', 'gameVersion', 'cocosVersion', 'hasGpNext', 'gpNextVersion', 'producerCommit', 'minHostAbi', 'fileIndexSha256'}
VERSION = re.compile(r'^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$')
DIGEST = re.compile(r'^[a-f0-9]{64}$')
COMMIT = re.compile(r'^[a-f0-9]{40}$')
MODULE = re.compile(r'<script\b(?=[^>]*\btype\s*=\s*["\']module["\'])[^>]*\bsrc\s*=\s*["\']([^"\']+)["\'][^>]*>\s*</script>', re.I)


class PackageError(RuntimeError):
    pass


def safe_path(value: str) -> str:
    if not isinstance(value, str) or not value or '\\' in value or any(ord(c) < 32 for c in value) or value.startswith('/') or re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', value) or PurePosixPath(value).as_posix() != value or any(c in ('', '.', '..') for c in value.split('/')):
        raise PackageError(f'unsafe package path: {value!r}')
    return value


def sort_key(value: str) -> bytes:
    # Dart's String.compareTo uses UTF-16 code units.
    return value.encode('utf-16-be')


def json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')


def read_json(data: bytes):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise PackageError(f'duplicate JSON key: {key}')
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=unique)


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def guard_entry(html: str) -> tuple[str, str]:
    visible = re.sub(r'<!--.*?-->', lambda match: ' ' * len(match.group(0)), html, flags=re.S)
    entries = list(MODULE.finditer(visible))
    if len(entries) != 1 or re.search(r'<head\b[^>]*>', html, re.I) is None:
        raise PackageError('expected one known module entry and a head element')
    entry = entries[0]
    path = safe_path(entry.group(1).removeprefix('/').removeprefix('./'))
    tag = re.sub(r'(?<![\w-])src\s*=', 'data-gardendless-src=', entry.group(0), count=1, flags=re.I)
    html = html[:entry.start()] + tag + html[entry.end():]
    guard = '''<script>(function(){function start(){var h=window.__gardendlessHost;
if(!h||!h.config||!Number.isInteger(h.config.hostAbi)||h.config.hostAbi<1){
document.body.textContent="此资源包需要 GardendlessLoader 0.9.0 或更高版本，请升级加载器。";return;}
var entry=document.querySelector("script[data-gardendless-src]");var script=document.createElement("script");
script.type="module";script.crossOrigin=entry.crossOrigin;script.src=entry.getAttribute("data-gardendless-src");entry.replaceWith(script);}
if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",start,{once:true});else start();})();</script>'''
    return re.sub(r'(<head\b[^>]*>)', lambda m: m.group(1) + guard, html, count=1, flags=re.I), path


def texture_jobs(source: Path, texture_map: Path) -> list[TextureJob]:
    jobs = []
    with texture_map.open(newline='', encoding='utf-8') as mapping:
        for row in csv.DictReader(mapping):
            relative = safe_path(row['source_path'])
            file = source / relative
            parts = PurePosixPath(relative).parts
            if len(parts) < 5 or parts[0] != 'assets' or parts[2] != 'native' or file.suffix.lower() not in ('.png', '.avif', '.webp'):
                raise PackageError(f'unknown native texture mapping: {relative}')
            job = TextureJob(file, file, file.with_suffix('.astc'), source / parts[0] / parts[1],
                             int(row['width']), int(row['height']), 'source', file.stat().st_size, 0, row['astc_block'], 'verified')
            _validate_astc_file(job.astc_path, job)
            jobs.append(job)
    if not jobs or len({j.source_path for j in jobs}) != len(jobs):
        raise PackageError('texture map is empty or contains duplicate sources')
    for path, expected in plan_metadata(jobs).items():
        if read_json(path.read_bytes()) != read_json(expected):
            raise PackageError(f'ASTC metadata contains an alternate image format: {path}')
    return jobs


def assemble_package(source: Path, output: Path, texture_map: Path, producer_commit: str) -> Path:
    if output.exists() or not COMMIT.fullmatch(producer_commit):
        raise PackageError('use a fresh output directory and an actual 40-digit producer commit')
    settings = read_json((source / 'src' / 'settings.json').read_bytes())
    if settings.get('CocosEngine') != '3.8.4':
        raise PackageError('unknown Cocos engine; no formal resource package will be created')
    html, entry_path = guard_entry((source / 'index.html').read_text(encoding='utf-8'))
    for required in ['cocos-js/cc.js', 'src/import-map.json', entry_path]:
        if not (source / required).is_file():
            raise PackageError(f'missing runtime entry: {required}')
    entry = (source / entry_path).read_text(encoding='utf-8')
    fingerprints = entry
    for file in sorted((source / 'assets').glob('gp-next-*.js')):
        if file.name in entry:
            fingerprints += '\n' + file.read_text(encoding='utf-8')
    version = re.search(r'(?:Playing version|Game Version:)\s*v?(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)', fingerprints, re.I)
    if version is None:
        version = re.search(r'(?:^|[^0-9A-Za-z])v?(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)(?![0-9A-Za-z.-])', html)
    if version is None:
        raise PackageError('game version is unknown')
    has_gp = all(marker in fingerprints for marker in ('GP-Next loading', 'window.gpNext', 'loadAllPatches'))
    title = re.search(r'<title[^>]*>(.*?)</title>', html, re.I | re.S)
    if title is None or 'PvZ2 Gardendless' not in title.group(1).replace('_', ' ') or (not has_gp and 'pvzge' not in html.lower()):
        raise PackageError('game title or entry fingerprint is invalid')
    jobs = texture_jobs(source, texture_map)
    originals = {j.source_path for j in jobs}
    selected_astc = {j.astc_path for j in jobs}
    legacy_gpu_alternatives = {j.source_path.with_suffix(ext) for j in jobs for ext in ('.pvr', '.pkm', '.ktx')}
    files = []
    for path in source.rglob('*'):
        if path.is_symlink():
            raise PackageError(f'linked payload is forbidden: {path}')
        if path.is_dir():
            continue
        relative = safe_path(path.relative_to(source).as_posix())
        if not stat.S_ISREG(path.stat().st_mode) or relative.startswith('__gardendless/'):
            raise PackageError(f'unexpected source payload: {relative}')
        if path.name == '.DS_Store' or relative == 'tauri-dumper.manifest.json' or path in originals or path in legacy_gpu_alternatives:
            continue
        if '/native/' in relative and path.suffix.lower() in ('.png', '.avif', '.jpg', '.jpeg', '.webp', '.ktx', '.pvr', '.pkm'):
            raise PackageError(f'unprocessed native image: {relative}')
        if path.suffix.lower() == '.astc' and path not in selected_astc:
            raise PackageError(f'unreferenced ASTC file: {relative}')
        files.append((relative, path))
    if len(files) > MAX_FILES or sum(path.stat().st_size for _, path in files) > MAX_TOTAL_BYTES or any(path.stat().st_size > MAX_FILE_BYTES for _, path in files):
        raise PackageError('payload exceeds package budgets')
    docs = output / 'docs'
    docs.mkdir(parents=True)
    for relative, path in files:
        target = docs / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    (docs / 'index.html').write_text(html, encoding='utf-8', newline='')
    metadata = docs / '__gardendless' / 'package'
    metadata.mkdir(parents=True)
    index = {'schemaVersion': 1, 'files': [
        {'path': relative, 'sizeBytes': (docs / relative).stat().st_size, 'sha256': hash_file(docs / relative)}
        for relative, _ in sorted(files, key=lambda item: sort_key(item[0]))
    ]}
    index_data = json_bytes(index)
    manifest = {'schemaVersion': 1, 'gameVersion': version.group(1), 'cocosVersion': settings['CocosEngine'],
                'hasGpNext': has_gp, 'gpNextVersion': None, 'producerCommit': producer_commit, 'minHostAbi': HOST_ABI,
                'fileIndexSha256': hashlib.sha256(index_data).hexdigest()}
    (docs / INDEX_PATH).write_bytes(index_data)
    (docs / MANIFEST_PATH).write_bytes(json_bytes(manifest))
    archive_path = output / 'GardendlessResources.zip'
    with zipfile.ZipFile(archive_path, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(docs.rglob('*')):
            if path.is_file():
                archive.write(path, 'docs/' + path.relative_to(docs).as_posix())
    verify_package_zip(archive_path)
    return archive_path


def verify_package_zip(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            members = {}
            total = 0
            for entry in archive.infolist():
                name = safe_path(entry.filename)
                if not name.startswith('docs/') or entry.is_dir() or name in members or entry.flag_bits & 1 or stat.S_IFMT(entry.external_attr >> 16) not in (0, stat.S_IFREG):
                    raise PackageError(f'invalid or duplicate archive entry: {name}')
                if entry.file_size > MAX_FILE_BYTES:
                    raise PackageError('archive member exceeds byte budget')
                total += entry.file_size
                members[name] = entry
            if len(members) > MAX_FILES or total > MAX_TOTAL_BYTES:
                raise PackageError('archive exceeds entry or byte budget')
            def metadata(relative, maximum):
                entry = members.pop('docs/' + relative, None)
                if entry is None or entry.file_size > maximum:
                    raise PackageError('package metadata is missing or too large')
                return archive.read(entry)
            manifest_data = metadata(MANIFEST_PATH, 64 * 1024)
            index_data = metadata(INDEX_PATH, 32 * 1024 * 1024)
            manifest, index = read_json(manifest_data), read_json(index_data)
            if not isinstance(manifest, dict) or set(manifest) != MANIFEST_FIELDS or type(manifest['schemaVersion']) is not int or manifest['schemaVersion'] != 1 or not isinstance(manifest['gameVersion'], str) or not VERSION.fullmatch(manifest['gameVersion']) or manifest['cocosVersion'] != '3.8.4' or type(manifest['hasGpNext']) is not bool or (manifest['gpNextVersion'] is not None and (not isinstance(manifest['gpNextVersion'], str) or not VERSION.fullmatch(manifest['gpNextVersion']) or not manifest['hasGpNext'])) or not isinstance(manifest['producerCommit'], str) or not COMMIT.fullmatch(manifest['producerCommit']) or type(manifest['minHostAbi']) is not int or not 1 <= manifest['minHostAbi'] <= HOST_ABI or manifest['fileIndexSha256'] != hashlib.sha256(index_data).hexdigest():
                raise PackageError('manifest shape, ABI, or index digest is invalid')
            if not isinstance(index, dict) or set(index) != {'schemaVersion', 'files'} or type(index['schemaVersion']) is not int or index['schemaVersion'] != 1 or not isinstance(index['files'], list):
                raise PackageError('file index shape is invalid')
            previous = None
            for item in index['files']:
                if not isinstance(item, dict) or set(item) != {'path', 'sizeBytes', 'sha256'} or type(item['sizeBytes']) is not int or item['sizeBytes'] < 0 or not isinstance(item['sha256'], str) or not DIGEST.fullmatch(item['sha256']):
                    raise PackageError('file index entry is invalid')
                relative = safe_path(item['path'])
                if relative.startswith('__gardendless/package/') or (previous is not None and sort_key(previous) >= sort_key(relative)):
                    raise PackageError('index paths are reserved, duplicate, or unsorted')
                previous = relative
                entry = members.pop('docs/' + relative, None)
                if entry is None or entry.file_size != item['sizeBytes']:
                    raise PackageError('payload size or membership mismatch')
                digest, actual = hashlib.sha256(), 0
                with archive.open(entry) as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        actual += len(chunk)
                        if actual > item['sizeBytes']:
                            raise PackageError('expanded payload exceeds declared size')
                        digest.update(chunk)
                if actual != item['sizeBytes'] or digest.hexdigest() != item['sha256']:
                    raise PackageError('payload length or digest mismatch')
            if members:
                raise PackageError('unindexed archive payload')
            return 'sha256:' + hashlib.sha256(manifest_data).hexdigest()
    except (OSError, zipfile.BadZipFile, ValueError, KeyError, TypeError) as error:
        raise PackageError(f'invalid resource ZIP: {error}') from error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('texture_map', type=Path)
    parser.add_argument('--producer-commit', required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--source-dirty', action='store_true', help='Mark a local uncommitted producer validation in the external report')
    args = parser.parse_args()
    try:
        archive = assemble_package(args.source, args.output, args.texture_map, args.producer_commit)
        package_id = 'sha256:' + hash_file(args.output / 'docs' / MANIFEST_PATH)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_bytes(json_bytes({'packageId': package_id, 'archiveSha256': hash_file(archive), 'producerCommit': args.producer_commit, 'sourceDirty': args.source_dirty, 'archive': str(archive)}))
    except (PackageError, AstcError, OSError, ValueError, KeyError) as error:
        print(f'error: {error}')
        return 1
    print(f'Verified {archive}: {package_id}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
