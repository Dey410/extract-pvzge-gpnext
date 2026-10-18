import subprocess
import unittest
from pathlib import Path

PLAYER = (Path(__file__).parents[1] / 'scripts' / 'music_player.js').read_text(encoding='utf-8')
HARNESS = '''const assert=require('node:assert/strict');
const states={INIT:0,PLAYING:1,PAUSED:2,STOPPED:3,INTERRUPTED:4};
const events={ENDED:'ended',INTERRUPTION_BEGIN:'interruptionBegin',INTERRUPTION_END:'interruptionEnd'};
const types={DOM_AUDIO:0,WEB_AUDIO:1};
const busEvents={EVENT_PAUSE:'pause',EVENT_RESUME:'resume'};
const bus={handlers:new Map(),
  on(type,fn,target){this.handlers.set(type,(this.handlers.get(type)||[]).concat([[fn,target]]))},
  off(type,fn,target){this.handlers.set(type,(this.handlers.get(type)||[]).filter(([f,t])=>f!==fn||t!==target))},
  emit(type){for(const [fn,target] of (this.handlers.get(type)||[]).slice())fn.call(target)}};
const requests=[],decodes=[],sources=[],gains=[],errors=[],warnings=[];
console.error=(...args)=>errors.push(args);
console.warn=(...args)=>warnings.push(args.join(' '));
const agent={currentTime:0,isRunning:true,waiting:[],
  decodeAudioData(data){return new Promise((resolve,reject)=>decodes.push({data,resolve,reject}))},
  createBufferSource(buffer,loop){
    const rate={writes:0,current:1,get value(){return this.current},set value(v){this.current=v;this.writes++}};
    const source={buffer,loop,playbackRate:rate,started:null,stops:0,onended:null,connect(){},disconnect(){},start(when,offset){this.started={when,offset}},stop(){this.stops++}};
    sources.push(source);return source},
  createGain(value){const gain={value,writes:0,disconnected:false,disconnect(){this.disconnected=true}};gains.push(gain);return gain},
  setGainValue(gain,value){gain.value=value;gain.writes++},
  connectContext(){},
  runContext(){return this.isRunning?Promise.resolve():new Promise(resolve=>this.waiting.push(resolve))}};
globalThis.XMLHttpRequest=class{open(method,url){this.url=url}send(){requests.push(this)}};
function respond(){const request=requests.at(-1);request.status=200;request.response=new ArrayBuffer(4);request.onload()}
function decoded(duration,channels=2,rate=32000){return {duration,sampleRate:rate,numberOfChannels:channels,length:Math.round(duration*rate)}}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
async function playing(player,duration){await player.play();respond();await tick();decodes.at(-1).resolve(decoded(duration));await tick();}
const near=(actual,expected)=>assert.ok(Math.abs(actual-expected)<1e-9,actual+' != '+expected);
PLAYER
const Music=__pvzgeCreateMusicPlayer(()=>({agent,states,events,types,bus,busEvents}),CACHE);
(async()=>{SCENARIO})().catch(error=>{console.log(error.stack||String(error));process.exitCode=1});'''


class MusicPlayerTests(unittest.TestCase):
    def run_js(self, scenario, cache=600000):
        program = (HARNESS.replace('PLAYER', PLAYER).replace('CACHE', str(cache))
                   .replace('SCENARIO', scenario))
        result = subprocess.run(['node', '-'], input=program, text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_load_reads_and_decodes_nothing(self):
        self.run_js('''const player=await Music.load('a.mp3');
assert.equal(requests.length,0);assert.equal(decodes.length,0);assert.equal(gains.length,0);
assert.equal(player.state,states.INIT);assert.equal(player.duration,0);assert.equal(player.type,types.WEB_AUDIO);''')

    def test_clock_runs_while_decoding_and_the_track_joins_at_the_elapsed_position(self):
        self.run_js('''const player=await Music.load('a.mp3');player.playbackRate=1.5;
await player.play();assert.equal(player.state,states.PLAYING);assert.equal(requests.length,1);
agent.currentTime=0.2;near(player.currentTime,0.3);
respond();await tick();agent.currentTime=0.4;decodes[0].resolve(decoded(10));await tick();
assert.equal(sources.length,1);near(sources[0].started.offset,0.6);assert.equal(sources[0].playbackRate.value,1.5);''')

    def test_stop_while_decoding_never_starts_the_track(self):
        self.run_js('''const player=await Music.load('a.mp3');await player.play();await player.stop();
respond();await tick();decodes[0].resolve(decoded(1));await tick();
assert.equal(sources.length,0);assert.equal(player.state,states.STOPPED);assert.equal(player.currentTime,0);''')

    def test_rate_and_volume_writes_only_when_they_change(self):
        self.run_js('''const player=await Music.load('a.mp3');await playing(player,10);
const source=sources[0];agent.currentTime=1;near(player.currentTime,1);
for(let i=0;i<100;i++)player.playbackRate=2;
assert.equal(source.playbackRate.writes,2);agent.currentTime=1.5;near(player.currentTime,2);
for(let i=0;i<100;i++)player.volume=0.5;
assert.equal(gains[0].writes,1);assert.equal(gains[0].value,0.5);''')

    def test_real_end_emits_once_and_stop_does_not(self):
        self.run_js('''let ended=0;const player=await Music.load('a.mp3');player.onEnded(()=>ended++);await playing(player,5);
agent.currentTime=5;sources[0].onended();
assert.equal(ended,1);assert.equal(player.state,states.INIT);assert.equal(player.currentTime,0);
await player.play();assert.equal(sources.length,2);assert.equal(requests.length,1);near(sources[1].started.offset,0);
await player.stop();assert.equal(sources[1].onended,null);assert.equal(sources[1].stops,1);assert.equal(ended,1);''')

    def test_players_share_one_decode_and_released_tracks_stay_within_the_cache(self):
        self.run_js('''const a=await Music.load('a.mp3'),b=await Music.load('a.mp3');
await a.play();await b.play();assert.equal(requests.length,1);
respond();await tick();decodes[0].resolve(decoded(1));await tick();assert.equal(sources.length,2);
await a.stop();await a.play();await tick();assert.equal(requests.length,1);assert.equal(sources.length,3);
await a.stop();b.destroy();assert.ok(gains[1].disconnected);assert.equal(bus.handlers.get('pause').length,1);
const reused=await Music.load('a.mp3');await reused.play();await tick();assert.equal(requests.length,1);await reused.stop();
for(const url of ['b.mp3','c.mp3']){const player=await Music.load(url);await playing(player,1);await player.stop();}
assert.equal(requests.length,3);
const evicted=await Music.load('a.mp3');await evicted.play();assert.equal(requests.length,4);
const kept=await Music.load('b.mp3');await kept.play();await tick();assert.equal(requests.length,4);''')

    def test_a_failed_track_keeps_the_clock_running_and_logs_once(self):
        self.run_js('''const player=await Music.load('bad.mp3');await player.play();respond();await tick();
decodes[0].reject(new Error('bad'));await tick();
assert.equal(errors.length,1);assert.equal(player.state,states.PLAYING);assert.equal(sources.length,0);
agent.currentTime=3;near(player.currentTime,3);
const missing=await Music.load('missing.mp3');await missing.play();requests.at(-1).onerror();await tick();
assert.equal(errors.length,2);assert.equal(missing.state,states.PLAYING);
await player.stop();await player.play();assert.equal(requests.length,3);''')

    def test_pause_seek_loop_interruption_and_a_suspended_context(self):
        self.run_js('''const player=await Music.load('a.mp3');let begin=0,end=0;
player.onInterruptionBegin(()=>begin++);player.onInterruptionEnd(()=>end++);await playing(player,4);
agent.currentTime=1;await player.pause();assert.equal(player.state,states.PAUSED);agent.currentTime=3;near(player.currentTime,1);
await player.seek(2.5);near(player.currentTime,2.5);
await player.play();near(sources.at(-1).started.offset,2.5);
player.loop=true;assert.equal(sources.at(-1).loop,true);agent.currentTime=5.5;near(player.currentTime,1);
bus.emit('pause');await tick();assert.equal(player.state,states.INTERRUPTED);assert.equal(begin,1);
bus.emit('resume');await tick();assert.equal(player.state,states.PLAYING);assert.equal(end,1);
agent.isRunning=false;const waiting=await Music.load('a.mp3');let started=false;waiting.play().then(()=>started=true);await tick();
assert.equal(started,false);assert.equal(waiting.state,states.INIT);
agent.waiting.shift()();await tick();assert.equal(started,true);assert.equal(waiting.state,states.PLAYING);
assert.deepEqual(warnings,['[pvzge-audio] music waits for the audio context','[pvzge-audio] music starts after the audio context resumed']);''')

    def test_pause_stop_or_a_newer_play_cancel_a_start_waiting_for_the_context(self):
        self.run_js('''agent.isRunning=false;const player=await Music.load('a.mp3');
player.play();await player.pause();agent.waiting.shift()();await tick();
assert.equal(player.state,states.INIT);assert.equal(requests.length,0);
player.play();await player.stop();agent.waiting.shift()();await tick();
assert.equal(player.state,states.STOPPED);assert.equal(requests.length,0);
player.play();const latest=player.play();agent.waiting.shift()();await tick();assert.equal(requests.length,0);
agent.waiting.shift()();await latest;assert.equal(player.state,states.PLAYING);assert.equal(requests.length,1);''')
