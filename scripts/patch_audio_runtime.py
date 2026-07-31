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
            + f'var {audio}=document.createElement("audio");'
            + f'{audio}.preload="none",'
            + f"{audio}.__pvzgeLazySrc={url},"
            + f"{resolve}({audio})"
            + "}))}"
        )

    patched, loader_count = DOM_LOADER_PATTERN.subn(replace_loader, source)

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

    changes = {
        "dom_loader": loader_count,
        "play_hook": play_hook_count,
        "force_dom": force_dom_count,
    }

    if changes != {"dom_loader": 1, "play_hook": 1, "force_dom": 3}:
        raise PatchError(
            "unexpected Cocos audio runtime layout: "
            f"expected dom_loader=1, play_hook=1, force_dom=3; got {changes}"
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
            "PvZGE lazy DOM audio runtime patch",
            "",
            f"Engine file: {engine_path.relative_to(docs_dir)}",
            f"Engine SHA-256 before: {sha256(engine_before)}",
            f"Engine SHA-256 after:  {sha256(engine_after)}",
            f"DOM loader replacements: {engine_changes['dom_loader']}",
            f"Play hook replacements: {engine_changes['play_hook']}",
            f"Forced DOM selectors: {engine_changes['force_dom']}",
            "",
            f"Game file: {game_path.relative_to(docs_dir)}",
            f"Game SHA-256 before: {sha256(game_before)}",
            f"Game SHA-256 after:  {sha256(game_after)}",
            f"DOM audio defaults: {game_changes}",
            "",
            "Behavior:",
            '  HTMLAudioElement is created with preload="none".',
            "  Its URL is assigned only immediately before the first play().",
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
