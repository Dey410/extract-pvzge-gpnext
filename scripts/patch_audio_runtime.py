#!/usr/bin/env python3

import argparse
import hashlib
import re
from pathlib import Path


class PatchError(RuntimeError):
    pass


DOM_LOADER_PATTERN = re.compile(
    r"(?P<head>[A-Za-z_$][\w$]*\.loadNative=function\("
    r"(?P<url>[A-Za-z_$][\w$]*)\)\{return new Promise\(\(function\("
    r"(?P<resolve>[A-Za-z_$][\w$]*),(?P<reject>[A-Za-z_$][\w$]*)\)\{)"
    r"var (?P<audio>[A-Za-z_$][\w$]*)=document\.createElement\(\"audio\"\).*?"
    r"(?P=audio)\.src=(?P=url)\}\)\)\}"
    r"(?=,[A-Za-z_$][\w$]*\.loadOneShotAudio=function)",
    re.DOTALL,
)

ONESHOT_ROLE_PATTERN = re.compile(
    r"(?P<head>[A-Za-z_$][\w$]*\.loadOneShotAudio=function\([^)]*\)"
    r"\{return new Promise\(\(function\([^)]*\)\{)"
    r"(?P<body>.*?)"
    r"(?P<call>[A-Za-z_$][\w$]*\.loadNative\()(?P<arg>[A-Za-z_$][\w$]*)(?P<close>\))",
    re.DOTALL,
)

DESTROY_PATTERN = re.compile(
    r"(?P<prefix>[A-Za-z_$][\w$]*\.destroy=function\(\)\{[^}]*?"
    r"this\._domAudio\.removeEventListener\(\"ended\",this\._onEnded\),)"
    r"this\._domAudio=null"
)

MAX_CHANNEL_PATTERN = re.compile(
    r"(?P<owner>[A-Za-z_$][\w$]*)\.maxAudioChannel=\d+"
)

UPGRADE_LOADER_PATTERN = re.compile(
    r"(?P<head>[A-Za-z_$][\w$]*\.loadNative=function\("
    r"(?P<url>[A-Za-z_$][\w$]*)\)\{return new Promise\(\(function\("
    r"(?P<resolve>[A-Za-z_$][\w$]*),(?P<reject>[A-Za-z_$][\w$]*)\)\{)"
    r"var (?P<audio>[A-Za-z_$][\w$]*)=document\.createElement\(\"audio\"\);"
    r"(?P=audio)\.preload=\"none\",(?P=audio)\.__pvzgeLazySrc=(?P=url),"
    r"(?P=resolve)\((?P=audio)\)\}\)\}"
)

PLAY_HOOK_PATTERN = re.compile(
    r"function (?P<function>[A-Za-z_$][\w$]*)\((?P<audio>[A-Za-z_$][\w$]*)\)"
    r"\{return new Promise\(\(function\((?P<resolve>[A-Za-z_$][\w$]*)\)\{"
    r"var (?P<result>[A-Za-z_$][\w$]*)=(?P=audio)\.play\(\);"
)

FORCE_DOM_PATTERN = re.compile(
    r"!==([A-Za-z_$][\w$]*)\.DOM_AUDIO&&([A-Za-z_$][\w$]*)\.support\?"
)

GAME_DEFAULT_PATTERN = re.compile(
    r"(\.loadmode=[A-Za-z_$][\w$]*\.AudioType\.)WEB_AUDIO"
)


def patch_engine_source(source: str) -> tuple[str, dict[str, int]]:
    def replace_loader(match: re.Match[str]) -> str:
        audio = match.group("audio")
        url = match.group("url")
        resolve = match.group("resolve")
        return (
            match.group("head")
            + f"var {audio}=window.__gardendlessNativeAudio"
            + "&&window.__gardendlessNativeAudio.createNativeAudioHandle"
            + f"?window.__gardendlessNativeAudio.createNativeAudioHandle({url},"
            + '{role:(arguments.length>1&&arguments[1]&&arguments[1].role)||"continuous"})'
            + f':function(){{var n=document.createElement("audio");'
            + f'n.preload="none",n.__pvzgeLazySrc={url};return n}}();'
            + f"{resolve}({audio})"
            + "}))}"
        )

    def replace_oneshot(match: re.Match[str]) -> str:
        return (
            match.group("head")
            + match.group("body")
            + match.group("call")
            + match.group("arg")
            + ',{role:"oneShot"}'
            + match.group("close")
        )

    patched, loader_count = DOM_LOADER_PATTERN.subn(replace_loader, source)
    patched, oneshot_count = ONESHOT_ROLE_PATTERN.subn(replace_oneshot, patched)

    def replace_play_hook(match: re.Match[str]) -> str:
        audio = match.group("audio")
        return (
            match.group(0)[: match.group(0).rfind("var ")]
            + f"{audio}.src||!{audio}.__pvzgeLazySrc||"
            + f"({audio}.src={audio}.__pvzgeLazySrc);"
            + f"var {match.group('result')}={audio}.play();"
        )

    patched, play_hook_count = PLAY_HOOK_PATTERN.subn(replace_play_hook, patched)
    patched, force_dom_count = FORCE_DOM_PATTERN.subn(
        r"!==\1.DOM_AUDIO&&!1?", patched
    )
    patched, destroy_count = DESTROY_PATTERN.subn(
        r"\g<prefix>"
        "this._domAudio&&this._domAudio.release&&this._domAudio.release(),"
        "this._domAudio=null",
        patched,
    )
    patched, max_channel_count = MAX_CHANNEL_PATTERN.subn(
        r"\g<owner>.maxAudioChannel=window.__gardendlessHostConfig"
        "&&window.__gardendlessHostConfig.audioVoicePoolSize||48",
        patched,
    )

    changes = {
        "dom_loader": loader_count,
        "play_hook": play_hook_count,
        "force_dom": force_dom_count,
        "oneshot_role": oneshot_count,
        "destroy_release": destroy_count,
        "max_channel": max_channel_count,
    }

    if changes != {
        "dom_loader": 1,
        "play_hook": 1,
        "force_dom": 3,
        "oneshot_role": 2,
        "destroy_release": 1,
        "max_channel": 1,
    }:
        raise PatchError(
            "unexpected Cocos audio runtime layout: "
            "expected dom_loader=1, play_hook=1, force_dom=3, "
            f"oneshot_role=2, destroy_release=1, max_channel=1; got {changes}"
        )

    return patched, changes


def patch_game_source(source: str) -> tuple[str, int]:
    if "SoundRescourses.ts" not in source:
        raise PatchError("SoundRescourses.ts marker is missing")

    patched, changes = GAME_DEFAULT_PATTERN.subn(r"\1DOM_AUDIO", source)
    if changes != 1:
        raise PatchError(
            "unexpected SoundRescourses audio default layout: "
            f"expected 1 WEB_AUDIO default; got {changes}"
        )
    return patched, changes


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def select_unique_javascript(
    candidates: list[Path], required_markers: tuple[bytes, ...], description: str
) -> Path:
    matches = []
    for path in candidates:
        if not path.is_file():
            continue
        data = path.read_bytes()
        if all(marker in data for marker in required_markers):
            matches.append(path)

    if len(matches) != 1:
        rendered = ", ".join(str(path) for path in matches) or "none"
        raise PatchError(
            f"expected exactly one {description} JavaScript file; "
            f"found {len(matches)}: {rendered}"
        )
    return matches[0]


def patch_docs(docs_dir: Path, report_path: Path) -> None:
    if not docs_dir.is_dir():
        raise PatchError(f"docs directory does not exist: {docs_dir}")

    engine_path = select_unique_javascript(
        sorted((docs_dir / "cocos-js").glob("_virtual_cc-*.js")),
        (b'document.createElement("audio")', b'"canplaythrough"', b".DOM_AUDIO"),
        "Cocos audio runtime",
    )
    game_path = select_unique_javascript(
        [docs_dir / "assets" / "main" / "index.js"],
        (b"SoundRescourses.ts", b".AudioType.WEB_AUDIO"),
        "SoundRescourses",
    )

    engine_before = engine_path.read_bytes()
    game_before = game_path.read_bytes()

    engine_after_text, engine_changes = patch_engine_source(
        engine_before.decode("utf-8")
    )
    game_after_text, game_changes = patch_game_source(game_before.decode("utf-8"))

    engine_after = engine_after_text.encode("utf-8")
    game_after = game_after_text.encode("utf-8")

    if engine_after == engine_before or game_after == game_before:
        raise PatchError("runtime patch did not modify both target files")

    engine_path.write_bytes(engine_after)
    game_path.write_bytes(game_after)

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = "\n".join(
        [
            "PvZGE native audio facade runtime patch",
            "",
            f"Engine file: {engine_path.relative_to(docs_dir)}",
            f"Engine SHA-256 before: {sha256(engine_before)}",
            f"Engine SHA-256 after:  {sha256(engine_after)}",
            f"DOM loader replacements: {engine_changes['dom_loader']}",
            f"Play hook replacements: {engine_changes['play_hook']}",
            f"Forced DOM selectors: {engine_changes['force_dom']}",
            f"One-shot role replacements: {engine_changes['oneshot_role']}",
            f"Destroy release replacements: {engine_changes['destroy_release']}",
            f"Max audio channel replacements: {engine_changes['max_channel']}",
            "",
            f"Game file: {game_path.relative_to(docs_dir)}",
            f"Game SHA-256 before: {sha256(game_before)}",
            f"Game SHA-256 after:  {sha256(game_after)}",
            f"DOM audio defaults: {game_changes}",
            "",
            "Behavior:",
            "  The DOM loader returns a native audio facade when available.",
            "  One-shot loads are tagged role=oneShot; AudioSource loads default",
            "  to role=continuous. A real <audio> element is the fallback when",
            "  the facade factory is absent (Android/OHOS or old game package).",
            "  Destroy releases the native voice, and maxAudioChannel is bound",
            "  to the host audioVoicePoolSize configuration.",
            "  Web Audio selection is disabled so eager PCM decoding cannot occur.",
            "",
        ]
    )
    report_path.write_text(report, encoding="utf-8", newline="\n")
    print(report, end="")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Patch extracted PvZGE JavaScript for lazy DOM audio loading."
    )
    parser.add_argument("docs_dir", nargs="?", default="docs", type=Path)
    parser.add_argument(
        "report_path",
        nargs="?",
        default=Path("reports/audio-runtime-patch.txt"),
        type=Path,
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        patch_docs(args.docs_dir, args.report_path)
    except PatchError as error:
        print(f"error: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
