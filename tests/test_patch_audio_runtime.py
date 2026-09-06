import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.patch_audio_runtime import (
    PatchError,
    classify_audio_files,
    patch_docs,
    patch_engine_source,
    probe_audio_duration,
    require_ffprobe,
)


DOM_LOADER = (
    'function p9(t){return new Promise((function(e){var i=t.play();return void 0===i?e():i.then(e)}))}'
    't.loadNative=function(t){return new Promise((function(e,i){'
    'var n=document.createElement("audio"),r="canplaythrough";'
    'Zs.os===Ys.IOS?r="loadedmetadata":Zs.browserType===Ws.FIREFOX&&(r="canplay");'
    'var s=setTimeout((function(){0===n.readyState?h():o()}),8e3),'
    'a=function(){clearTimeout(s),n.removeEventListener(r,o,!1),n.removeEventListener("error",h,!1)},'
    'o=function(){a(),e(n)},h=function(){a(),i(new Error("load audio failure - "+t))};'
    'n.addEventListener(r,o,!1),n.addEventListener("error",h,!1),n.src=t}))},'
    't.loadOneShotAudio=function(e,i){return 0}'
)

DOM_SELECTORS = (
    '(null==i?void 0:i.audioLoadMode)!==s9.DOM_AUDIO&&E9.support?D9.load(e):y9.load(e);'
    '(null==e?void 0:e.audioLoadMode)!==s9.DOM_AUDIO&&E9.support?D9.loadNative(t):y9.loadNative(t);'
    '(null==i?void 0:i.audioLoadMode)!==s9.DOM_AUDIO&&E9.support?D9.loadOneShotAudio(t,e):y9.loadOneShotAudio(t,e)'
)

GAME_SOURCE = (
    'System.register("chunks:///_virtual/SoundRescourses.ts",[],function(){});'
    "yn.loadmode=r.AudioType.WEB_AUDIO;"
)


class PatchEngineSourceTests(unittest.TestCase):
    def test_preloads_dom_metadata_and_routes_only_listed_paths_to_dom(self):
        patched, changes = patch_engine_source(
            DOM_LOADER + DOM_SELECTORS,
            ["assets/resources/native/ab/long-track.mp3"],
        )

        self.assertIn('n.preload="metadata"', patched)
        self.assertIn("n.__pvzgeLazySrc=t", patched)
        self.assertIn("t.src=t.__pvzgeLazySrc", patched)
        self.assertNotIn('"canplaythrough"', patched)
        self.assertIn('"assets/resources/native/ab/long-track.mp3"', patched)
        self.assertIn("new URL(t,document.baseURI).pathname", patched)
        self.assertEqual(patched.count("E9.support&&!__pvzgeUseDomAudio(e)?"), 1)
        self.assertEqual(patched.count("E9.support&&!__pvzgeUseDomAudio(t)?"), 2)
        self.assertNotIn("audioLoadMode)!==", patched)
        self.assertNotIn("DOM_AUDIO&&!1?", patched)
        self.assertEqual(
            changes,
            {"dom_loader": 1, "play_hook": 1, "hybrid_selector": 3},
        )

    def test_rejects_unknown_engine_layout(self):
        with self.assertRaises(PatchError):
            patch_engine_source("unrelated JavaScript", [])


class DomAudioLifecycleTests(unittest.TestCase):
    def run_audio_scenario(self, scenario):
        patched, _ = patch_engine_source(DOM_LOADER + ";" + DOM_SELECTORS, [])
        # Run the generated loader and play hook; routing has separate coverage.
        runtime = patched.split("E9.support&&!__pvzgeUseDomAudio", 1)[0]
        program = r'''
const assert = require("node:assert/strict");
const t = {};
function makeAudio(src = "") {
    return {
        src, events: [],
        load() { this.events.push(["load", this.src, this.preload]); },
        play() { this.events.push(["play", this.src]); return Promise.resolve(); }
    };
}
const document = { createElement: () => makeAudio() };
RUNTIME
(async () => {
SCENARIO
})().catch(error => { console.error(error); process.exitCode = 1; });
'''.replace("RUNTIME", runtime).replace("SCENARIO", scenario)
        result = subprocess.run(
            ["node", "-"], input=program, capture_output=True, text=True, timeout=10
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_loader_requests_metadata_before_any_play(self):
        self.run_audio_scenario('''
const audio = await t.loadNative("long.m4a");
assert.equal(audio.src, "long.m4a");
assert.equal(audio.__pvzgeLazySrc, "long.m4a");
assert.deepEqual(audio.events, [["load", "long.m4a", "metadata"]]);
''')

    def test_play_does_not_reload_a_prebound_source(self):
        self.run_audio_scenario('''
const audio = await t.loadNative("long.m4a");
audio.events = [];
audio.__pvzgeLazySrc = "stale.m4a";
await p9(audio);
await p9(audio);
assert.deepEqual(audio.events, [["play", "long.m4a"], ["play", "long.m4a"]]);
''')

    def test_legacy_unbound_source_loads_once_before_play(self):
        self.run_audio_scenario('''
const audio = makeAudio();
audio.__pvzgeLazySrc = "legacy.m4a";
await p9(audio);
await p9(audio);
assert.deepEqual(audio.events, [
    ["load", "legacy.m4a", undefined],
    ["play", "legacy.m4a"], ["play", "legacy.m4a"]
]);
''')

    def test_missing_fallback_url_does_not_trigger_load(self):
        self.run_audio_scenario('''
const audio = makeAudio();
await p9(audio);
assert.deepEqual(audio.events, [["play", ""]]);
''')


class AudioClassificationTests(unittest.TestCase):
    def test_uses_dom_at_ten_seconds_and_web_audio_below_ten_seconds(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            docs_dir = Path(temporary_directory)
            short_path = docs_dir / "assets" / "short.mp3"
            boundary_path = docs_dir / "assets" / "boundary.ogg"
            failed_path = docs_dir / "assets" / "unknown.wav"
            ignored_path = docs_dir / "assets" / "texture.png"
            for path in (short_path, boundary_path, failed_path, ignored_path):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()

            def probe_duration(path: Path) -> float:
                if path == short_path:
                    return 9.999
                if path == boundary_path:
                    return 10.0
                raise RuntimeError("unsupported stream")

            decisions = classify_audio_files(docs_dir, 10.0, probe_duration)

        self.assertEqual(
            [(item.relative_path, item.backend) for item in decisions],
            [
                ("assets/boundary.ogg", "DOM_AUDIO"),
                ("assets/short.mp3", "WEB_AUDIO"),
                ("assets/unknown.wav", "DOM_AUDIO"),
            ],
        )
        self.assertEqual(decisions[0].duration_seconds, 10.0)
        self.assertEqual(decisions[1].duration_seconds, 9.999)
        self.assertIsNone(decisions[2].duration_seconds)
        self.assertEqual(decisions[2].error, "unsupported stream")

    def test_requires_ffprobe_before_classification(self):
        with self.assertRaisesRegex(PatchError, "ffprobe"):
            require_ffprobe(lambda command: None)

    def test_reads_a_finite_duration_from_ffprobe(self):
        with patch("scripts.patch_audio_runtime.subprocess.run") as run:
            run.return_value = Mock(returncode=0, stdout="10.250000\n", stderr="")

            duration = probe_audio_duration("/usr/bin/ffprobe", Path("audio.mp3"))

        self.assertEqual(duration, 10.25)

    def test_rejects_an_invalid_ffprobe_duration(self):
        with patch("scripts.patch_audio_runtime.subprocess.run") as run:
            run.return_value = Mock(returncode=0, stdout="N/A\n", stderr="")

            with self.assertRaisesRegex(RuntimeError, "invalid duration"):
                probe_audio_duration("/usr/bin/ffprobe", Path("audio.mp3"))


class PatchDocsTests(unittest.TestCase):
    def test_patches_only_the_engine_and_reports_every_audio_decision(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            docs_dir = root / "docs"
            engine_path = docs_dir / "cocos-js" / "_virtual_cc-test.js"
            game_path = docs_dir / "assets" / "main" / "index.js"
            short_path = docs_dir / "assets" / "resources" / "native" / "short.mp3"
            long_path = docs_dir / "assets" / "resources" / "native" / "long.mp3"
            failed_path = docs_dir / "assets" / "resources" / "native" / "failed.mp3"
            report_path = root / "reports" / "audio-runtime-patch.txt"

            engine_path.parent.mkdir(parents=True)
            game_path.parent.mkdir(parents=True)
            short_path.parent.mkdir(parents=True)
            engine_path.write_text(DOM_LOADER + DOM_SELECTORS, encoding="utf-8")
            game_path.write_text(GAME_SOURCE, encoding="utf-8")
            short_path.touch()
            long_path.touch()
            failed_path.touch()
            game_before = game_path.read_bytes()

            def probe_duration(_ffprobe_path: str, path: Path) -> float:
                if path == short_path:
                    return 2.5
                if path == long_path:
                    return 30.0
                raise RuntimeError("cannot read duration")

            patch_docs(
                docs_dir,
                report_path,
                ffprobe_path="/fake/ffprobe",
                duration_probe=probe_duration,
            )

            patched_engine = engine_path.read_text(encoding="utf-8")
            game_after = game_path.read_bytes()
            report = report_path.read_text(encoding="utf-8")

        self.assertEqual(game_after, game_before)
        self.assertNotIn("short.mp3", patched_engine)
        self.assertIn("long.mp3", patched_engine)
        self.assertIn("failed.mp3", patched_engine)
        self.assertIn("Threshold seconds: 10.000", report)
        self.assertIn("WEB_AUDIO | 2.500 | assets/resources/native/short.mp3", report)
        self.assertIn("DOM_AUDIO | 30.000 | assets/resources/native/long.mp3", report)
        self.assertIn("DOM_AUDIO | unknown | assets/resources/native/failed.mp3", report)
        self.assertIn("cannot read duration", report)


if __name__ == "__main__":
    unittest.main()
