import unittest

from scripts.patch_audio_runtime import PatchError, patch_engine_source, patch_game_source


DOM_LOADER = (
    'function p9(t){return new Promise((function(e){var i=t.play();return void 0===i?e():i.then(e)}))}'
    't.load=function(e){return new Promise((function(i,n){t.loadNative(e).then((function(e){i(new t(e))})).catch(n)}))},'
    't.loadNative=function(t){return new Promise((function(e,i){'
    'var n=document.createElement("audio"),r="canplaythrough";'
    'Zs.os===Ys.IOS?r="loadedmetadata":Zs.browserType===Ws.FIREFOX&&(r="canplay");'
    'var s=setTimeout((function(){0===n.readyState?h():o()}),8e3),'
    'a=function(){clearTimeout(s),n.removeEventListener(r,o,!1),n.removeEventListener("error",h,!1)},'
    'o=function(){a(),e(n)},h=function(){a(),i(new Error("load audio failure - "+t))};'
    'n.addEventListener(r,o,!1),n.addEventListener("error",h,!1),n.src=t}))},'
    't.loadOneShotAudio=function(e,i){return new Promise((function(n,r){t.loadNative(e).then((function(t){var e=new v9(t,i);n(e)})).catch(r)}))},'
    't.loadOneShotAudio=function(e,i){return new Promise((function(n,r){t.loadNative(e).then((function(t){var r=new I9(t,i,e);n(r)})).catch(r)}))}'
)

ENGINE_TAIL = (
    'e.destroy=function(){bB.off(AB.EVENT_PAUSE,this._onInterruptedBegin,this),'
    'bB.off(AB.EVENT_RESUME,this._onInterruptedEnd,this),'
    'this._domAudio.removeEventListener("ended",this._onEnded),this._domAudio=null};'
    'P9.maxAudioChannel=48;'
)

DOM_SELECTORS = (
    '(null==i?void 0:i.audioLoadMode)!==s9.DOM_AUDIO&&E9.support?D9.load(e):y9.load(e);'
    '(null==e?void 0:e.audioLoadMode)!==s9.DOM_AUDIO&&E9.support?D9.loadNative(t):y9.loadNative(t);'
    '(null==i?void 0:i.audioLoadMode)!==s9.DOM_AUDIO&&E9.support?D9.loadOneShotAudio(t,e):y9.loadOneShotAudio(t,e)'
)


class PatchEngineSourceTests(unittest.TestCase):
    def test_returns_native_facade_and_forces_dom_backend(self):
        patched, changes = patch_engine_source(
            DOM_LOADER + DOM_SELECTORS + ENGINE_TAIL
        )

        self.assertIn(
            "window.__gardendlessNativeAudio.createNativeAudioHandle",
            patched,
        )
        self.assertEqual(patched.count('{role:"oneShot"}'), 2)
        self.assertIn('n.__pvzgeLazySrc=t', patched)
        self.assertIn("t.src=t.__pvzgeLazySrc", patched)
        self.assertIn(
            "this._domAudio&&this._domAudio.release&&this._domAudio.release(),"
            "this._domAudio=null",
            patched,
        )
        self.assertIn(
            "window.__gardendlessHostConfig.audioVoicePoolSize||48",
            patched,
        )
        self.assertIn('document.createElement("audio")', patched)
        self.assertNotIn('"canplaythrough"', patched)
        self.assertEqual(patched.count("DOM_AUDIO&&!1?"), 3)
        self.assertEqual(changes["dom_loader"], 1)
        self.assertEqual(changes["play_hook"], 1)
        self.assertEqual(changes["force_dom"], 3)
        self.assertEqual(changes["oneshot_role"], 2)
        self.assertEqual(changes["destroy_release"], 1)
        self.assertEqual(changes["max_channel"], 1)

    def test_rejects_unknown_engine_layout(self):
        with self.assertRaises(PatchError):
            patch_engine_source("unrelated JavaScript")


class PatchGameSourceTests(unittest.TestCase):
    def test_changes_sound_resources_default_to_dom_audio(self):
        source = (
            'System.register("chunks:///_virtual/SoundRescourses.ts",[],function(){});'
            "yn.loadmode=r.AudioType.WEB_AUDIO;"
        )

        patched, changes = patch_game_source(source)

        self.assertIn("yn.loadmode=r.AudioType.DOM_AUDIO", patched)
        self.assertEqual(changes, 1)

    def test_rejects_missing_sound_resources_default(self):
        with self.assertRaises(PatchError):
            patch_game_source("unrelated JavaScript")


if __name__ == "__main__":
    unittest.main()
