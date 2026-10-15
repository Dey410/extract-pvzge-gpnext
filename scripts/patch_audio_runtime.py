#!/usr/bin/env python3
"""Patch the known Cocos 3.8.4 audio layout; unknown layouts fail closed."""
import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

from scripts.transcode_textures_astc import decode_uuid


class PatchError(RuntimeError):
    pass


AUDIO_EXTENSIONS = frozenset({'.aac', '.flac', '.m4a', '.mp3', '.oga', '.ogg', '.opus', '.wav'})
IDENT = r'[A-Za-z_$][\w$]*'
DOM_LOADER_PATTERN = re.compile(
    rf'(?P<head>{IDENT}\.loadNative=function\((?P<url>{IDENT})\)\{{return new Promise\(\(function\((?P<resolve>{IDENT}),(?P<reject>{IDENT})\)\{{)'
    rf'var (?P<audio>{IDENT})=document\.createElement\("audio"\).*?'
    r'(?P=audio)\.src=(?P=url)\}\)\)\}(?=,[A-Za-z_$][\w$]*\.loadOneShotAudio=function)', re.DOTALL,
)
HYBRID_SELECTOR_PATTERN = re.compile(
    rf'\(null==(?P<option>{IDENT})\?void 0:(?P=option)\.audioLoadMode\)'
    rf'!==(?P<enum>{IDENT})\.DOM_AUDIO&&(?P<support>{IDENT}\.support)'
    rf'(?=\?{IDENT}\.load(?:Native|OneShotAudio)?\((?P<url>{IDENT}))'
)
DECODE_CALLBACK_PATTERN = re.compile(
    rf'(?P<head>{IDENT}\.decodeAudioData=function\({IDENT}\)\{{var {IDENT}=this;return new Promise\(\(function\({IDENT},(?P<reject>{IDENT})\)\{{.*?'
    rf'\(function\((?P<error>{IDENT})\)\{{)console\.error\("failed to load Web Audio",(?P=error)\)', re.DOTALL,
)
WEB_LOADER_PATTERN = re.compile(
    rf'(?P<head>{IDENT}\.loadNative=function\({IDENT}\)\{{return new Promise\(\(function\({IDENT},(?P<reject>{IDENT})\)\{{)'
    rf'(?P<body>var {IDENT}={IDENT}\.getCache\(.*?{IDENT}\.send\(null\)\}}\)\)\}})', re.DOTALL,
)


ONE_SHOT_PATTERN = re.compile(
    rf'(?P<name>{IDENT})=function\(\)\{{function t\(t,e,i\)\{{this\._duration=void 0,.*?'
    rf'this\._bufferSourceNode=(?P<context>{IDENT})\.createBufferSource\(t,!1\);var n=(?P=context)\.createGain\(e\);.*?'
    rf'var e=t\.prototype;return e\.play=function\(t\)\{{.*?(?P<cache>{IDENT})\.tryReleasingCache\(e\._url\).*?'
    rf'\}},e\.stop=function\(\)\{{.*?\}},(?P<descriptors>{IDENT}\(t,\[\{{key:"onPlay".*?\]\)),t\}}\(\)', re.DOTALL,
)


def patch_one_shot(match):
    context, cache = match['context'], match['cache']
    return (
        f'{match["name"]}=function(){{function t(buffer,volume,url){{'
        'this._onPlayCb=void 0,this._onEndCb=void 0,this._url=url,this._ended=!1,this._started=!1;'
        f'this._bufferSourceNode={context}.createBufferSource(buffer,!1);'
        f'this._gainNode={context}.createGain(volume);'
        f'this._bufferSourceNode.connect(this._gainNode),{context}.connectContext(this._gainNode)}}'
        'var e=t.prototype;return e._finish=function(notify){if(this._ended)return;this._ended=!0;'
        'var source=this._bufferSourceNode;source.onended=null,source.disconnect(),source.buffer=null;'
        'this._gainNode.disconnect(),this._bufferSourceNode=null,this._gainNode=null;'
        f'{cache}.tryReleasingCache(this._url);if(notify&&this.onEnd)this.onEnd()}},'
        'e.play=function(rate){if(void 0===rate)rate=1;'
        'if(!Number.isFinite(rate)||rate<=0)throw new RangeError("invalid audio playback rate");'
        'var self=this,source=this._bufferSourceNode;source.playbackRate.value=rate;'
        'source.onended=function(){self._finish(!0)};'
        f'{context}.runContext().then(function(){{if(self._ended)return;source.start();self._started=!0;if(self.onPlay)self.onPlay()}})'
        '.catch(function(error){self._finish(!0);console.error("one-shot playback failed",error)})},'
        'e.stop=function(){if(this._ended)return;this._bufferSourceNode.onended=null;'
        'try{if(this._started)this._bufferSourceNode.stop()}finally{this._finish(!1)}},'
        f'{match["descriptors"]},t}}()'
    )


def music_audio_paths(docs_dir: Path) -> list[str]:
    """Resolve the game's music role through its Cocos resources UUID table."""
    bundle = docs_dir / 'assets' / 'resources'
    config = json.loads((bundle / 'config.json').read_text(encoding='utf-8'))
    uuids, types, paths = config['uuids'], config['types'], config['paths']
    native = {}
    for file in sorted((bundle / 'native').rglob('*')):
        if file.is_file() and file.suffix.lower() in AUDIO_EXTENSIONS:
            key = file.stem.split('.', 1)[0]
            if key in native:
                raise PatchError(f'ambiguous native AudioClip UUID: {key}')
            native[key] = file.relative_to(docs_dir).as_posix()
    music = set()
    for index, entry in paths.items():
        if not isinstance(entry, list) or len(entry) < 2 or not isinstance(entry[0], str):
            raise PatchError('unknown Cocos resource path layout')
        if entry[0].startswith('music/') and types[entry[1]] == 'cc.AudioClip':
            uuid = decode_uuid(uuids[int(index)])
            if uuid not in native:
                raise PatchError(f'music AudioClip native file is missing: {entry[0]} ({uuid})')
            music.add(native[uuid])
    if not music:
        raise PatchError('no music AudioClip UUID mappings found')
    return sorted(music)


def render_audio_selector(paths: list[str]) -> str:
    return (
        'var __pvzgeMusicPaths=new Set('
        + json.dumps(sorted(paths), separators=(',', ':'))
        + ');function __pvzgeUseWebAudio(u,o,d,w,s){'
        'var m=o&&o.audioLoadMode;if(m===d)return!1;'
        'var p=decodeURIComponent(new URL(u,document.baseURI).pathname).replace(/^\\/+/,"");'
        'var a=p.indexOf("assets/");if(a>=0)p=p.slice(a);'
        'var web=m===w||!__pvzgeMusicPaths.has(p);'
        'if(web&&!s)throw new Error("Web Audio unavailable");return web}'
    )


def check_javascript(source: str) -> None:
    result = subprocess.run(['node', '--check', '-'], input=source, text=True, capture_output=True, timeout=30)
    if result.returncode:
        raise PatchError(f'patched JavaScript did not parse: {result.stderr.strip()}')


def patch_engine_source(source: str, music_paths: list[str]) -> tuple[str, dict[str, int]]:
    def loader(match):
        a, u, ok, fail = (match.group(k) for k in ('audio', 'url', 'resolve', 'reject'))
        return (
            match['head'] + f'var {a}=document.createElement("audio"),done=!1,timer;'
            'function cleanup(){clearTimeout(timer);'
            f'{a}.removeEventListener("loadedmetadata",ready),{a}.removeEventListener("error",bad),{a}.removeEventListener("abort",bad)}}'
            f'function finish(error){{if(done)return;done=!0;cleanup();error?{fail}(error):{ok}({a})}}'
            f'function ready(){{if({a}.readyState>=1&&Number.isFinite({a}.duration))finish()}}'
            f'function bad(){{finish(new Error("load audio failed: "+{u}))}}'
            f'{a}.preload="metadata",{a}.addEventListener("loadedmetadata",ready),'
            f'{a}.addEventListener("error",bad),{a}.addEventListener("abort",bad);'
            'timer=setTimeout(bad,8e3);'
            f'try{{{a}.src={u},{a}.load(),ready()}}catch(error){{finish(error)}}'
            '}))}'
        )
    patched, dom_count = DOM_LOADER_PATTERN.subn(loader, source)
    patched, selector_count = HYBRID_SELECTOR_PATTERN.subn(
        lambda m: f'(__pvzgeUseWebAudio({m["url"]},{m["option"]},{m["enum"]}.DOM_AUDIO,{m["enum"]}.WEB_AUDIO,{m["support"]}))', patched,
    )
    patched, callback_count = DECODE_CALLBACK_PATTERN.subn(lambda m: m['head'] + f'{m["reject"]}({m["error"]})', patched)
    decode_count = 0
    def web_loader(match):
        nonlocal decode_count
        body, count = re.subn(
            rf'\.catch\(\(function\({IDENT}\)\{{{IDENT}\("loadNative error",{IDENT},{IDENT}\)\}}\)\)',
            f'.catch({match["reject"]})', match['body'],
        )
        decode_count += count
        return match['head'] + body
    patched = WEB_LOADER_PATTERN.sub(web_loader, patched)
    destroy_old = 'this._domAudio.removeEventListener("ended",this._onEnded),this._domAudio=null'
    destroy_count = patched.count(destroy_old)
    patched = patched.replace(destroy_old,
        'this._domAudio.pause(),this._domAudio.removeAttribute("src"),this._domAudio.load(),'
        + destroy_old)
    patched, one_shot_count = ONE_SHOT_PATTERN.subn(patch_one_shot, patched)
    changes = {'one_shot': one_shot_count, 'dom_loader': dom_count, 'hybrid_selector': selector_count,
               'decode_callback': callback_count, 'decode_rejection': decode_count, 'dom_destroy': destroy_count}
    if changes != {'one_shot': 1, 'dom_loader': 1, 'hybrid_selector': 3, 'decode_callback': 1, 'decode_rejection': 1, 'dom_destroy': 1}:
        raise PatchError(f'unexpected Cocos audio runtime layout: {changes}')
    helper = render_audio_selector(music_paths)
    strict = next((v for v in ('"use strict";', "'use strict';") if patched.startswith(v)), '')
    patched = strict + helper + patched[len(strict):]
    check_javascript(patched)
    return patched, changes


def patch_docs(docs_dir: Path, report_path: Path) -> None:
    settings = json.loads((docs_dir / 'src' / 'settings.json').read_text(encoding='utf-8'))
    if settings.get('CocosEngine') != '3.8.4':
        raise PatchError('only the verified Cocos 3.8.4 audio layout is supported')
    music = music_audio_paths(docs_dir)
    candidates = []
    for path in sorted((docs_dir / 'cocos-js').glob('_virtual_cc-*.js')):
        source = path.read_bytes()
        if b'document.createElement("audio")' in source and b'.DOM_AUDIO' in source:
            candidates.append(path)
        del source
    if len(candidates) != 1:
        raise PatchError(f'expected one Cocos audio runtime, found {len(candidates)}')
    engine = candidates[0]
    before = engine.read_bytes()
    patched, changes = patch_engine_source(before.decode('utf-8'), music)
    engine.write_text(patched, encoding='utf-8', newline='')
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps({
        'policy': 'explicit mode, otherwise resources music UUIDs DOM / effects WebAudio',
        'engine': engine.relative_to(docs_dir).as_posix(), 'changes': changes,
        'engineSha256Before': hashlib.sha256(before).hexdigest(),
        'engineSha256After': hashlib.sha256(patched.encode()).hexdigest(),
        'musicPaths': music,
    }, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('docs_dir', type=Path)
    parser.add_argument('report_path', type=Path)
    args = parser.parse_args()
    try:
        patch_docs(args.docs_dir, args.report_path)
    except (PatchError, OSError, ValueError, KeyError, IndexError) as error:
        print(f'error: {error}')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
