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


AUDIO_CONTEXT_SAMPLE_RATE = 32000
MUSIC_PCM_CACHE_BYTES = 32 * 1024 * 1024
MUSIC_PLAYER_SOURCE = (Path(__file__).parent / 'music_player.js').read_text(encoding='utf-8')
AUDIO_CONTEXT_SOURCE = (Path(__file__).parent / 'audio_context.js').read_text(encoding='utf-8')
AUDIO_EXTENSIONS = frozenset({'.aac', '.flac', '.m4a', '.mp3', '.oga', '.ogg', '.opus', '.wav'})
IDENT = r'[A-Za-z_$][\w$]*'
MUSIC_SELECTOR_PATTERN = re.compile(
    rf'\(null==(?P<option>{IDENT})\?void 0:(?P=option)\.audioLoadMode\)'
    rf'!==(?P<enum>{IDENT})\.DOM_AUDIO&&(?P<support>{IDENT}\.support)'
    rf'\?(?P<web>{IDENT})\.(?P<method>loadNative|loadOneShotAudio|load)\((?P<url>{IDENT})'
)
WEB_PLAYER_PATTERN = re.compile(
    rf'(?P<web>{IDENT})=\({IDENT}\(\((?P<inner>{IDENT})=function\(\)\{{function t\(t,e\)\{{'
    r'this\._src=void 0,this\._audioBuffer=void 0,.*?'
    r'Object\.getOwnPropertyDescriptor\((?P=inner)\.prototype,"stop"\),(?P=inner)\.prototype\),(?P=inner)\)'
    rf'(?=,{IDENT}=function\(\)\{{function t\(t\)\{{this\._audio=void 0)', re.DOTALL,
)
WEB_PLAYER_DEPS = {
    'agent': re.compile(rf'this\._gainNode=(?P<name>{IDENT})\.createGain\(\)'),
    'states': re.compile(rf'this\._state=(?P<name>{IDENT})\.INIT'),
    'events': re.compile(rf'\.emit\((?P<name>{IDENT})\.ENDED\)'),
    'types': re.compile(rf'return (?P<name>{IDENT})\.WEB_AUDIO'),
    'bus': re.compile(rf'(?P<name>{IDENT})\.on\({IDENT}\.EVENT_PAUSE,this\._onInterruptedBegin,this\)'),
    'busEvents': re.compile(rf'{IDENT}\.on\((?P<name>{IDENT})\.EVENT_PAUSE,this\._onInterruptedBegin,this\)'),
}
CONTEXT_PATTERN = re.compile(
    r'this\._context=new\(window\.AudioContext\|\|window\.webkitAudioContext\|\|window\.mozAudioContext\)'
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


def render_audio_runtime(paths: list[str]) -> str:
    return (
        'var __pvzgeMusicPaths=new Set('
        + json.dumps(sorted(paths), separators=(',', ':'))
        + ');function __pvzgeIsMusic(u){'
        'var p=decodeURIComponent(new URL(u,document.baseURI).pathname).replace(/^\\/+/,"");'
        'var a=p.indexOf("assets/");if(a>=0)p=p.slice(a);return __pvzgeMusicPaths.has(p)}'
        'function __pvzgeDecodesOnPlay(u,o,d){return __pvzgeIsMusic(u)||(null==o?void 0:o.audioLoadMode)===d}'
        'function __pvzgeUseWebAudio(u,o,d,s,load){'
        'if(load?__pvzgeDecodesOnPlay(u,o,d):__pvzgeIsMusic(u)){'
        'if(!s)throw new Error("Web Audio unavailable");return!0}'
        'return(null==o?void 0:o.audioLoadMode)!==d&&s}\n'
        + MUSIC_PLAYER_SOURCE + AUDIO_CONTEXT_SOURCE
    )


def web_player_deps(source: str) -> dict[str, str]:
    deps = {}
    for key, pattern in WEB_PLAYER_DEPS.items():
        names = {match['name'] for match in pattern.finditer(source)}
        if len(names) != 1:
            raise PatchError(f'unexpected Cocos WebAudio player dependency {key}: {sorted(names)}')
        deps[key] = names.pop()
    return deps


def check_javascript(source: str) -> None:
    result = subprocess.run(['node', '--check', '-'], input=source, text=True, capture_output=True, timeout=30)
    if result.returncode:
        raise PatchError(f'patched JavaScript did not parse: {result.stderr.strip()}')


def patch_engine_source(source: str, music_paths: list[str]) -> tuple[str, dict[str, int]]:
    web_players = set()
    def selector(match):
        web_players.add(match['web'])
        url, option, dom = match['url'], match['option'], f'{match["enum"]}.DOM_AUDIO'
        load = match['method'] == 'load'
        target = f'(__pvzgeDecodesOnPlay({url},{option},{dom})?__pvzgeMusic:{match["web"]})' if load else match['web']
        return (
            f'(__pvzgeUseWebAudio({url},{option},{dom},{match["support"]},{"!0" if load else "!1"}))'
            f'?{target}.{match["method"]}({url}'
        )
    patched, selector_count = MUSIC_SELECTOR_PATTERN.subn(selector, source)
    def music_player(match):
        web_players.add(match['web'])
        names = web_player_deps(match.group(0))
        deps = ','.join(f'{key}:{name}' for key, name in names.items())
        context = f'{{agent:{names["agent"]},bus:{names["bus"]},busEvents:{names["busEvents"]}}}'
        # The engine's context agent and game exist here, so the show report is registered during module setup.
        return match.group(0) + (
            f',__pvzgeMusic=(__pvzgeReportAudioContextOnShow({context}),'
            f'__pvzgeCreateMusicPlayer(function(){{return{{{deps}}}}},{MUSIC_PCM_CACHE_BYTES}))'
        )
    patched, player_count = WEB_PLAYER_PATTERN.subn(music_player, patched)
    if len(web_players) > 1:
        raise PatchError(f'audio selectors and music player name different WebAudio players: {sorted(web_players)}')
    patched, context_count = CONTEXT_PATTERN.subn(lambda m: m.group(0) + f'({{sampleRate:{AUDIO_CONTEXT_SAMPLE_RATE}}})', patched)
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
    patched, one_shot_count = ONE_SHOT_PATTERN.subn(patch_one_shot, patched)
    changes = {'one_shot': one_shot_count, 'music_selector': selector_count, 'music_player': player_count,
               'context_sample_rate': context_count, 'decode_callback': callback_count, 'decode_rejection': decode_count}
    if changes != {'one_shot': 1, 'music_selector': 3, 'music_player': 1, 'context_sample_rate': 1,
                   'decode_callback': 1, 'decode_rejection': 1}:
        raise PatchError(f'unexpected Cocos audio runtime layout: {changes}')
    helper = render_audio_runtime(music_paths)
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
        'policy': 'AudioSource loads of resources music UUIDs or explicit DOM_AUDIO use the decode-on-play '
                  'WebAudio player; music one-shots use WebAudio; other audio keeps the Cocos rule; '
                  'WebAudio context at a fixed sample rate',
        'engine': engine.relative_to(docs_dir).as_posix(), 'changes': changes,
        'engineSha256Before': hashlib.sha256(before).hexdigest(),
        'engineSha256After': hashlib.sha256(patched.encode()).hexdigest(),
        'audioContextSampleRate': AUDIO_CONTEXT_SAMPLE_RATE,
        'musicPcmCacheBytes': MUSIC_PCM_CACHE_BYTES,
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
