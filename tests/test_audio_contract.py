import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.patch_audio_runtime import patch_engine_source
from tests.audio_fixture import DOM_LOADER, ONE_SHOT

SELECTORS = '''
var s9={DOM_AUDIO:0,WEB_AUDIO:1},E9={support:true};
var D9={load:()=>Promise.resolve('web'),loadNative:()=>Promise.resolve('web'),loadOneShotAudio:()=>Promise.resolve('web')};
var y9={load:()=>Promise.resolve('dom'),loadNative:()=>Promise.resolve('dom'),loadOneShotAudio:()=>Promise.resolve('dom')};
var W9={};
W9.load=function(e,i){return(null==i?void 0:i.audioLoadMode)!==s9.DOM_AUDIO&&E9.support?D9.load(e):y9.load(e)};
W9.loadNative=function(t,e){return(null==e?void 0:e.audioLoadMode)!==s9.DOM_AUDIO&&E9.support?D9.loadNative(t):y9.loadNative(t)};
W9.loadOneShotAudio=function(t,e,i){return(null==i?void 0:i.audioLoadMode)!==s9.DOM_AUDIO&&E9.support?D9.loadOneShotAudio(t,e):y9.loadOneShotAudio(t,e)};
'''
DECODE = '''
var C9={getCache:()=>null,addCache:()=>{},retainCache:()=>{}};
var T9={decodeAudioData:()=>Promise.reject(new Error('bad decode'))};
var z=()=>{};
var Web={};Web.loadNative=function(t){return new Promise((function(e,i){var n=C9.getCache(t);if(n)return C9.retainCache(t),void e(n);var r=new XMLHttpRequest,s="load audio failed: "+t+", status: ";r.open("GET",t,!0),r.responseType="arraybuffer",r.onload=function(){200===r.status||0===r.status?T9.decodeAudioData(r.response).then((function(i){C9.addCache(t,i),e(i)})).catch((function(e){z("loadNative error",t,e)})):i(new Error(""+s+r.status+"(no response)"))},r.onerror=function(){i(new Error(""+s+r.status+"(error)"))},r.ontimeout=function(){i(new Error(""+s+r.status+"(time out)"))},r.onabort=function(){i(new Error(""+s+r.status+"(abort)"))},r.send(null)}))};
var Decoder=function(){};var e=Decoder.prototype;
e.decodeAudioData=function(t){var e=this;return new Promise((function(i,n){var r=e._context.decodeAudioData(t,(function(t){i(t)}),(function(t){console.error("failed to load Web Audio",t)}));null==r||r.catch(n)}))};
var Player=function(){};e=Player.prototype;
e.destroy=function(){EB.off(bB.EVENT_PAUSE,this._onInterruptedBegin,this),EB.off(bB.EVENT_RESUME,this._onInterruptedEnd,this),this._domAudio.removeEventListener("ended",this._onEnded),this._domAudio=null};
'''
ENGINE = DOM_LOADER + ';' + SELECTORS + DECODE + ONE_SHOT


class AudioContractTests(unittest.TestCase):
    def run_js(self, scenario):
        patched, _ = patch_engine_source(ENGINE, ['assets/resources/native/aa/music.mp3'])
        program = '''const assert=require('node:assert/strict');const deadline=globalThis.setTimeout(()=>{console.error('unsettled audio promise');process.exitCode=1},500);
const t={};let audio;let timers=[];
function n(type,entries){for(const entry of entries)Object.defineProperty(type.prototype,entry.key,{get:entry.get,set:entry.set})}
const setTimeout=(f)=>{timers.push(f);return timers.length};const clearTimeout=()=>{};
const document={baseURI:'https://game/',createElement:()=>audio={src:'',readyState:0,duration:NaN,events:new Map(),preload:'',addEventListener(k,f){this.events.set(k,f)},removeEventListener(k){this.events.delete(k)},load(){},fire(k){this.events.get(k)?.()},play(){return Promise.resolve()}}};
const XMLHttpRequest=class{open(){}send(){this.status=200;queueMicrotask(()=>this.onload())}};
RUNTIME
(async()=>{SCENARIO})().then(()=>globalThis.clearTimeout(deadline),e=>{console.error(e);process.exitCode=1;globalThis.clearTimeout(deadline)});'''.replace('RUNTIME', patched).replace('SCENARIO', scenario)
        result = subprocess.run(['node', '-'], input=program, text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_all_three_return_entries_preserve_explicit_modes(self):
        self.run_js('''for(const f of [W9.load,W9.loadNative,(u,o)=>W9.loadOneShotAudio(u,1,o)]){
assert.equal(await f('short.mp3',{audioLoadMode:0}),'dom');
assert.equal(await f('assets/resources/native/aa/music.mp3',{audioLoadMode:1}),'web');
assert.equal(await f('assets/resources/native/aa/music.mp3'),'dom');
assert.equal(await f('short.mp3'),'web');}''')

    def test_metadata_error_abort_and_timeout_settle_once(self):
        self.run_js('''let settled=false;const pending=t.loadNative('music.mp3').then(()=>settled=true);await Promise.resolve();assert.equal(settled,false);
audio.readyState=1;audio.duration=30;audio.fire('loadedmetadata');await pending;assert.equal(settled,true);assert.equal(audio.events.size,0);
for(const reason of ['error','abort','timeout']){const pending=t.loadNative('bad.mp3');if(reason==='timeout')timers.at(-1)();else audio.fire(reason);await assert.rejects(pending);assert.equal(audio.events.size,0);}''')

    def test_decode_failure_rejects_outer_loader_and_legacy_callback(self):
        self.run_js('''await assert.rejects(Web.loadNative('bad.mp3'),/bad decode/);
const decoder=new Decoder();decoder._context={decodeAudioData:(data,ok,fail)=>fail(new Error('callback failure'))};
await assert.rejects(decoder.decodeAudioData(new ArrayBuffer(0)),/callback failure/);''')

    def test_one_shot_uses_real_ended_and_releases_source_gain_once(self):
        self.run_js('''let releases=0,disconnected=0,stops=0,started=0,ends=0;
C9.tryReleasingCache=()=>releases++;
T9.createBufferSource=()=>({playbackRate:{value:1},buffer:{},connect(){},disconnect(){disconnected++},start(){started++},stop(){stops++}});
T9.createGain=()=>({disconnect(){disconnected++}});T9.connectContext=()=>{};T9.runContext=()=>Promise.resolve();
const voice=new M9({duration:10},1,'effect');voice.onEnd=()=>ends++;voice.play(0.5);
await Promise.resolve();const node=voice._bufferSourceNode;assert.equal(node.playbackRate.value,0.5);assert.equal(started,1);
assert.equal(timers.length,0);node.onended();voice.stop();assert.equal(releases,1);assert.equal(disconnected,2);assert.equal(ends,1);assert.equal(node.buffer,null);assert.equal(stops,0);''')

    def test_one_shot_cancellation_before_context_ready_cannot_start_later(self):
        self.run_js('''let release=0,start=0,stop=0,disconnect=0,resume;
C9.tryReleasingCache=()=>release++;
T9.createBufferSource=()=>({playbackRate:{value:1},buffer:{},connect(){},disconnect(){disconnect++},start(){start++},stop(){stop++}});
T9.createGain=()=>({disconnect(){disconnect++}});T9.connectContext=()=>{};
T9.runContext=()=>new Promise(resolve=>resume=resolve);
const voice=new M9({duration:10},1,'effect');voice.play(2);voice.stop();resume();await Promise.resolve();
assert.equal(start,0);assert.equal(stop,0);assert.equal(release,1);assert.equal(disconnect,2);''')

    def test_patched_engine_parses_as_complete_javascript(self):
        patched, _ = patch_engine_source(ENGINE, [])
        result = subprocess.run(['node', '--check', '-'], input=patched, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
