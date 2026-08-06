#!/usr/bin/env python3

"""Audit extracted audio assets so native playback limits can be calibrated.

Scans every supported audio file below a docs directory, probes duration /
sample rate / channel count with afinfo (macOS) or ffprobe, and reports the
distribution that matters for the GardendlessLoader native audio engine:
compressed size vs the short-SFX byte limit, duration vs the short-SFX
duration limit, and estimated decoded PCM size vs the buffer/cache limits.

Usage:
  python3 scripts/audit_audio_assets.py docs [--json report.json]
"""

import argparse
import json
import shutil
import statistics
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


AUDIO_EXTENSIONS = {".mp3", ".m4a"}


def parse_afinfo(text: str) -> dict:
    """Parse `afinfo` output into duration/sample_rate/channels/bit_rate."""
    result = {}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("estimated duration:"):
            value = stripped.split(":", 1)[1].strip().split()[0]
            try:
                result["duration"] = float(value)
            except ValueError:
                pass
        elif stripped.startswith("Data format:"):
            match = __import__("re").search(
                r"(\d+)\s+ch,\s+(\d+)\s+Hz", stripped
            )
            if match:
                result["channels"] = int(match.group(1))
                result["sample_rate"] = int(match.group(2))
        elif stripped.startswith("bit rate:"):
            value = stripped.split(":", 1)[1].strip().split()[0]
            try:
                result["bit_rate"] = int(value)
            except ValueError:
                pass
    return result


def probe_afinfo(path: Path) -> dict:
    output = subprocess.run(
        ["afinfo", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if output.returncode != 0:
        return {}
    return parse_afinfo(output.stdout)


def probe_ffprobe(path: Path) -> dict:
    output = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if output.returncode != 0:
        return {}
    try:
        data = json.loads(output.stdout)
    except json.JSONDecodeError:
        return {}
    stream = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "audio"),
        None,
    )
    if not stream:
        return {}
    duration = float(data.get("format", {}).get("duration") or 0)
    return {
        "duration": duration or None,
        "sample_rate": int(stream.get("sample_rate") or 0) or None,
        "channels": int(stream.get("channels") or 0) or None,
        "bit_rate": int(data.get("format", {}).get("bit_rate") or 0) or None,
    }


def probe_file(path: Path, probe: str) -> dict:
    if probe == "afinfo":
        return probe_afinfo(path)
    if probe == "ffprobe":
        return probe_ffprobe(path)
    raise ValueError(f"unknown probe: {probe}")


def classify(
    size: int,
    duration: float,
    short_bytes: int,
    short_duration: float,
) -> str:
    if size <= 0 or duration is None or duration <= 0:
        return "unreadable"
    if size <= short_bytes and duration <= short_duration:
        return "short_candidate"
    if size > short_bytes and duration <= short_duration:
        return "size_over"
    if duration > short_duration:
        return "long_candidate"
    return "unknown"


def pcm_bytes(duration: float, sample_rate: int, channels: int) -> int:
    if not duration or not sample_rate or not channels:
        return 0
    return int(duration * sample_rate * channels * 4)


def audit(
    root: Path,
    short_bytes: int,
    short_duration: float,
    single_buffer: int,
    probe: str,
    workers: int = 8,
) -> dict:
    files = sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS
    )
    rows = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_path = {
            executor.submit(probe_file, path, probe): path for path in files
        }
        for future in future_to_path:
            path = future_to_path[future]
            try:
                meta = future.result()
            except Exception:
                meta = {}
            size = path.stat().st_size
            rows.append(
                {
                    "path": str(path.relative_to(root)),
                    "size": size,
                    "duration": meta.get("duration"),
                    "sample_rate": meta.get("sample_rate"),
                    "channels": meta.get("channels"),
                    "bit_rate": meta.get("bit_rate"),
                    "pcm_bytes": pcm_bytes(
                        meta.get("duration"),
                        meta.get("sample_rate"),
                        meta.get("channels"),
                    ),
                }
            )

    durations = [r["duration"] for r in rows if r["duration"] is not None]
    sizes = [r["size"] for r in rows]
    pcm = [r["pcm_bytes"] for r in rows if r["pcm_bytes"] > 0]
    readable = [r for r in rows if r["duration"] is not None]

    def count(predicate):
        return sum(1 for r in rows if predicate(r))

    summary = {
        "files": len(rows),
        "readable": len(readable),
        "total_bytes": sum(sizes),
        "max_bytes": max(sizes) if sizes else 0,
        "sizes": {
            "over_256kb": count(lambda r: r["size"] > 256 * 1024),
            "over_384kb": count(lambda r: r["size"] > 384 * 1024),
            "over_512kb": count(lambda r: r["size"] > 512 * 1024),
            "over_1mb": count(lambda r: r["size"] > 1024 * 1024),
        },
        "durations": {
            "under_1s": count(lambda r: r["duration"] is not None and r["duration"] <= 1),
            "1_to_3s": count(
                lambda r: r["duration"] is not None and 1 < r["duration"] <= 3
            ),
            "3_to_10s": count(
                lambda r: r["duration"] is not None and 3 < r["duration"] <= 10
            ),
            "10_to_30s": count(
                lambda r: r["duration"] is not None and 10 < r["duration"] <= 30
            ),
            "over_30s": count(lambda r: r["duration"] is not None and r["duration"] > 30),
        },
        "classifications": {},
        "pcm": {
            "estimated_total_bytes": sum(pcm),
            "over_single_buffer": count(lambda r: r["pcm_bytes"] > single_buffer),
        },
    }
    for label in [
        "unreadable",
        "short_candidate",
        "size_over",
        "long_candidate",
    ]:
        summary["classifications"][label] = count(
            lambda r, label=label: classify(
                r["size"],
                r["duration"],
                short_bytes,
                short_duration,
            )
            == label
        )

    short_candidates = [
        r
        for r in rows
        if classify(r["size"], r["duration"], short_bytes, short_duration)
        == "short_candidate"
    ]
    short_pcm_values = sorted(
        [r["pcm_bytes"] for r in short_candidates if r["pcm_bytes"] > 0]
    )

    def pcm_percentile(values, p):
        if not values:
            return None
        return values[min(len(values) - 1, int(len(values) * p))]

    summary["short_pool"] = {
        "candidates": len(short_candidates),
        "pcm_bytes": sum(r["pcm_bytes"] for r in short_candidates),
        "p50_duration": round(statistics.median(
            [r["duration"] for r in short_candidates if r["duration"]]
        ), 2) if short_candidates else None,
        "p50_size": round(statistics.median(
            [r["size"] for r in short_candidates]
        ), 0) if short_candidates else None,
        "p50_pcm": pcm_percentile(short_pcm_values, 0.5),
        "p90_pcm": pcm_percentile(short_pcm_values, 0.9),
    }
    if durations:
        durations_sorted = sorted(durations)
        sizes_sorted = sorted(sizes)

        def percentile(values, p):
            if not values:
                return None
            index = min(len(values) - 1, int(len(values) * p))
            return values[index]

        summary["duration_percentiles"] = {
            "p50": round(percentile(durations_sorted, 0.5), 2),
            "p90": round(percentile(durations_sorted, 0.9), 2),
            "p99": round(percentile(durations_sorted, 0.99), 2),
            "max": round(durations_sorted[-1], 2),
        }
        summary["size_percentiles"] = {
            "p50": percentile(sizes_sorted, 0.5),
            "p90": percentile(sizes_sorted, 0.9),
            "p99": percentile(sizes_sorted, 0.99),
            "max": sizes_sorted[-1],
        }
    return summary


def render(summary: dict, short_bytes: int, short_duration: float) -> str:
    lines = [
        "Audio asset audit",
        "",
        f"Files: {summary['files']} (readable {summary['readable']})",
        f"Total compressed: {summary['total_bytes'] / 1048576:.1f} MB, "
        f"max {summary['max_bytes'] / 1024:.1f} KB",
        "",
        f"Short-SFX rule: size <= {short_bytes / 1024:.0f} KB "
        f"and duration <= {short_duration:.0f} s",
        "Sizes: " + ", ".join(
            f">{k.replace('over_', '')} = {v}"
            for k, v in summary["sizes"].items()
        ),
        "Durations: " + ", ".join(
            f"{k} = {v}" for k, v in summary["durations"].items()
        ),
        "Classifications: " + ", ".join(
            f"{k} = {v}" for k, v in summary["classifications"].items()
        ),
        "",
        f"Estimated PCM total: {summary['pcm']['estimated_total_bytes'] / 1048576:.1f} MB, "
        f"over single-buffer limit: {summary['pcm']['over_single_buffer']}",
    ]
    if "short_pool" in summary:
        pool = summary["short_pool"]
        lines.append(
            f"Short pool: {pool['candidates']} candidates, "
            f"PCM {pool['pcm_bytes'] / 1048576:.1f} MB, "
            f"median duration {pool['p50_duration']} s, "
            f"median size {pool['p50_size'] / 1024:.1f} KB, "
            f"median PCM {pool['p50_pcm'] / 1048576:.2f} MB, "
            f"p90 PCM {pool['p90_pcm'] / 1048576:.2f} MB"
        )
    if "duration_percentiles" in summary:
        lines.append(
            "Duration percentiles: "
            + " ".join(f"{k}={v}s" for k, v in summary["duration_percentiles"].items())
        )
        lines.append(
            "Size percentiles: "
            + " ".join(f"{k}={v}" for k, v in summary["size_percentiles"].items())
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("docs_dir", type=Path)
    parser.add_argument(
        "--probe",
        choices=["auto", "afinfo", "ffprobe"],
        default="auto",
    )
    parser.add_argument("--short-bytes", type=int, default=256 * 1024)
    parser.add_argument("--short-duration", type=float, default=10)
    parser.add_argument("--single-buffer", type=int, default=4 * 1024 * 1024)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    if not args.docs_dir.is_dir():
        print(f"error: not a directory: {args.docs_dir}")
        return 1
    probe = args.probe
    if probe == "auto":
        if shutil.which("afinfo"):
            probe = "afinfo"
        elif shutil.which("ffprobe"):
            probe = "ffprobe"
        else:
            print("error: neither afinfo nor ffprobe is available")
            return 1

    summary = audit(
        args.docs_dir,
        short_bytes=args.short_bytes,
        short_duration=args.short_duration,
        single_buffer=args.single_buffer,
        probe=probe,
        workers=args.workers,
    )
    print(render(summary, args.short_bytes, args.short_duration))
    if args.json:
        args.json.write_text(
            json.dumps(summary, indent=2),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
