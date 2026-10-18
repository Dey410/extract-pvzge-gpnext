import subprocess
import unittest
from pathlib import Path

SOURCE = (Path(__file__).parents[1] / 'scripts' / 'audio_context.js').read_text(encoding='utf-8')
HARNESS = '''const assert=require('node:assert/strict');
const warnings=[],timers=[];
console.warn=(message)=>warnings.push(message);
const setTimeout=(fn)=>timers.push(fn);
const context={state:'running',currentTime:1};
const handlers={};
const bus={on(type,fn){handlers[type]=fn}};
SOURCE
(async()=>{SCENARIO})().catch(error=>{console.log(error.stack||String(error));process.exitCode=1});'''


class AudioContextTests(unittest.TestCase):
    def run_js(self, scenario, prelude=''):
        program = prelude + HARNESS.replace('SOURCE', SOURCE).replace('SCENARIO', scenario)
        result = subprocess.run(['node', '-'], input=program, text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_show_reports_state_and_whether_the_clock_advances(self):
        self.run_js('''__pvzgeReportAudioContextOnShow({agent:{_context:context},bus,busEvents:{EVENT_SHOW:'show'}});
handlers.show();assert.equal(warnings[0],'[pvzge-audio] show state=running time=1.000');
context.currentTime=2;timers.shift()();assert.equal(warnings[1],'[pvzge-audio] advancing state=running time=2.000');
handlers.show();timers.shift()();assert.equal(warnings[3],'[pvzge-audio] stalled state=running time=2.000');''')

    def test_without_web_audio_nothing_is_registered(self):
        self.run_js('''__pvzgeReportAudioContextOnShow({agent:undefined,bus,busEvents:{EVENT_SHOW:'show'}});
assert.deepEqual(Object.keys(handlers),[]);''')
