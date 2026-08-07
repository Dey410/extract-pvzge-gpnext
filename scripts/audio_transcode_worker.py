#!/usr/bin/env python3
"""Per-file MP3 -> AAC-LC worker used by transcode-assets.sh.

Each invocation handles exactly one .mp3 path: probes it, applies the
compact-150 policy, optionally encodes a temporary M4A, compares the real
output size with the original, and appends one TSV decision row to the shared
decisions log.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

from audio_policy import (
    MIN_EXPECTED_SAVING_RATIO,
    MIN_SIZE_SAVING_RATIO,
    PROFILE_COMPACT_150,
    AudioPolicyError,
    select_audio_policy,
)


def read_magic(path: Path) -> bytes:
    with open(path, "rb") as handle:
        return handle.read(32)


def is_m4a(path: Path) -> bool:
    return read_magic(path).startswith(b"ftypM4A")


def probe_ffprobe(ffprobe: str, path: Path) -> dict[str, Any]:
    command = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=duration,sample_rate,channels,bit_rate",
        "-show_entries",
        "format=duration,bit_rate",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip() or f"ffprobe could not read {path}"
        )
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"ffprobe returned invalid JSON for {path}: {error}") from error

    stream = next(
        (item for item in data.get("streams", []) if item.get("codec_type") == "audio"),
        {},
    )
    fmt = data.get("format", {})

    def first(*values: Any) -> Any:
        for value in values:
            if value not in (None, "", "N/A"):
                return value
        return None

    def to_float(value: Any) -> Optional[float]:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def to_int(value: Any) -> Optional[int]:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    duration = to_float(first(stream.get("duration"), fmt.get("duration")))
    sample_rate = to_int(first(stream.get("sample_rate")))
    channels = to_int(first(stream.get("channels")))
    bitrate = to_int(first(stream.get("bit_rate"), fmt.get("bit_rate")))
    return {
        "duration": duration,
        "sample_rate": sample_rate,
        "channels": channels,
        "bitrate": bitrate,
    }


def encode_aac(ffmpeg: str, source: Path, destination: Path, policy: dict[str, Any]) -> None:
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-vn",
        "-c:a",
        "aac",
        "-profile:a",
        "aac_low",
        "-b:a",
        f"{policy['target_bitrate'] // 1000}k",
        "-ac",
        str(policy["target_channels"]),
        "-ar",
        str(policy["target_sample_rate"]),
        "-threads",
        "1",
        "-map_metadata",
        "-1",
        "-movflags",
        "+faststart",
        "-f",
        "ipod",
        str(destination),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip() or f"ffmpeg failed for {source}"
        )
    if not destination.is_file() or destination.stat().st_size <= 0:
        raise RuntimeError(f"ffmpeg produced an empty file for {source}")
    if not read_magic(destination).startswith(b"ftypM4A"):
        raise RuntimeError(f"ffmpeg produced an invalid M4A file for {source}")


def write_decision(
    log_path: Path,
    action: str,
    reason: str,
    policy_class: str,
    target_bitrate: Optional[int],
    output_size: Optional[int],
    duration: Optional[float],
) -> None:
    fields = [
        action,
        reason,
        policy_class,
        str(target_bitrate) if target_bitrate is not None else "-",
        str(output_size) if output_size is not None else "-",
        f"{duration:.6f}" if duration is not None else "-",
    ]
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write("\t".join(fields) + "\n")


def _find_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"required command is unavailable: {name}")
    return path


def process_file(args: argparse.Namespace) -> int:
    source = Path(args.file)
    decisions_log = Path(args.decisions_log)
    ffmpeg = _find_tool("ffmpeg")
    ffprobe = _find_tool("ffprobe")
    min_size_saving_ratio = float(args.min_size_saving_ratio)
    min_expected_saving_ratio = float(args.min_expected_saving_ratio)

    try:
        meta = probe_ffprobe(ffprobe, source)
    except Exception as error:
        print(f"warning: {error}; keeping {source} unchanged", file=sys.stderr)
        write_decision(
            decisions_log,
            "unreadable",
            "unreadable_safe_keep",
            "-",
            None,
            None,
            None,
        )
        return 0

    duration = meta.get("duration")
    if is_m4a(source):
        # Already-encoded AAC must never be re-encoded.
        write_decision(
            decisions_log,
            "already_aac",
            "-",
            "-",
            None,
            None,
            duration,
        )
        return 0

    input_size = source.stat().st_size
    try:
        policy = select_audio_policy(
            {**meta, "input_size": input_size},
            profile=args.profile,
            min_expected_saving_ratio=min_expected_saving_ratio,
        )
    except AudioPolicyError as error:
        print(f"warning: {error}; keeping {source} unchanged", file=sys.stderr)
        write_decision(
            decisions_log,
            "unreadable",
            "unreadable_safe_keep",
            "-",
            None,
            None,
            duration,
        )
        return 0

    if policy["action"] == "keep":
        write_decision(
            decisions_log,
            "keep",
            policy["reason"],
            policy["policy_class"],
            policy["target_bitrate"],
            None,
            duration,
        )
        return 0

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{source.name}.transcoding.",
        suffix=".m4a",
        dir=source.parent,
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        encode_aac(ffmpeg, source, temporary, policy)
        output_size = temporary.stat().st_size
        if output_size <= input_size * (1.0 - min_size_saving_ratio):
            os.replace(temporary, source)
            write_decision(
                decisions_log,
                "transcode",
                policy["reason"],
                policy["policy_class"],
                policy["target_bitrate"],
                output_size,
                duration,
            )
        else:
            temporary.unlink(missing_ok=True)
            write_decision(
                decisions_log,
                "keep",
                "rejected_after_size_compare",
                policy["policy_class"],
                policy["target_bitrate"],
                None,
                duration,
            )
    except Exception as error:
        temporary.unlink(missing_ok=True)
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True)
    parser.add_argument("--decisions-log", required=True)
    parser.add_argument("--profile", default=PROFILE_COMPACT_150)
    parser.add_argument(
        "--min-size-saving-ratio",
        default=str(MIN_SIZE_SAVING_RATIO),
    )
    parser.add_argument(
        "--min-expected-saving-ratio",
        default=str(MIN_EXPECTED_SAVING_RATIO),
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return process_file(args)


if __name__ == "__main__":
    raise SystemExit(main())
