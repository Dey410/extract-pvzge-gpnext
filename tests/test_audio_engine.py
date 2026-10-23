import subprocess
import unittest
from pathlib import Path

ENGINE = (Path(__file__).parents[1] / 'scripts' / 'audio_engine.js').read_text(encoding='utf-8')
HARNESS = '''const assert=require('node:assert/strict');
const fetches=[],decodes=[],sources=[],gains=[],errors=[],warnings=[],listeners={};
console.error=(...args)=>errors.push(args.join(' '));
console.warn=(...args)=>warnings.push(args.join(' '));
class Param{constructor(value){this.value=value;this.events=[]}
  cancelScheduledValues(t){this.events.push(['cancel',t])}setValueAtTime(v,t){this.events.push(['set',v,t])}
  linearRampToValueAtTime(v,t){this.events.push(['ramp',v,t])}}
class Context{constructor(options){this.options=options;this.currentTime=0;this.state='running';this.resumes=0;this.destination={};this.failResume=false}
  resume(){this.resumes++;if(this.failResume)return Promise.reject(new Error('refused'));this.state='running';return Promise.resolve()}
  decodeAudioData(data){return new Promise((resolve,reject)=>decodes.push({data,resolve,reject}))}
  createBufferSource(){const source={buffer:null,loop:false,loopEnd:0,playbackRate:{value:1},started:null,stops:0,onended:null,disconnected:false,
    connect(){},disconnect(){this.disconnected=true},start(when,offset){this.started={when,offset}},stop(){this.stops++}};sources.push(source);return source}
  createGain(){const gain={gain:new Param(1),connected:false,disconnected:false,connect(){this.connected=true},disconnect(){this.disconnected=true}};gains.push(gain);return gain}}
globalThis.window={AudioContext:Context,addEventListener(type,fn,capture){(listeners[type]=listeners[type]||[]).push([fn,capture])}};
globalThis.fetch=url=>new Promise((resolve,reject)=>fetches.push({url,resolve,reject}));
function respond(){fetches.at(-1).resolve({ok:true,status:200,arrayBuffer:()=>Promise.resolve(new ArrayBuffer(4))})}
function decoded(duration,channels=2,rate=32000){return {duration,sampleRate:rate,numberOfChannels:channels,length:Math.round(duration*rate)}}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function loaded(duration,channels){respond();await tick();decodes.at(-1).resolve(decoded(duration,channels));await tick();}
const near=(actual,expected)=>assert.ok(Math.abs(actual-expected)<1e-9,actual+' != '+expected);
ENGINE
const AudioEngine=__pvzgeCreateAudioEngine(32000,CACHE);
const manifest={Music_A:'Music/A b.mp3',Sound_X:'Sounds/x.mp3',Music_C:'Music/c.mp3',Music_D:'Music/d.mp3'};
const engine=new AudioEngine(manifest,new URL('https://game.test/audio/'));
(async()=>{await engine.init();const context=engine.context;SCENARIO})().catch(error=>{console.log(error.stack||String(error));process.exitCode=1});'''


class AudioEngineTests(unittest.TestCase):
    def run_js(self, scenario, cache=640000):
        program = HARNESS.replace('ENGINE', ENGINE).replace('CACHE', str(cache)).replace('SCENARIO', scenario)
        result = subprocess.run(['node', '-'], input=program, text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_play_fetches_and_decodes_only_on_first_play(self):
        self.run_js('''assert.deepEqual(context.options,{sampleRate:32000});assert.equal(fetches.length,0);
assert.deepEqual(engine.listClips(),Object.keys(manifest));assert.equal(engine.nameToPath('Music_A'),'Music/A b.mp3');
const id=engine.play('Music_A');assert.equal(id,1);assert.equal(fetches[0].url,'https://game.test/audio/Music/A%20b.mp3');
assert.equal(gains[0].gain.value,0.9);assert.equal(gains[0].connected,true);
assert.deepEqual(engine.getPosition(id),[0,null]);assert.equal(engine.getDuration(id),null);assert.equal(sources.length,0);
assert.equal(engine.play('Missing'),null);assert.deepEqual(warnings,['[audio] unknown clip: Missing']);
assert.equal(engine.cleanupFinished(),0);''')

    def test_clock_runs_at_its_rate_while_decoding_and_the_clip_joins_at_the_elapsed_position(self):
        self.run_js('''const id=engine.play('Music_A',1.5,1);context.currentTime=0.2;near(engine.getPosition(id)[0],0.3);
respond();await tick();context.currentTime=0.4;decodes[0].resolve(decoded(10));await tick();
assert.equal(sources.length,1);near(sources[0].started.offset,0.6);assert.equal(sources[0].playbackRate.value,1.5);
assert.equal(sources[0].loop,false);assert.equal(engine.getDuration(id),10);
context.currentTime=1.4;const [position,duration]=engine.getPosition(id);near(position,2.1);assert.equal(duration,10);''')

    def test_a_real_end_releases_the_voice_and_the_clip_is_reused(self):
        self.run_js('''const id=engine.play('Sound_X');await loaded(2);sources[0].onended();
assert.deepEqual(engine.getPosition(id),[0,null]);assert.equal(gains[0].disconnected,true);
const again=engine.play('Sound_X');await tick();assert.equal(fetches.length,1);assert.equal(sources.length,2);near(sources[1].started.offset,0);
engine.destroy(again);assert.equal(sources[1].onended,null);assert.equal(sources[1].stops,1);''')

    def test_a_failed_clip_keeps_the_clock_running_and_is_fetched_again_later(self):
        self.run_js('''const id=engine.play('Music_A');respond();await tick();decodes[0].reject(new Error('bad'));await tick();
assert.equal(errors.length,1);assert.equal(sources.length,0);context.currentTime=3;assert.deepEqual(engine.getPosition(id),[3,null]);
const missing=engine.play('Music_C');fetches.at(-1).resolve({ok:false,status:404});await tick();await tick();
assert.equal(errors.length,2);context.currentTime=4;assert.deepEqual(engine.getPosition(missing),[1,null]);
engine.destroy(id);engine.play('Music_A');assert.equal(fetches.length,3);''')

    def test_volume_uses_gain_and_a_fade_ramps_to_silence_before_destroying(self):
        self.run_js('''const id=engine.play('Music_A',1,0.5);await loaded(10);const gain=gains[0].gain;assert.equal(gain.value,0.5);
engine.setVolume(id,2);assert.equal(gain.value,1);engine.setVolume(id,0.4);assert.equal(gain.value,0.4);
context.currentTime=1;engine.fadeOut(id,20);
assert.deepEqual(gain.events,[['cancel',1],['set',0.4,1],['ramp',0,1.02]]);
engine.setVolume(id,0.8);assert.equal(gain.value,0.4);engine.fadeOut(id,5000);assert.equal(gain.events.length,3);
await sleep(40);assert.deepEqual(engine.getPosition(id),[0,null]);assert.equal(sources[0].stops,1);''')

    def test_pause_resume_and_seek_keep_the_position_and_seeking_past_the_end_ends_the_voice(self):
        self.run_js('''const id=engine.play('Music_A');await loaded(4);
context.currentTime=1;engine.setPlaying(id,false);assert.equal(sources[0].stops,1);context.currentTime=3;near(engine.getPosition(id)[0],1);
engine.seek(id,2.5);near(engine.getPosition(id)[0],2.5);engine.setPlaying(id,false);assert.equal(sources.length,1);
engine.setPlaying(id,true);assert.equal(sources.length,2);near(sources[1].started.offset,2.5);
context.currentTime=3.5;near(engine.getPosition(id)[0],3);engine.seek(id,9);assert.equal(sources[1].stops,1);
assert.deepEqual(engine.getPosition(id),[0,null]);assert.equal(gains[0].disconnected,true);''')

    def test_restart_before_end_loops_at_the_pool_restart_point(self):
        self.run_js('''const id=engine.play('Music_A',2);engine.setRestartBeforeEnd(id,2000);await loaded(10);
assert.equal(sources[0].loop,true);assert.equal(sources[0].loopEnd,9);
context.currentTime=4.75;near(engine.getPosition(id)[0],0.5);
const late=engine.play('Music_C');await loaded(6);context.currentTime=5;engine.setRestartBeforeEnd(late,1000);
assert.equal(sources[1].stops,1);assert.equal(sources[2].loop,true);assert.equal(sources[2].loopEnd,5);near(sources[2].started.offset,0.25);
const whole=engine.play('Music_D');engine.setRestartBeforeEnd(whole,60000);await loaded(3);assert.equal(sources[3].loopEnd,3);''')

    def test_voices_share_a_decode_and_released_clips_stay_within_the_cache(self):
        self.run_js('''const a=engine.play('Music_A'),b=engine.play('Music_A');assert.equal(fetches.length,1);
await loaded(1);assert.equal(sources.length,2);
engine.destroy(a);engine.destroy(b);engine.play('Music_A');await tick();assert.equal(fetches.length,1);engine.destroy(3);
for(const name of ['Music_C','Sound_X']){const id=engine.play(name);await loaded(1);engine.destroy(id);}
assert.equal(fetches.length,3);engine.play('Music_A');assert.equal(fetches.length,4);
engine.play('Music_C');await tick();assert.equal(fetches.length,4);
const big=engine.play('Music_D');await loaded(10);engine.destroy(big);engine.play('Sound_X');await tick();assert.equal(fetches.length,5);''', cache=600000)

    def test_the_pool_limit_drops_extra_voices(self):
        self.run_js('''for(let i=0;i<60;i++)assert.equal(engine.play('Sound_X'),i+1);
assert.equal(engine.play('Sound_X'),null);assert.deepEqual(warnings,['[audio] pool exhausted, drop: Sound_X']);
engine.destroy(1);assert.equal(engine.play('Sound_X'),61);''')

    def test_a_suspended_context_resumes_on_play_and_on_any_gesture(self):
        self.run_js('''context.state='suspended';engine.play('Sound_X');assert.equal(context.resumes,1);
engine.play('Sound_X');assert.equal(context.resumes,1);
context.state='suspended';for(const type of ['touchend','mouseup','keydown']){assert.equal(listeners[type][0][1],true);listeners[type][0][0]();}
assert.equal(context.resumes,2);context.state='interrupted';context.failResume=true;listeners.touchend[0][0]();await tick();
assert.equal(warnings.length,1);assert.match(warnings[0],/resume failed/);''')
