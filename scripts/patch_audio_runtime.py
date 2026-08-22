#!/usr/bin/env python3

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class PatchError(RuntimeError):
    pass


AUDIO_THRESHOLD_SECONDS = 10.0
AUDIO_EXTENSIONS = frozenset(
    {".aac", ".flac", ".m4a", ".mp3", ".oga", ".ogg", ".opus", ".wav"}
)
DOM_AUDIO = "DOM_AUDIO"
WEB_AUDIO = "WEB_AUDIO"


@dataclass(frozen=True)
class AudioDecision:
    relative_path: str
    duration_seconds: float | None
    backend: str
    error: str | None = None


def require_ffprobe(
    which: Callable[[str], str | None] = shutil.which,
) -> str:
    ffprobe_path = which("ffprobe")
    if ffprobe_path is None:
        raise PatchError("required command is unavailable: ffprobe")
    return ffprobe_path


def probe_audio_duration(ffprobe_path: str, path: Path) -> float:
    result = subprocess.run(
        [
            ffprobe_path,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or f"ffprobe exited with {result.returncode}"
        raise RuntimeError(detail)

    rendered_duration = result.stdout.strip()
    try:
        duration = float(rendered_duration)
    except ValueError as error:
        raise RuntimeError(f"invalid duration: {rendered_duration or 'empty'}") from error

    if not math.isfinite(duration) or duration < 0:
        raise RuntimeError(f"invalid duration: {rendered_duration}")
    return duration


def classify_audio_files(
    docs_dir: Path,
    threshold_seconds: float,
    probe_duration: Callable[[Path], float],
) -> list[AudioDecision]:
    audio_paths = sorted(
        (
            path
            for path in docs_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS
        ),
        key=lambda path: path.relative_to(docs_dir).as_posix(),
    )
    decisions = []
    for path in audio_paths:
        relative_path = path.relative_to(docs_dir).as_posix()
        try:
            duration = probe_duration(path)
        except Exception as error:
            detail = str(error).strip() or error.__class__.__name__
            decisions.append(AudioDecision(relative_path, None, DOM_AUDIO, detail))
            continue

        backend = DOM_AUDIO if duration >= threshold_seconds else WEB_AUDIO
        decisions.append(AudioDecision(relative_path, duration, backend))
    return decisions


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

HYBRID_SELECTOR_PATTERN = re.compile(
    r"\(null==(?P<option>[A-Za-z_$][\w$]*)\?void 0:"
    r"(?P=option)\.audioLoadMode\)"
    r"!==[A-Za-z_$][\w$]*\.DOM_AUDIO&&"
    r"(?P<support>[A-Za-z_$][\w$]*\.support)"
    r"(?=\?[A-Za-z_$][\w$]*\.load(?:Native|OneShotAudio)?\("
    r"(?P<url>[A-Za-z_$][\w$]*))"
)


def render_dom_audio_matcher(dom_audio_paths: list[str]) -> str:
    manifest = json.dumps(
        sorted(dom_audio_paths), ensure_ascii=True, separators=(",", ":")
    )
    return (
        f"var __pvzgeDomAudioPaths={manifest};"
        "function __pvzgeUseDomAudio(t){var e;"
        "try{e=decodeURIComponent(new URL(t,document.baseURI).pathname)}"
        'catch(n){e=String(t).split("?")[0].split("#")[0]}'
        "for(var n=0;n<__pvzgeDomAudioPaths.length;n++){"
        "var r=__pvzgeDomAudioPaths[n];"
        'if(e===r||e.endsWith("/"+r))return!0}'
        "return!1}"
    )


def inject_runtime_helper(source: str, helper: str) -> str:
    for directive in ('"use strict";', "'use strict';"):
        if source.startswith(directive):
            return directive + helper + source[len(directive) :]
    return helper + source


def patch_engine_source(
    source: str, dom_audio_paths: list[str]
) -> tuple[str, dict[str, int]]:
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

    def replace_selector(match: re.Match[str]) -> str:
        return (
            match.group("support")
            + f"&&!__pvzgeUseDomAudio({match.group('url')})"
        )

    patched, hybrid_selector_count = HYBRID_SELECTOR_PATTERN.subn(
        replace_selector, patched
    )

    changes = {
        "dom_loader": loader_count,
        "play_hook": play_hook_count,
        "hybrid_selector": hybrid_selector_count,
    }

    if changes != {"dom_loader": 1, "play_hook": 1, "hybrid_selector": 3}:
        raise PatchError(
            "unexpected Cocos audio runtime layout: "
            "expected dom_loader=1, play_hook=1, hybrid_selector=3; "
            f"got {changes}"
        )

    helper = render_dom_audio_matcher(dom_audio_paths)
    return inject_runtime_helper(patched, helper), changes


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


def patch_docs(
    docs_dir: Path,
    report_path: Path,
    *,
    ffprobe_path: str | None = None,
    duration_probe: Callable[[str, Path], float] = probe_audio_duration,
) -> None:
    if not docs_dir.is_dir():
        raise PatchError(f"docs directory does not exist: {docs_dir}")

    resolved_ffprobe_path = ffprobe_path or require_ffprobe()
    decisions = classify_audio_files(
        docs_dir,
        AUDIO_THRESHOLD_SECONDS,
        lambda path: duration_probe(resolved_ffprobe_path, path),
    )
    dom_audio_paths = [
        decision.relative_path
        for decision in decisions
        if decision.backend == DOM_AUDIO
    ]

    engine_path = select_unique_javascript(
        sorted((docs_dir / "cocos-js").glob("_virtual_cc-*.js")),
        (b'document.createElement("audio")', b'"canplaythrough"', b".DOM_AUDIO"),
        "Cocos audio runtime",
    )
    engine_before = engine_path.read_bytes()

    engine_after_text, engine_changes = patch_engine_source(
        engine_before.decode("utf-8"), dom_audio_paths
    )

    engine_after = engine_after_text.encode("utf-8")

    if engine_after == engine_before:
        raise PatchError("runtime patch did not modify the Cocos engine file")

    engine_path.write_bytes(engine_after)

    report_path.parent.mkdir(parents=True, exist_ok=True)
    dom_count = sum(decision.backend == DOM_AUDIO for decision in decisions)
    web_audio_count = sum(decision.backend == WEB_AUDIO for decision in decisions)
    probe_failure_count = sum(decision.error is not None for decision in decisions)
    decision_lines = []
    for decision in decisions:
        duration = (
            "unknown"
            if decision.duration_seconds is None
            else f"{decision.duration_seconds:.3f}"
        )
        line = f"{decision.backend} | {duration} | {decision.relative_path}"
        if decision.error is not None:
            detail = " ".join(decision.error.splitlines())
            line += f" | probe error: {detail}"
        decision_lines.append(line)

    report = "\n".join(
        [
            "PvZGE hybrid audio runtime patch",
            "",
            f"Threshold seconds: {AUDIO_THRESHOLD_SECONDS:.3f}",
            f"Audio files: {len(decisions)}",
            f"DOM_AUDIO files: {dom_count}",
            f"WEB_AUDIO files: {web_audio_count}",
            f"Probe failures: {probe_failure_count}",
            "",
            f"Engine file: {engine_path.relative_to(docs_dir)}",
            f"Engine SHA-256 before: {sha256(engine_before)}",
            f"Engine SHA-256 after:  {sha256(engine_after)}",
            f"DOM loader replacements: {engine_changes['dom_loader']}",
            f"Play hook replacements: {engine_changes['play_hook']}",
            f"Hybrid selector replacements: {engine_changes['hybrid_selector']}",
            "",
            "Behavior:",
            "  Audio at or above the threshold uses lazy DOM Audio.",
            "  Audio below the threshold uses the original Web Audio loader.",
            "  Duration probe failures conservatively use lazy DOM Audio.",
            '  DOM HTMLAudioElement instances use preload="none".',
            "  A DOM Audio URL is assigned immediately before its first play().",
            "",
            "Audio decisions:",
            *decision_lines,
            "",
        ]
    )
    report_path.write_text(report, encoding="utf-8", newline="\n")
    print(report, end="")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Patch extracted PvZGE JavaScript for hybrid audio loading."
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
