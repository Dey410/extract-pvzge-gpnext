#!/usr/bin/env python3
"""Swap the game's <audio> pool for the Web Audio engine; unknown layouts fail closed."""
import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path, PurePosixPath


class PatchError(RuntimeError):
    pass


AUDIO_CONTEXT_SAMPLE_RATE = 32000
AUDIO_CACHE_BYTES = 32 * 1024 * 1024
ENGINE_SOURCE = (Path(__file__).parent / 'audio_engine.js').read_text(encoding='utf-8')
IDENT = r'[A-Za-z_$][\w$]*'
# The commands TauriAudioAPI sends to window.audioEngine; audio_engine.js
# implements the methods behind exactly these.
AUDIO_COMMANDS = frozenset({
    'audio_play', 'audio_play_fire_and_forget', 'audio_get_duration', 'audio_get_position', 'audio_set_volume',
    'audio_set_playing', 'audio_seek', 'audio_fade_out', 'audio_set_restart_before_end', 'audio_destroy',
    'audio_cleanup', 'audio_list',
})
AUDIO_API_PATTERN = re.compile(r'System\.register\("chunks:///_virtual/TauriAudioAPI\.ts".*?(?=System\.register\(|\Z)', re.DOTALL)
POOL_PATTERN = re.compile(
    rf'let (?P<engine>{IDENT})=new (?P<pool>{IDENT})(?P<args>\(await {IDENT}\.json\(\),{IDENT}\);'
    rf'await (?P=engine)\.init\(\),window\.audioEngine=(?P=engine)\}})'
)


def audio_manifest(docs_dir: Path) -> dict[str, str]:
    manifest = json.loads((docs_dir / 'audio' / 'manifest.json').read_text(encoding='utf-8'))
    if not isinstance(manifest, dict) or not manifest:
        raise PatchError('audio manifest is not a non-empty object')
    for name, path in manifest.items():
        parts = PurePosixPath(path).parts if isinstance(path, str) else ()
        if not parts or path.startswith('/') or '..' in parts or not (docs_dir / 'audio' / path).is_file():
            raise PatchError(f'audio manifest entry is unsafe or missing: {name} -> {path!r}')
    return manifest


def check_cocos_has_no_audio(docs_dir: Path) -> None:
    for config in sorted((docs_dir / 'assets').glob('*/config.json')):
        if 'cc.AudioClip' in json.loads(config.read_text(encoding='utf-8')).get('types', []):
            raise PatchError(f'Cocos still loads AudioClips: {config.relative_to(docs_dir).as_posix()}')


def check_audio_commands(docs_dir: Path) -> None:
    match = AUDIO_API_PATTERN.search((docs_dir / 'assets' / 'main' / 'index.js').read_text(encoding='utf-8'))
    commands = set(re.findall(r'"(audio_[a-z_]+)"', match.group(0))) if match else set()
    if commands != AUDIO_COMMANDS:
        raise PatchError(f'unexpected TauriAudioAPI commands: {sorted(commands ^ AUDIO_COMMANDS)}')


def check_module(source: str) -> None:
    result = subprocess.run(['node', '--input-type=module', '--check', '-'], input=source, text=True,
                            capture_output=True, timeout=30)
    if result.returncode:
        raise PatchError(f'patched JavaScript did not parse: {result.stderr.strip()}')


def patch_entry_source(source: str) -> str:
    patched, count = POOL_PATTERN.subn(lambda m: f'let {m["engine"]}=new __pvzgeAudioEngine{m["args"]}', source)
    if count != 1:
        raise PatchError(f'expected one window.audioEngine pool construction, found {count}')
    engine = (ENGINE_SOURCE + f'var __pvzgeAudioEngine=__pvzgeCreateAudioEngine('
              f'{AUDIO_CONTEXT_SAMPLE_RATE},{AUDIO_CACHE_BYTES});\n')
    patched = engine + patched
    check_module(patched)
    return patched


def patch_docs(docs_dir: Path, report_path: Path) -> None:
    settings = json.loads((docs_dir / 'src' / 'settings.json').read_text(encoding='utf-8'))
    if settings.get('CocosEngine') != '3.8.4':
        raise PatchError('only the verified Cocos 3.8.4 layout is supported')
    manifest = audio_manifest(docs_dir)
    check_cocos_has_no_audio(docs_dir)
    check_audio_commands(docs_dir)
    candidates = [path for path in sorted((docs_dir / 'assets').glob('*.js'))
                  if b'window.audioEngine=' in path.read_bytes()]
    if len(candidates) != 1:
        raise PatchError(f'expected one module creating window.audioEngine, found {len(candidates)}')
    entry = candidates[0]
    before = entry.read_bytes()
    patched = patch_entry_source(before.decode('utf-8'))
    entry.write_text(patched, encoding='utf-8', newline='')
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps({
        'policy': 'window.audioEngine is a Web Audio engine with the <audio> pool interface: clips decode on '
                  'first play and are shared by URL, volume and fades use gain',
        'entry': entry.relative_to(docs_dir).as_posix(),
        'entrySha256Before': hashlib.sha256(before).hexdigest(),
        'entrySha256After': hashlib.sha256(patched.encode()).hexdigest(),
        'audioContextSampleRate': AUDIO_CONTEXT_SAMPLE_RATE,
        'audioCacheBytes': AUDIO_CACHE_BYTES,
        'clips': len(manifest),
    }, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('docs_dir', type=Path)
    parser.add_argument('report_path', type=Path)
    args = parser.parse_args()
    try:
        patch_docs(args.docs_dir, args.report_path)
    except (PatchError, OSError, ValueError, KeyError) as error:
        print(f'error: {error}')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
