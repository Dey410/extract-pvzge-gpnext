import json
import tempfile
import unittest
from pathlib import Path

from scripts.patch_audio_runtime import PatchError, music_audio_paths, patch_docs, patch_engine_source
from tests.test_audio_contract import ENGINE


class PatchDocsTests(unittest.TestCase):
    def test_music_role_uses_exact_uuid_mapping_without_duration_probes(self):
        root = Path(tempfile.mkdtemp(prefix='pvzge-audio-retained-'))
        bundle = root / 'assets' / 'resources'
        (bundle / 'native' / 'fc').mkdir(parents=True)
        (bundle / 'native' / 'fc' / 'fc991dd7-0033-4b80-9d41-c8a86a702e59.mp3').write_bytes(b'music')
        (bundle / 'native' / 'fc' / 'effect.mp3').write_bytes(b'effect')
        (bundle / 'config.json').write_text(json.dumps({
            'uuids': ['fcmR3XADNLgJ1ByKhqcC5Z'], 'types': ['cc.AudioClip'],
            'paths': {'0': ['music/short-intro', 0, 1]},
        }))
        self.assertEqual(music_audio_paths(root), ['assets/resources/native/fc/fc991dd7-0033-4b80-9d41-c8a86a702e59.mp3'])
        (root / 'src').mkdir()
        (root / 'src' / 'settings.json').write_text('{"CocosEngine":"3.8.4"}')
        (root / 'cocos-js').mkdir()
        engine = root / 'cocos-js' / '_virtual_cc-test.js'
        engine.write_text(ENGINE)
        patch_docs(root, root / 'reports' / 'audio.json')
        self.assertIn('__pvzgeMusicPaths', engine.read_text())
        report = json.loads((root / 'reports' / 'audio.json').read_text())
        self.assertIn('explicit DOM_AUDIO use the decode-on-play', report['policy'])
        self.assertEqual(report['audioContextSampleRate'], 32000)
        self.assertEqual(report['musicPcmCacheBytes'], 32 * 1024 * 1024)
        self.assertEqual(report['musicPaths'], ['assets/resources/native/fc/fc991dd7-0033-4b80-9d41-c8a86a702e59.mp3'])

    def test_unknown_engine_layout_fails_explicitly(self):
        with self.assertRaises(PatchError):
            patch_engine_source('unrelated JavaScript', [])
