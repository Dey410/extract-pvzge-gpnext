import csv
import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.resource_package import PackageError, assemble_package, build_delta, verify_delta_zip, verify_package_zip


class PackageTests(unittest.TestCase):
    def fixture(self, texture_extension='.png'):
        root = Path(tempfile.mkdtemp(prefix='pvzge-package-retained-'))
        docs = root / 'source'
        files = {
            'index.html': '<html><head><title>PvZ2 Gardendless | 0.15.0</title><!--pvzge--><script type="module" src="/assets/index-test.js"></script></head><body></body></html>',
            'assets/index-test.js': "console.log('Playing version 0.15.0');",
            'tauri-dumper.manifest.json': '{"tool":"tauri-dumper"}',
            'src/settings.json': '{"CocosEngine":"3.8.4","engine":{},"assets":{},"launch":{}}',
            'src/import-map.json': '{}', 'cocos-js/cc.js': 'System.register;',
            'assets/resources/native/aa/aa-test'+texture_extension: 'original image',
            'assets/resources/import/aa/aa-test.json': '[{"fmt":"7@89","w":4,"h":4}]',
        }
        for path, text in files.items():
            target = docs / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        astc = bytes.fromhex('13aba15c') + bytes([4, 4, 1, 4, 0, 0, 4, 0, 0, 1, 0, 0]) + bytes(16)
        (docs / 'assets/resources/native/aa/aa-test.astc').write_bytes(astc)
        mapping = root / 'texture-map.csv'
        with mapping.open('w') as output:
            writer = csv.DictWriter(output, fieldnames=['source_path', 'width', 'height', 'astc_block'])
            writer.writeheader()
            writer.writerow({'source_path':'assets/resources/native/aa/aa-test'+texture_extension,'width':4,'height':4,'astc_block':'4x4'})
        return root, docs, mapping

    def test_fresh_package_excludes_originals_and_matches_its_sealed_index(self):
        root, source, mapping = self.fixture()
        result = assemble_package(source, root / 'package', mapping, 'a' * 40)
        package_id = verify_package_zip(result)
        with zipfile.ZipFile(result) as archive:
            names = archive.namelist()
            self.assertNotIn('docs/assets/resources/native/aa/aa-test.png', names)
            self.assertNotIn('docs/tauri-dumper.manifest.json', names)
            self.assertIn('docs/assets/resources/native/aa/aa-test.astc', names)
            manifest_bytes = archive.read('docs/__gardendless/package/resource-manifest.json')
            manifest = json.loads(manifest_bytes)
            self.assertEqual(len(manifest), 8)
            self.assertEqual(manifest['minHostAbi'], 1)
            self.assertEqual(package_id, 'sha256:' + hashlib.sha256(manifest_bytes).hexdigest())
            html = archive.read('docs/index.html').decode()
            self.assertIn('data-gardendless-src=', html)
            self.assertIn('hostAbi', html)
        self.assertTrue((source / 'assets/resources/native/aa/aa-test.png').exists())

    def test_selected_astc_omits_webp_and_its_legacy_gpu_alternatives(self):
        root, source, mapping = self.fixture(texture_extension='.webp')
        for extension in ['.pvr', '.pkm']:
            (source / ('assets/resources/native/aa/aa-test'+extension)).write_bytes(b'old alternate')
        result = assemble_package(source, root / 'package', mapping, 'a' * 40)
        with zipfile.ZipFile(result) as archive:
            names = archive.namelist()
            self.assertIn('docs/assets/resources/native/aa/aa-test.astc', names)
            for extension in ['.webp', '.pvr', '.pkm']:
                self.assertNotIn('docs/assets/resources/native/aa/aa-test'+extension, names)
                self.assertTrue((source / ('assets/resources/native/aa/aa-test'+extension)).exists())

    def test_rejects_hidden_payload_after_sealing(self):
        root, source, mapping = self.fixture()
        result = assemble_package(source, root / 'package', mapping, 'a' * 40)
        with zipfile.ZipFile(result, 'a') as archive:
            archive.writestr('docs/hidden.js', b'hidden')
        with self.assertRaises(PackageError):
            verify_package_zip(result)

    def test_rejects_unknown_cocos_before_formal_output(self):
        root, source, mapping = self.fixture()
        (source / 'src/settings.json').write_text('{"CocosEngine":"9.9.9"}')
        with self.assertRaises(PackageError):
            assemble_package(source, root / 'package', mapping, 'a' * 40)
        self.assertFalse((root / 'package').exists())

    def test_requires_exact_astc_metadata_without_image_fallback(self):
        root, source, mapping = self.fixture()
        (source / 'assets/resources/import/aa/aa-test.json').write_text('[{"fmt":"7@89_0"}]')
        with self.assertRaises(PackageError):
            assemble_package(source, root / 'package', mapping, 'a' * 40)

    def test_reopened_zip_rejects_crc_digest_paths_and_duplicate_entries(self):
        import struct
        import warnings
        root, source, mapping = self.fixture()
        result = assemble_package(source, root / 'package', mapping, 'a' * 40)
        with zipfile.ZipFile(result) as archive:
            entries = [(info.filename, archive.read(info)) for info in archive.infolist()]
        for corruption in ['digest', 'path', 'duplicate', 'symlink', 'missingMetadata', 'futureAbi', 'unsorted']:
            target = root / (corruption + '.zip')
            content = list(entries)
            if corruption == 'digest':
                content = [(name, b'X' * len(data) if name.endswith('/cc.js') else data) for name, data in content]
            if corruption == 'path': content.append(('docs/../escaped', b'escape'))
            if corruption == 'duplicate': content.append(content[0])
            if corruption == 'missingMetadata': content = [(name, data) for name, data in content if not name.endswith('file-index.json')]
            if corruption == 'futureAbi':
                content = [(name, json.dumps(dict(json.loads(data), minHostAbi=2)).encode() if name.endswith('resource-manifest.json') else data) for name, data in content]
            if corruption == 'unsorted':
                index_name = 'docs/__gardendless/package/file-index.json'
                index = json.loads(dict(content)[index_name]);index['files'].reverse()
                data = json.dumps(index).encode();digest = hashlib.sha256(data).hexdigest()
                content = [(name, data if name == index_name else json.dumps(dict(json.loads(value), fileIndexSha256=digest)).encode() if name.endswith('resource-manifest.json') else value) for name, value in content]
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', UserWarning)
                with zipfile.ZipFile(target, 'x') as archive:
                    for name, data in content: archive.writestr(name, data)
                    if corruption == 'symlink':
                        info = zipfile.ZipInfo('docs/link');info.external_attr = 0o120777 << 16
                        archive.writestr(info, b'outside')
            with self.subTest(corruption=corruption), self.assertRaises(PackageError):
                verify_package_zip(target)
        stored = root / 'stored.zip'
        with zipfile.ZipFile(stored, 'x', compression=zipfile.ZIP_STORED) as archive:
            for name, data in entries: archive.writestr(name, data)
        with zipfile.ZipFile(stored) as archive: info = archive.getinfo('docs/cocos-js/cc.js')
        raw = bytearray(stored.read_bytes())
        name_len, extra_len = struct.unpack_from('<HH', raw, info.header_offset + 26)
        raw[info.header_offset + 30 + name_len + extra_len] ^= 1
        corrupt = root / 'bad-crc.zip';corrupt.write_bytes(raw)
        with self.assertRaises(PackageError): verify_package_zip(corrupt)


class DeltaTests(unittest.TestCase):
    fixture = PackageTests.fixture

    def releases(self):
        root, source, mapping = self.fixture()
        base = assemble_package(source, root / 'base', mapping, 'a' * 40)
        (source / 'cocos-js/cc.js').write_text('System.register; // next')
        package = assemble_package(source, root / 'package', mapping, 'b' * 40)
        return root, source, mapping, base, package

    def test_delta_keeps_changed_files_and_restores_the_new_package(self):
        root, _, _, base, package = self.releases()
        delta = build_delta(package, base, root / 'delta.zip')
        with zipfile.ZipFile(delta) as archive, zipfile.ZipFile(package) as full:
            self.assertEqual(set(archive.namelist()), {'docs/cocos-js/cc.js', 'docs/__gardendless/package/resource-manifest.json', 'docs/__gardendless/package/file-index.json', 'docs/__gardendless/package/delta.json'})
            for name in ['docs/__gardendless/package/resource-manifest.json', 'docs/__gardendless/package/file-index.json']:
                self.assertEqual(archive.read(name), full.read(name))
            self.assertEqual(json.loads(archive.read('docs/__gardendless/package/delta.json')), {'schemaVersion': 1, 'basePackageId': verify_package_zip(base)})
        self.assertEqual(verify_delta_zip(delta, base, package), verify_package_zip(package))

    def test_delta_rejects_another_base_missing_changes_and_tampering(self):
        root, source, mapping, base, package = self.releases()
        delta = build_delta(package, base, root / 'delta.zip')
        (source / 'cocos-js/cc.js').write_text('System.register; // other')
        other = assemble_package(source, root / 'other', mapping, 'c' * 40)
        with self.assertRaises(PackageError):
            verify_delta_zip(delta, other, package)
        with zipfile.ZipFile(delta) as archive:
            entries = [(info.filename, archive.read(info)) for info in archive.infolist()]
        for corruption in ['missing', 'tampered', 'hidden']:
            target = root / (corruption + '.zip')
            content = list(entries)
            if corruption == 'missing': content = [(name, data) for name, data in content if not name.endswith('/cc.js')]
            if corruption == 'tampered': content = [(name, b'X' * len(data) if name.endswith('/cc.js') else data) for name, data in content]
            if corruption == 'hidden': content.append(('docs/hidden.js', b'hidden'))
            with zipfile.ZipFile(target, 'x') as archive:
                for name, data in content: archive.writestr(name, data)
            with self.subTest(corruption=corruption), self.assertRaises(PackageError):
                verify_delta_zip(target, base, package)


class AbiGuardTests(unittest.TestCase):
    def test_ignores_the_commented_module_present_in_actual_015_html(self):
        from scripts.resource_package import guard_entry
        html, entry = guard_entry('<html><head><script type="module" src="/assets/index.js"></script></head><body><!-- <script src="addon/patcher.js" type="module"></script> --></body></html>')
        self.assertEqual(entry, 'assets/index.js')
        self.assertIn('data-gardendless-src="/assets/index.js"', html)

    def test_missing_abi_never_starts_entry_and_ready_document_runs_immediately(self):
        import subprocess
        from scripts.resource_package import guard_entry
        html, _ = guard_entry('<html><head><script type="module" src="/assets/index.js"></script></head><body></body></html>')
        script = html.split('<script>', 1)[1].split('</script>', 1)[0]
        for abi in [None, 0, 1]:
            for ready in ['loading', 'complete']:
                code = '''const assert=require('node:assert/strict');let listener;let started=[];
const window={__gardendlessHost:{config:{hostAbi:ABI}}};
const document={readyState:READY,body:{textContent:''},addEventListener:(e,f)=>listener=f,createElement:()=>({}),querySelector:()=>({crossOrigin:'anonymous',getAttribute:()=>'/assets/index.js',replaceWith:s=>started.push(s.src)})};
SCRIPT
if(listener)listener();assert.equal(started.length,ABI===1?1:0);if(ABI!==1)assert.match(document.body.textContent,/0.9.0/);
'''.replace('ABI', json.dumps(abi)).replace('READY', json.dumps(ready)).replace('SCRIPT', script)
                result = subprocess.run(['node', '-'], input=code, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
