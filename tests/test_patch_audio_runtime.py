import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.patch_audio_runtime import AUDIO_COMMANDS, PatchError, patch_docs, patch_entry_source

ENTRY = '''import{x as O}from"./chunk.js";var z=class{constructor(e,t){this.audioRoot=t,this.manifest=e}async init(){}};
async function ae(){let e=new URL(`audio/`,document.baseURI),t=await fetch(new URL(`manifest.json`,e));if(!t.ok)throw Error(`Audio manifest HTTP ${t.status}`);let n=new z(await t.json(),e);await n.init(),window.audioEngine=n}export{ae as start};'''
AUDIO_API = 'System.register("chunks:///_virtual/TauriAudioAPI.ts",[],function(){var commands=[COMMANDS]});System.register("chunks:///_virtual/Other.ts",[],function(){"audio_other"});'


class PatchDocsTests(unittest.TestCase):
    def fixture(self, commands=AUDIO_COMMANDS):
        root = Path(tempfile.mkdtemp(prefix='pvzge-audio-retained-'))
        for path, text in {
            'src/settings.json': '{"CocosEngine":"3.8.4"}',
            'audio/manifest.json': '{"Music_A":"Music/A b.mp3","Sound_X":"Sounds/x.mp3"}',
            'audio/Music/A b.mp3': 'music', 'audio/Sounds/x.mp3': 'sound',
            'assets/resources/config.json': '{"types":["cc.ImageAsset","cc.Prefab"]}',
            'assets/main/index.js': AUDIO_API.replace('COMMANDS', ','.join(f'"{c}"' for c in sorted(commands))),
            'assets/game-test.js': ENTRY, 'assets/chunk.js': 'export const x=1;',
        }.items():
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text(text)
        return root

    def test_entry_creates_the_web_audio_engine_with_the_game_manifest(self):
        root = self.fixture()
        patch_docs(root, root / 'reports' / 'audio.json')
        entry = (root / 'assets' / 'game-test.js').read_text()
        self.assertIn('let n=new __pvzgeAudioEngine(await t.json(),e);await n.init(),window.audioEngine=n}', entry)
        report = json.loads((root / 'reports' / 'audio.json').read_text())
        self.assertEqual(report['entry'], 'assets/game-test.js')
        self.assertEqual((report['audioContextSampleRate'], report['audioCacheBytes'], report['clips']), (32000, 32 * 1024 * 1024, 2))
        program = '''import assert from 'node:assert/strict';import {pathToFileURL} from 'node:url';
class Context{constructor(options){this.options=options}}
globalThis.window={AudioContext:Context,navigator:{},addEventListener(){}};globalThis.document={baseURI:'https://game.test/'};
globalThis.fetch=async url=>({ok:true,json:async()=>({Music_A:'Music/A b.mp3'})});
const {start}=await import(pathToFileURL(process.argv[2]).href);await start();
assert.equal(window.audioEngine.constructor.name,'AudioEngine');assert.deepEqual(window.audioEngine.context.options,{sampleRate:32000});
assert.equal(window.audioEngine.audioRoot.href,'https://game.test/audio/');assert.deepEqual(window.audioEngine.listClips(),['Music_A']);'''
        result = subprocess.run(['node', '--input-type=module', '-', str(root / 'assets' / 'game-test.js')],
                                input=program, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unknown_layouts_fail_before_writing(self):
        cases = {
            'missing clip': lambda root: (root / 'audio/Sounds/x.mp3').unlink(),
            'unsafe clip': lambda root: (root / 'audio/manifest.json').write_text('{"A":"../index.html"}'),
            'cocos audio': lambda root: (root / 'assets/resources/config.json').write_text('{"types":["cc.AudioClip"]}'),
            'second pool': lambda root: (root / 'assets/game-test.js').write_text(ENTRY + ENTRY.split(';', 1)[1]),
            'second module': lambda root: (root / 'assets/other.js').write_text('window.audioEngine=null'),
        }
        for name, corrupt in cases.items():
            root = self.fixture()
            corrupt(root)
            with self.subTest(case=name), self.assertRaises(PatchError):
                patch_docs(root, root / 'reports' / 'audio.json')
        for commands in [AUDIO_COMMANDS | {'audio_set_speed'}, AUDIO_COMMANDS - {'audio_seek'}]:
            root = self.fixture(commands)
            with self.subTest(commands=sorted(commands ^ AUDIO_COMMANDS)), self.assertRaises(PatchError):
                patch_docs(root, root / 'reports' / 'audio.json')
            self.assertEqual((root / 'assets' / 'game-test.js').read_text(), ENTRY)

    def test_unparseable_result_fails(self):
        with self.assertRaises(PatchError):
            patch_entry_source(ENTRY.replace('export{ae as start};', 'export{'))
