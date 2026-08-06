#!/usr/bin/env python3

import argparse
import concurrent.futures
import csv
import io
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
ASTC_MAGIC = bytes.fromhex("13aba15c")
ASTC_FORMAT_CODES = {"4x4": 89, "6x6": 93, "8x8": 96, "10x10": 100}
FALLBACK_EXTENSIONS = (".png", ".pkm", ".pvr", ".webp", ".jpg", ".jpeg", ".bmp")
QUALITY_CRITICAL_TEXTURE_IDS = {
    # This atlas contains the animated vortex that is enlarged to fill loading
    # screens. Its low PNG bpp looks compact, but ASTC 10x10 visibly bands the
    # gradient and destroys texture detail after the runtime scaling.
    "201c8e38-0573-4744-a79c-fac86dc60efd",
}


class AstcError(RuntimeError):
    pass


@dataclass(frozen=True)
class PngInfo:
    width: int
    height: int
    bit_depth: int
    color_type: int


@dataclass(frozen=True)
class AstcInfo:
    block_x: int
    block_y: int
    block_z: int
    width: int
    height: int
    depth: int
    expected_size: int


@dataclass(frozen=True)
class TextureJob:
    png_path: Path
    astc_path: Path
    bundle_dir: Path
    width: int
    height: int
    png_size: int
    block: str
    reason: str
    largest_alpha_component_fraction: float | None = None

    @classmethod
    def for_test(cls, png_path: Path, block: str) -> "TextureJob":
        data = png_path.read_bytes()
        info = parse_png_header(data)
        native_dir = next(
            parent for parent in png_path.parents if parent.name == "native"
        )
        return cls(
            png_path=png_path,
            astc_path=png_path.with_suffix(".astc"),
            bundle_dir=native_dir.parent,
            width=info.width,
            height=info.height,
            png_size=len(data),
            block=block,
            reason="test",
        )


@dataclass(frozen=True)
class MetadataStats:
    standalone: int
    packed: int
    files_written: int


def parse_png_header(data: bytes) -> PngInfo:
    if len(data) < 33 or not data.startswith(PNG_MAGIC):
        raise AstcError("input is not a PNG file")
    if data[12:16] != b"IHDR":
        raise AstcError("PNG IHDR chunk is missing")
    width, height = struct.unpack(">II", data[16:24])
    if width <= 0 or height <= 0:
        raise AstcError(f"invalid PNG dimensions: {width}x{height}")
    return PngInfo(width, height, data[24], data[25])


def _read_uint24_le(data: bytes, offset: int) -> int:
    return data[offset] | (data[offset + 1] << 8) | (data[offset + 2] << 16)


def parse_astc_header(data: bytes) -> AstcInfo:
    if len(data) < 16 or not data.startswith(ASTC_MAGIC):
        raise AstcError("output is not an ASTC file")
    block_x, block_y, block_z = data[4], data[5], data[6]
    width = _read_uint24_le(data, 7)
    height = _read_uint24_le(data, 10)
    depth = _read_uint24_le(data, 13)
    if min(block_x, block_y, block_z, width, height, depth) <= 0:
        raise AstcError("ASTC header contains a zero dimension")
    block_count = (
        math.ceil(width / block_x)
        * math.ceil(height / block_y)
        * math.ceil(depth / block_z)
    )
    return AstcInfo(
        block_x,
        block_y,
        block_z,
        width,
        height,
        depth,
        16 + block_count * 16,
    )


def classify_block(
    *,
    width: int,
    height: int,
    png_size: int,
    small_texture_max: int,
    detail_bpp_threshold: float,
    large_texture_min: int,
    background_component_threshold: float,
    largest_alpha_component_fraction: float | None,
) -> tuple[str, str]:
    if max(width, height) <= small_texture_max:
        return "4x4", "small"
    compressed_bpp = png_size * 8 / (width * height)
    if compressed_bpp >= detail_bpp_threshold:
        return "4x4", "detail"
    if (
        max(width, height) >= large_texture_min
        and largest_alpha_component_fraction is not None
        and largest_alpha_component_fraction >= background_component_threshold
    ):
        return "6x6", "background"
    return "8x8", "compact"


def largest_connected_fraction(
    alpha: bytes, width: int, height: int, threshold: int
) -> float:
    if width <= 0 or height <= 0 or len(alpha) != width * height:
        raise AstcError("invalid alpha analysis dimensions")

    visited = bytearray(len(alpha))
    largest = 0
    for start, value in enumerate(alpha):
        if visited[start] or value < threshold:
            continue
        visited[start] = 1
        size = 0
        pending = [start]
        while pending:
            index = pending.pop()
            size += 1
            x = index % width
            if x > 0:
                neighbor = index - 1
                if not visited[neighbor] and alpha[neighbor] >= threshold:
                    visited[neighbor] = 1
                    pending.append(neighbor)
            if x + 1 < width:
                neighbor = index + 1
                if not visited[neighbor] and alpha[neighbor] >= threshold:
                    visited[neighbor] = 1
                    pending.append(neighbor)
            if index >= width:
                neighbor = index - width
                if not visited[neighbor] and alpha[neighbor] >= threshold:
                    visited[neighbor] = 1
                    pending.append(neighbor)
            if index + width < len(alpha):
                neighbor = index + width
                if not visited[neighbor] and alpha[neighbor] >= threshold:
                    visited[neighbor] = 1
                    pending.append(neighbor)
        largest = max(largest, size)
    return largest / len(alpha)


def analyze_largest_alpha_component(
    data: bytes, analysis_size: int, alpha_threshold: int
) -> float:
    try:
        from PIL import Image
    except ImportError as error:
        raise AstcError(
            "Pillow is required for content-aware ASTC texture classification"
        ) from error

    with Image.open(io.BytesIO(data)) as image:
        image = image.convert("RGBA")
        resampling = getattr(Image, "Resampling", Image)
        image.thumbnail((analysis_size, analysis_size), resampling.BOX)
        alpha = image.getchannel("A")
        return largest_connected_fraction(
            alpha.tobytes(), alpha.width, alpha.height, alpha_threshold
        )


def discover_jobs(
    docs_dir: Path,
    small_texture_max: int,
    detail_bpp_threshold: float,
    large_texture_min: int,
    background_component_threshold: float,
    alpha_threshold: int,
    analysis_size: int,
) -> list[TextureJob]:
    jobs = []
    for png_path in sorted(docs_dir.glob("assets/*/native/**/*.png")):
        data = png_path.read_bytes()
        info = parse_png_header(data)
        native_dir = next(
            parent for parent in png_path.parents if parent.name == "native"
        )
        largest_component = None
        if png_path.stem in QUALITY_CRITICAL_TEXTURE_IDS:
            block, reason = "4x4", "quality_override"
        else:
            block, reason = classify_block(
                width=info.width,
                height=info.height,
                png_size=len(data),
                small_texture_max=small_texture_max,
                detail_bpp_threshold=detail_bpp_threshold,
                large_texture_min=large_texture_min,
                background_component_threshold=background_component_threshold,
                largest_alpha_component_fraction=largest_component,
            )
            if reason == "compact" and max(info.width, info.height) >= large_texture_min:
                largest_component = analyze_largest_alpha_component(
                    data, analysis_size, alpha_threshold
                )
                block, reason = classify_block(
                    width=info.width,
                    height=info.height,
                    png_size=len(data),
                    small_texture_max=small_texture_max,
                    detail_bpp_threshold=detail_bpp_threshold,
                    large_texture_min=large_texture_min,
                    background_component_threshold=background_component_threshold,
                    largest_alpha_component_fraction=largest_component,
                )
        jobs.append(
            TextureJob(
                png_path=png_path,
                astc_path=png_path.with_suffix(".astc"),
                bundle_dir=native_dir.parent,
                width=info.width,
                height=info.height,
                png_size=len(data),
                block=block,
                reason=reason,
                largest_alpha_component_fraction=largest_component,
            )
        )
    if not jobs:
        raise AstcError(f"no native PNG textures found below {docs_dir}")
    return jobs


def _validate_astc_file(path: Path, job: TextureJob) -> None:
    data = path.read_bytes()
    info = parse_astc_header(data)
    block = int(job.block.split("x", 1)[0])
    actual = (info.block_x, info.block_y, info.block_z)
    expected = (block, block, 1)
    if actual != expected:
        raise AstcError(f"{path}: expected ASTC block {expected}, got {actual}")
    if (info.width, info.height, info.depth) != (job.width, job.height, 1):
        raise AstcError(
            f"{path}: expected {job.width}x{job.height}x1, "
            f"got {info.width}x{info.height}x{info.depth}"
        )
    if len(data) != info.expected_size:
        raise AstcError(
            f"{path}: expected {info.expected_size} bytes, got {len(data)}"
        )


def encode_texture(
    job: TextureJob, encoder: str, preset: str, encoder_threads: int
) -> int:
    job.astc_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{job.astc_path.stem}.",
        suffix=".astc",
        dir=job.astc_path.parent,
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
    temporary_path.unlink()
    command = [
        encoder,
        "-cl",
        str(job.png_path),
        str(temporary_path),
        job.block,
        f"-{preset}",
        "-j",
        str(encoder_threads),
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if result.returncode != 0:
            raise AstcError(
                f"astcenc failed for {job.png_path} with exit code "
                f"{result.returncode}:\n{result.stdout.strip()}"
            )
        _validate_astc_file(temporary_path, job)
        os.replace(temporary_path, job.astc_path)
        return job.astc_path.stat().st_size
    finally:
        temporary_path.unlink(missing_ok=True)


def encode_all(
    jobs: list[TextureJob], encoder: str, preset: str, workers: int, encoder_threads: int
) -> int:
    total_bytes = 0
    completed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(encode_texture, job, encoder, preset, encoder_threads): job
            for job in jobs
        }
        try:
            for future in concurrent.futures.as_completed(futures):
                total_bytes += future.result()
                completed += 1
                if completed == len(jobs) or completed % 25 == 0:
                    print(f"Encoded {completed}/{len(jobs)} ASTC textures")
        except BaseException:
            for future in futures:
                future.cancel()
            raise
    return total_bytes


def _find_fmt_nodes(value: Any) -> list[dict[str, Any]]:
    nodes = []
    if isinstance(value, dict):
        if "fmt" in value:
            nodes.append(value)
        for child in value.values():
            nodes.extend(_find_fmt_nodes(child))
    elif isinstance(value, list):
        for child in value:
            nodes.extend(_find_fmt_nodes(child))
    return nodes


def _set_single_fmt(value: Any, block: str, description: str) -> None:
    nodes = _find_fmt_nodes(value)
    if len(nodes) != 1:
        raise AstcError(
            f"{description}: expected exactly one ImageAsset fmt node, "
            f"found {len(nodes)}"
        )
    nodes[0]["fmt"] = f"7@{ASTC_FORMAT_CODES[block]}"


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _write_atomic(path: Path, data: bytes) -> None:
    with tempfile.NamedTemporaryFile(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
    ) as temporary:
        temporary.write(data)
        temporary_path = Path(temporary.name)
    try:
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _packed_location(job: TextureJob) -> tuple[Path, int]:
    config_path = job.bundle_dir / "config.json"
    if not config_path.is_file():
        raise AstcError(f"bundle config is missing: {config_path}")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    stem = job.png_path.stem
    if "@" not in stem:
        raise AstcError(f"cannot map packed ImageAsset without a subasset suffix: {stem}")
    suffix = stem[stem.index("@") :]
    uuid_matches = [
        index
        for index, uuid in enumerate(config.get("uuids", []))
        if isinstance(uuid, str) and uuid.startswith(stem[:2]) and uuid.endswith(suffix)
    ]
    if len(uuid_matches) != 1:
        raise AstcError(
            f"{stem}: expected one UUID ending in {suffix}, found {len(uuid_matches)}"
        )
    uuid_index = uuid_matches[0]
    pack_matches = []
    for pack_id, uuid_indices in config.get("packs", {}).items():
        if uuid_index in uuid_indices:
            pack_matches.append((pack_id, uuid_indices.index(uuid_index)))
    if len(pack_matches) != 1:
        raise AstcError(
            f"{stem}: expected one import pack, found {len(pack_matches)}"
        )
    pack_id, position = pack_matches[0]
    pack_path = job.bundle_dir / "import" / pack_id[:2] / f"{pack_id}.json"
    return pack_path, position


def rewrite_image_asset_metadata(jobs: list[TextureJob]) -> MetadataStats:
    documents: dict[Path, Any] = {}
    standalone = 0
    packed = 0

    def load(path: Path) -> Any:
        if path not in documents:
            if not path.is_file():
                raise AstcError(f"ImageAsset metadata is missing: {path}")
            documents[path] = json.loads(path.read_text(encoding="utf-8"))
        return documents[path]

    for job in jobs:
        native_dir = job.bundle_dir / "native"
        relative = job.png_path.relative_to(native_dir)
        import_path = (job.bundle_dir / "import" / relative).with_suffix(".json")
        if import_path.is_file():
            document = load(import_path)
            _set_single_fmt(document, job.block, str(import_path))
            standalone += 1
            continue

        pack_path, position = _packed_location(job)
        document = load(pack_path)
        try:
            record = document[5][position]
        except (IndexError, TypeError) as error:
            raise AstcError(
                f"{pack_path}: packed record {position} is missing"
            ) from error
        _set_single_fmt(record, job.block, f"{pack_path} record {position}")
        packed += 1

    if standalone + packed != len(jobs):
        raise AstcError(
            f"mapped {standalone + packed} ImageAssets for {len(jobs)} textures"
        )
    for path, document in documents.items():
        _write_atomic(path, _json_bytes(document))
    return MetadataStats(standalone, packed, len(documents))


def remove_fallbacks(jobs: list[TextureJob]) -> tuple[int, int]:
    removed_files = 0
    removed_bytes = 0
    for job in jobs:
        for extension in FALLBACK_EXTENSIONS:
            path = job.png_path.with_suffix(extension)
            if not path.is_file():
                continue
            removed_bytes += path.stat().st_size
            path.unlink()
            removed_files += 1
    return removed_files, removed_bytes


def validate_final_state(docs_dir: Path, jobs: list[TextureJob]) -> None:
    for job in jobs:
        _validate_astc_file(job.astc_path, job)
        if job.png_path.exists():
            raise AstcError(f"PNG fallback was not removed: {job.png_path}")
    remaining_pngs = sorted(docs_dir.glob("assets/*/native/**/*.png"))
    if remaining_pngs:
        raise AstcError(
            f"expected no native PNG fallbacks, found {len(remaining_pngs)}"
        )


def _encoder_version(encoder: str) -> str:
    result = subprocess.run(
        [encoder, "-version"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    first_line = result.stdout.strip().splitlines()
    return first_line[0] if first_line else "unknown"


def write_report(
    report_path: Path,
    jobs: list[TextureJob],
    metadata: MetadataStats,
    astc_bytes: int,
    removed_files: int,
    removed_bytes: int,
    elapsed_seconds: int,
    args: argparse.Namespace,
) -> None:
    by_block = {block: sum(job.block == block for job in jobs) for block in ASTC_FORMAT_CODES}
    by_reason = {
        reason: sum(job.reason == reason for job in jobs)
        for reason in (
            "small",
            "detail",
            "background",
            "compact",
            "quality_override",
        )
    }
    png_bytes = sum(job.png_size for job in jobs)
    rgba_bytes = sum(job.width * job.height * 4 for job in jobs)
    map_path = report_path.with_name("astc-texture-map.csv")
    map_path.parent.mkdir(parents=True, exist_ok=True)
    with map_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(
            [
                "source_path",
                "width",
                "height",
                "png_bytes",
                "png_bits_per_pixel",
                "astc_block",
                "classification_reason",
                "largest_alpha_component_fraction",
                "astc_bytes",
            ]
        )
        for job in jobs:
            writer.writerow(
                [
                    job.png_path.relative_to(args.docs_dir).as_posix(),
                    job.width,
                    job.height,
                    job.png_size,
                    f"{job.png_size * 8 / (job.width * job.height):.4f}",
                    job.block,
                    job.reason,
                    (
                        f"{job.largest_alpha_component_fraction:.4f}"
                        if job.largest_alpha_component_fraction is not None
                        else ""
                    ),
                    job.astc_path.stat().st_size,
                ]
            )
    report = "\n".join(
        [
            "PvZGE ASTC texture transcode summary",
            "",
            "Settings:",
            f"  Encoder: {_encoder_version(args.encoder)}",
            f"  Quality preset: {args.preset}",
            f"  Parallel encoders: {args.jobs}",
            f"  Threads per encoder: {args.encoder_threads}",
            f"  Small texture maximum: {args.small_texture_max}px",
            f"  Detail threshold: {args.detail_bpp_threshold:g} PNG bits/pixel",
            f"  Large texture minimum: {args.large_texture_min}px",
            (
                "  Background connected-area threshold: "
                f"{args.background_component_threshold:g}"
            ),
            f"  Alpha visibility threshold: {args.alpha_threshold}/255",
            f"  Content analysis size: {args.analysis_size}px",
            "  Compatibility fallback: disabled",
            "",
            "Classification:",
            f"  Textures: {len(jobs)}",
            f"  ASTC 6x6: {by_block['6x6']}",
            f"  ASTC 8x8: {by_block['8x8']}",
            f"  ASTC 10x10: {by_block['10x10']}",
            f"  Reason small: {by_reason['small']}",
            f"  Reason detail: {by_reason['detail']}",
            f"  Reason background: {by_reason['background']}",
            f"  Reason compact: {by_reason['compact']}",
            f"  Reason quality override: {by_reason['quality_override']}",
            "",
            "Metadata:",
            f"  Standalone ImageAssets: {metadata.standalone}",
            f"  Packed ImageAssets: {metadata.packed}",
            f"  Import JSON files written: {metadata.files_written}",
            f"  Per-texture map: {map_path}",
            "",
            "Sizes:",
            f"  PNG input bytes: {png_bytes}",
            f"  Estimated decoded RGBA bytes: {rgba_bytes}",
            f"  ASTC output bytes: {astc_bytes}",
            f"  ASTC / PNG: {astc_bytes * 100 / png_bytes:.2f}%",
            f"  ASTC / decoded RGBA: {astc_bytes * 100 / rgba_bytes:.2f}%",
            f"  Removed fallback files: {removed_files}",
            f"  Removed fallback bytes: {removed_bytes}",
            "",
            f"Elapsed seconds: {elapsed_seconds}",
            "",
        ]
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8", newline="\n")
    print(report, end="")


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return parsed


def unit_fraction(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or not 0 < parsed <= 1:
        raise argparse.ArgumentTypeError("must be greater than 0 and at most 1")
    return parsed


def byte_value(value: str) -> int:
    parsed = int(value)
    if not 0 <= parsed <= 255:
        raise argparse.ArgumentTypeError("must be between 0 and 255")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Transcode Cocos native PNG textures to ASTC without fallbacks."
    )
    parser.add_argument("docs_dir", nargs="?", default=Path("docs"), type=Path)
    parser.add_argument(
        "report_path",
        nargs="?",
        default=Path("reports/astc-texture-summary.txt"),
        type=Path,
    )
    parser.add_argument("--encoder", default=os.environ.get("ASTCENC", "astcenc"))
    parser.add_argument(
        "--preset",
        choices=("fastest", "fast", "medium", "thorough", "verythorough", "exhaustive"),
        default=os.environ.get("ASTC_PRESET", "fast"),
    )
    parser.add_argument(
        "--jobs",
        type=positive_int,
        default=positive_int(os.environ.get("ASTC_JOBS", str(os.cpu_count() or 1))),
    )
    parser.add_argument(
        "--encoder-threads",
        type=positive_int,
        default=positive_int(os.environ.get("ASTC_ENCODER_THREADS", "1")),
    )
    parser.add_argument(
        "--small-texture-max",
        type=positive_int,
        default=positive_int(os.environ.get("ASTC_SMALL_TEXTURE_MAX", "512")),
    )
    parser.add_argument(
        "--detail-bpp-threshold",
        type=positive_float,
        default=positive_float(os.environ.get("ASTC_DETAIL_BPP_THRESHOLD", "6")),
    )
    parser.add_argument(
        "--large-texture-min",
        type=positive_int,
        default=positive_int(os.environ.get("ASTC_LARGE_TEXTURE_MIN", "2048")),
    )
    parser.add_argument(
        "--background-component-threshold",
        type=unit_fraction,
        default=unit_fraction(
            os.environ.get("ASTC_BACKGROUND_COMPONENT_THRESHOLD", "0.35")
        ),
    )
    parser.add_argument(
        "--alpha-threshold",
        type=byte_value,
        default=byte_value(os.environ.get("ASTC_ALPHA_THRESHOLD", "64")),
    )
    parser.add_argument(
        "--analysis-size",
        type=positive_int,
        default=positive_int(os.environ.get("ASTC_ANALYSIS_SIZE", "128")),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    started_at = time.monotonic()
    try:
        if not args.docs_dir.is_dir():
            raise AstcError(f"docs directory does not exist: {args.docs_dir}")
        encoder = shutil.which(args.encoder)
        if encoder is None:
            raise AstcError(f"ASTC encoder is unavailable: {args.encoder}")
        args.encoder = encoder
        jobs = discover_jobs(
            args.docs_dir,
            args.small_texture_max,
            args.detail_bpp_threshold,
            args.large_texture_min,
            args.background_component_threshold,
            args.alpha_threshold,
            args.analysis_size,
        )
        print(
            f"Encoding {len(jobs)} PNG textures with {args.jobs} workers "
            f"using ASTC {args.preset}"
        )
        astc_bytes = encode_all(
            jobs, encoder, args.preset, args.jobs, args.encoder_threads
        )
        metadata = rewrite_image_asset_metadata(jobs)
        removed_files, removed_bytes = remove_fallbacks(jobs)
        validate_final_state(args.docs_dir, jobs)
        write_report(
            args.report_path,
            jobs,
            metadata,
            astc_bytes,
            removed_files,
            removed_bytes,
            round(time.monotonic() - started_at),
            args,
        )
    except (AstcError, OSError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
