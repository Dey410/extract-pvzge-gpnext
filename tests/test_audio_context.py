import subprocess
import unittest
from pathlib import Path

SOURCE = (Path(__file__).parents[1] / 'scripts' / 'audio_context.js').read_text(encoding='utf-8')
HARNESS = '''const assert=require('node:assert/strict');
const calls=[],warnings=[],timers=[];
console.warn=(message)=>warnings.push(message);
const setTimeout=(fn)=>timers.push(fn);
const context={state:'running',currentTime:1,
  suspend(){calls.push('suspend');this.state='suspended';return Promise.resolve()},
  resume(){calls.push('resume');this.state='running';return Promise.resolve()}};
const handlers={};
const bus={on(type,fn){handlers[type]=fn}};
SOURCE
(async()=>{SCENARIO})().catch(error=>{console.log(error.stack||String(error));process.exitCode=1});'''


class AudioContextRestartTests(unittest.TestCase):
    def run_js(self, scenario):
        program = HARNESS.replace('SOURCE', SOURCE).replace('SCENARIO', scenario)
        result = subprocess.run(['node', '-'], input=program, text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_hide_suspends_and_show_cycles_suspend_and_resume(self):
        self.run_js('''__pvzgeRestartAudioContextOnShow({agent:{_context:context},bus,busEvents:{EVENT_HIDE:'hide',EVENT_SHOW:'show'}});
handlers.hide();await Promise.resolve();assert.deepEqual(calls,['suspend']);
context.state='interrupted';handlers.show();await new Promise(r=>setImmediate(r));
assert.deepEqual(calls,['suspend','suspend','resume']);assert.equal(warnings[0],'[pvzge-audio] show state=interrupted time=1.000');
context.currentTime=1.5;timers.shift()();assert.equal(warnings[1],'[pvzge-audio] resumed state=running time=1.500');
handlers.show();await new Promise(r=>setImmediate(r));timers.shift()();
assert.equal(warnings[3],'[pvzge-audio] resumed without progress state=running time=1.500');''')

    def test_without_web_audio_nothing_is_registered(self):
        self.run_js('''__pvzgeRestartAudioContextOnShow({agent:undefined,bus,busEvents:{EVENT_HIDE:'hide',EVENT_SHOW:'show'}});
assert.deepEqual(Object.keys(handlers),[]);''')
