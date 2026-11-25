#!/usr/bin/env python3
"""Swap the game's <audio> pool for the Web Audio engine; unknown layouts fail closed."""
import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path, PurePosixPath


class PatchError(RuntimeError):
    pass


AUDIO_CONTEXT_SAMPLE_RATE = 32000
AUDIO_CACHE_BYTES = 32 * 1024 * 1024
# Clips at least this long stream through a media element instead of being
# decoded whole: a 3-minute stereo track is about 45 MB of PCM at 32 kHz.
STREAM_MIN_SECONDS = 20.0
# MPEG audio Layer III tables, indexed by the header's version bits (3 = MPEG-1,
# 2 = MPEG-2, 0 = MPEG-2.5).
MP3_BITRATES_KBPS = {
    3: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
MP3_SAMPLE_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}
ENGINE_SOURCE = (Path(__file__).parent / 'audio_engine.js').read_text(encoding='utf-8')
IDENT = r'[A-Za-z_$][\w$]*'
# The commands TauriAudioAPI sends to window.audioEngine; audio_engine.js
# implements the methods behind exactly these.
AUDIO_COMMANDS = frozenset({
    'audio_play', 'audio_play_fire_and_forget', 'audio_get_duration', 'audio_get_position', 'audio_set_volume',
    'audio_set_playing', 'audio_seek', 'audio_fade_out', 'audio_set_restart_before_end', 'audio_destroy',
    'audio_cleanup', 'audio_list',
})
AUDIO_API_PATTERN = re.compile(r'System\.register\("chunks:///_virtual/TauriAudioAPI\.ts".*?(?=System\.register\(|\Z)', re.DOTALL)
# 0.16.15 appends more startup calls after the assignment (`window.audioEngine=n,ue(F())}`),
# so the match ends at the assignment and only requires the statement to end there.
POOL_PATTERN = re.compile(
    rf'let (?P<engine>{IDENT})=new (?P<pool>{IDENT})(?P<args>\(await {IDENT}\.json\(\),{IDENT}\);'
    rf'await (?P=engine)\.init\(\),window\.audioEngine=(?P=engine))(?=[,}}])'
)


def audio_manifest(docs_dir: Path) -> dict[str, str]:
    manifest = json.loads((docs_dir / 'audio' / 'manifest.json').read_text(encoding='utf-8'))
    if not isinstance(manifest, dict) or not manifest:
        raise PatchError('audio manifest is not a non-empty object')
    # 0.16.31+ lists several variants for some clips; the game plays one at random.
    for name, entry in manifest.items():
        paths = entry if isinstance(entry, list) and entry else [entry]
        for path in paths:
            parts = PurePosixPath(path).parts if isinstance(path, str) else ()
            if not parts or path.startswith('/') or '..' in parts or not (docs_dir / 'audio' / path).is_file():
                raise PatchError(f'audio manifest entry is unsafe or missing: {name} -> {entry!r}')
    return manifest


def _mp3_frame(data: bytes, offset: int) -> tuple[int, int, int, int, bool] | None:
    """(version, bitrate kbps, sample rate, frame length, mono) of a Layer III header at offset."""
    if offset + 4 > len(data) or data[offset] != 0xFF or data[offset + 1] & 0xE0 != 0xE0:
        return None
    version, layer = (data[offset + 1] >> 3) & 3, (data[offset + 1] >> 1) & 3
    bitrate_index, rate_index = data[offset + 2] >> 4, (data[offset + 2] >> 2) & 3
    if version == 1 or layer != 1 or bitrate_index in (0, 15) or rate_index == 3:
        return None
    bitrate = MP3_BITRATES_KBPS[3 if version == 3 else 2][bitrate_index]
    rate = MP3_SAMPLE_RATES[version][rate_index]
    length = (144 if version == 3 else 72) * bitrate * 1000 // rate + ((data[offset + 2] >> 1) & 1)
    return version, bitrate, rate, length, data[offset + 3] >> 6 == 3


def mp3_duration(path: Path) -> float:
    """Seconds of an MP3: a Xing/Info or VBRI frame count when present, else the CBR byte length."""
    data = path.read_bytes()
    start = 0
    if data[:3] == b'ID3' and len(data) >= 10:
        start = 10 + ((data[6] & 0x7F) << 21 | (data[7] & 0x7F) << 14 | (data[8] & 0x7F) << 7 | data[9] & 0x7F)
        if data[5] & 0x10:
            start += 10
    # The first header must be followed by another, so a stray 0xFFE byte is not taken for a frame.
    offset = start
    while offset < len(data):
        frame = _mp3_frame(data, offset)
        if frame and (offset + frame[3] >= len(data) or _mp3_frame(data, offset + frame[3])):
            break
        offset += 1
    else:
        raise PatchError(f'audio clip is not an MP3 Layer III stream: {path}')
    version, bitrate, rate, _, mono = frame
    samples = 1152 if version == 3 else 576
    side_info = (17 if mono else 32) if version == 3 else (9 if mono else 17)
    xing = offset + 4 + side_info
    if data[xing:xing + 4] in (b'Xing', b'Info') and data[xing + 7] & 1:
        return int.from_bytes(data[xing + 8:xing + 12], 'big') * samples / rate
    vbri = offset + 36
    if data[vbri:vbri + 4] == b'VBRI':
        return int.from_bytes(data[vbri + 14:vbri + 18], 'big') * samples / rate
    end = len(data) - 128 if data[-128:-125] == b'TAG' else len(data)
    return (end - offset) * 8 / (bitrate * 1000)


def stream_paths(docs_dir: Path, manifest: dict) -> list[str]:
    paths = {path for entry in manifest.values() for path in (entry if isinstance(entry, list) else [entry])}
    return sorted(path for path in paths if mp3_duration(docs_dir / 'audio' / path) >= STREAM_MIN_SECONDS)


def check_cocos_has_no_audio(docs_dir: Path) -> None:
    for config in sorted((docs_dir / 'assets').glob('*/config.json')):
        if 'cc.AudioClip' in json.loads(config.read_text(encoding='utf-8')).get('types', []):
            raise PatchError(f'Cocos still loads AudioClips: {config.relative_to(docs_dir).as_posix()}')


def check_audio_commands(docs_dir: Path) -> None:
    match = AUDIO_API_PATTERN.search((docs_dir / 'assets' / 'main' / 'index.js').read_text(encoding='utf-8'))
    commands = set(re.findall(r'"(audio_[a-z_]+)"', match.group(0))) if match else set()
    if commands != AUDIO_COMMANDS:
        raise PatchError(f'unexpected TauriAudioAPI commands: {sorted(commands ^ AUDIO_COMMANDS)}')


def check_module(source: str) -> None:
    result = subprocess.run(['node', '--input-type=module', '--check', '-'], input=source, text=True,
                            capture_output=True, timeout=30)
    if result.returncode:
        raise PatchError(f'patched JavaScript did not parse: {result.stderr.strip()}')


def patch_entry_source(source: str, streams: list[str] = ()) -> str:
    patched, count = POOL_PATTERN.subn(lambda m: f'let {m["engine"]}=new __pvzgeAudioEngine{m["args"]}', source)
    if count != 1:
        raise PatchError(f'expected one window.audioEngine pool construction, found {count}')
    engine = (ENGINE_SOURCE + f'var __pvzgeAudioEngine=__pvzgeCreateAudioEngine('
              f'{AUDIO_CONTEXT_SAMPLE_RATE},{AUDIO_CACHE_BYTES},'
              f'{json.dumps(list(streams), ensure_ascii=False, separators=(",", ":"))});\n')
    patched = engine + patched
    check_module(patched)
    return patched


def patch_docs(docs_dir: Path, report_path: Path) -> None:
    settings = json.loads((docs_dir / 'src' / 'settings.json').read_text(encoding='utf-8'))
    if settings.get('CocosEngine') != '3.8.4':
        raise PatchError('only the verified Cocos 3.8.4 layout is supported')
    manifest = audio_manifest(docs_dir)
    streams = stream_paths(docs_dir, manifest)
    check_cocos_has_no_audio(docs_dir)
    check_audio_commands(docs_dir)
    candidates = [path for path in sorted((docs_dir / 'assets').glob('*.js'))
                  if b'window.audioEngine=' in path.read_bytes()]
    if len(candidates) != 1:
        raise PatchError(f'expected one module creating window.audioEngine, found {len(candidates)}')
    entry = candidates[0]
    before = entry.read_bytes()
    patched = patch_entry_source(before.decode('utf-8'), streams)
    entry.write_text(patched, encoding='utf-8', newline='')
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps({
        'policy': 'window.audioEngine is a Web Audio engine with the <audio> pool interface: short clips decode on '
                  'first play and are shared by URL, long clips and music pack clips stream through a media '
                  'element, volume and fades use gain',
        'entry': entry.relative_to(docs_dir).as_posix(),
        'entrySha256Before': hashlib.sha256(before).hexdigest(),
        'entrySha256After': hashlib.sha256(patched.encode()).hexdigest(),
        'audioContextSampleRate': AUDIO_CONTEXT_SAMPLE_RATE,
        'audioCacheBytes': AUDIO_CACHE_BYTES,
        'clips': len(manifest),
        'streamMinSeconds': STREAM_MIN_SECONDS,
        'streamedFiles': streams,
    }, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('docs_dir', type=Path)
    parser.add_argument('report_path', type=Path)
    args = parser.parse_args()
    try:
        patch_docs(args.docs_dir, args.report_path)
    except (PatchError, OSError, ValueError, KeyError) as error:
        print(f'error: {error}')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
