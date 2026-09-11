#!/usr/bin/env python3
"""Convert Lite's native AVIF/PNG textures using the compact-150 ASTC policy.

Original images are retained. Cocos metadata selects only ASTC after every
output has been validated; originals are not automatic runtime fallbacks.
"""
import argparse
import base64
import concurrent.futures
import csv
import io
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ASTC_MAGIC = bytes.fromhex("13aba15c")
ASTC_FORMAT_CODES = {"4x4": 89, "6x6": 93, "8x8": 96}
QUALITY_CRITICAL_TEXTURE_IDS = {"201c8e38-0573-4744-a79c-fac86dc60efd"}


class AstcError(RuntimeError):
    pass


@dataclass(frozen=True)
class TextureJob:
    source_path: Path
    png_path: Path
    astc_path: Path
    bundle_dir: Path
    width: int
    height: int
    source_format: str
    source_bytes: int
    png_size: int
    block: str
    reason: str
    largest_alpha_component_fraction: float | None = None


@dataclass(frozen=True)
class AstcInfo:
    block_x: int
    block_y: int
    block_z: int
    width: int
    height: int
    depth: int
    expected_size: int


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


def discover_jobs(args: argparse.Namespace) -> list[TextureJob]:
    from PIL import Image

    jobs = []
    outputs = set()
    paths = sorted(
        path for path in args.docs_dir.glob("assets/*/native/**/*")
        if path.is_file() and path.suffix.lower() in (".png", ".avif")
    )
    for source in paths:
        astc_path = source.with_suffix(".astc")
        if astc_path in outputs or astc_path.exists():
            raise AstcError(f"ASTC output already exists or has duplicate sources: {astc_path}")
        outputs.add(astc_path)
        # Content detection handles AVIF data stored at a .png path.
        with Image.open(source) as image:
            source_format = image.format
            if source_format not in ("PNG", "AVIF"):
                raise AstcError(f"{source}: expected PNG or AVIF, got {source_format}")
            if getattr(image, "n_frames", 1) != 1:
                raise AstcError(f"{source}: animated images cannot become a single ASTC texture")
            rgba = image.convert("RGBA")
            normalized = io.BytesIO()
            rgba.save(normalized, format="PNG", compress_level=6)
            png_data = normalized.getvalue()
            width, height = rgba.size

        # Preserve the original PNG bpp policy. AVIF uses a lossless RGBA PNG
        # of the decoded pixels; AVIF bytes are not comparable to PNG bytes.
        png_size = source.stat().st_size if source_format == "PNG" else len(png_data)
        largest_component = None
        policy = dict(
            width=width, height=height, png_size=png_size,
            small_texture_max=args.small_texture_max,
            detail_bpp_threshold=args.detail_bpp_threshold,
            large_texture_min=args.large_texture_min,
            background_component_threshold=args.background_component_threshold,
        )
        if source.stem.split("@", 1)[0] in QUALITY_CRITICAL_TEXTURE_IDS:
            block, reason = "4x4", "quality_override"
        else:
            block, reason = classify_block(
                **policy, largest_alpha_component_fraction=None
            )
            if reason == "compact" and max(width, height) >= args.large_texture_min:
                largest_component = analyze_largest_alpha_component(
                    png_data, args.analysis_size, args.alpha_threshold
                )
                block, reason = classify_block(
                    **policy, largest_alpha_component_fraction=largest_component
                )

        png_path = args.work_dir / f"{len(jobs):06d}.png"
        png_path.write_bytes(png_data)
        relative = source.relative_to(args.docs_dir)
        jobs.append(TextureJob(
            source, png_path, astc_path,
            args.docs_dir / relative.parts[0] / relative.parts[1],
            width, height, source_format, source.stat().st_size,
            png_size, block, reason, largest_component,
        ))
    if not jobs:
        raise AstcError(f"no native PNG/AVIF textures found below {args.docs_dir}")
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
    # A unique same-directory staging file permits atomic publication.
    # Failed staging files are retained for inspection, never deleted.
    with tempfile.NamedTemporaryFile(
        prefix=f".{job.astc_path.stem}.", suffix=".astc",
        dir=job.astc_path.parent, delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
    result = subprocess.run(
        [encoder, "-cl", str(job.png_path), str(temporary_path),
         job.block, f"-{preset}", "-j", str(encoder_threads)],
        check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    if result.returncode != 0:
        raise AstcError(f"astcenc failed for {job.source_path}:\n{result.stdout.strip()}")
    _validate_astc_file(temporary_path, job)
    os.replace(temporary_path, job.astc_path)
    return job.astc_path.stat().st_size


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
    os.replace(temporary_path, path)


def decode_uuid(value: str) -> str:
    core, separator, suffix = value.partition("@")
    if len(core) != 22:
        return value
    try:
        # Cocos keeps the first two hex digits and base64-encodes the other 15 bytes.
        decoded = uuid.UUID(hex=core[:2] + base64.b64decode(core[2:], validate=True).hex())
    except ValueError as error:
        raise AstcError(f"invalid compressed Cocos UUID: {value}") from error
    return str(decoded) + separator + suffix


def _packed_location(job: TextureJob) -> tuple[Path, int]:
    config_path = job.bundle_dir / "config.json"
    if not config_path.is_file():
        raise AstcError(f"bundle config is missing: {config_path}")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    stem = job.source_path.stem
    matches = [
        index for index, value in enumerate(config.get("uuids", []))
        if isinstance(value, str) and decode_uuid(value) == stem
    ]
    if len(matches) != 1:
        raise AstcError(f"{stem}: expected one exact UUID, found {len(matches)}")
    packs = [
        (pack_id, indices.index(matches[0]))
        for pack_id, indices in config.get("packs", {}).items()
        if matches[0] in indices
    ]
    if len(packs) != 1:
        raise AstcError(f"{stem}: expected one import pack, found {len(packs)}")
    pack_id, position = packs[0]
    # Reject unexpected paths from config before accessing pack files.
    if not isinstance(pack_id, str) or any(c in pack_id for c in ("/", "\\")) or ".." in pack_id:
        raise AstcError(f"invalid import pack ID: {pack_id}")
    return job.bundle_dir / "import" / pack_id[:2] / f"{pack_id}.json", position


def plan_metadata(jobs: list[TextureJob]) -> dict[Path, bytes]:
    documents: dict[Path, Any] = {}
    used_records = set()

    def load(path: Path) -> Any:
        if path not in documents:
            if not path.is_file():
                raise AstcError(f"ImageAsset metadata is missing: {path}")
            documents[path] = json.loads(path.read_text(encoding="utf-8"))
        return documents[path]

    for job in jobs:
        relative = job.source_path.relative_to(job.bundle_dir / "native")
        import_path = (job.bundle_dir / "import" / relative).with_suffix(".json")
        if import_path.is_file():
            key = (import_path, None)
            record = load(import_path)
        else:
            import_path, position = _packed_location(job)
            key = (import_path, position)
            document = load(import_path)
            try:
                record = document[5][position]
            except (IndexError, TypeError, KeyError) as error:
                raise AstcError(f"{import_path}: packed record {position} is missing") from error
        if key in used_records:
            raise AstcError(f"multiple textures map to the same ImageAsset: {key}")
        used_records.add(key)
        _set_single_fmt(record, job.block, str(import_path))
    return {path: _json_bytes(document) for path, document in documents.items()}


def write_report(args: argparse.Namespace, jobs: list[TextureJob],
                 astc_bytes: int, metadata_count: int, elapsed: float,
                 backup_dir: Path) -> None:
    map_path = args.report_path.with_name("astc-texture-map.csv")
    with map_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(["source_path", "source_format", "width", "height",
                         "source_bytes", "classification_png_bytes", "astc_block",
                         "reason", "largest_alpha_component_fraction", "astc_bytes"])
        for job in jobs:
            writer.writerow([
                job.source_path.relative_to(args.docs_dir).as_posix(),
                job.source_format, job.width, job.height, job.source_bytes,
                job.png_size, job.block, job.reason,
                job.largest_alpha_component_fraction, job.astc_path.stat().st_size,
            ])
    lines = [
        "PvZGE Lite ASTC texture transcode summary", "",
        f"Preset: {args.preset} (-cl)",
        f"Workers / threads per encoder: {args.jobs} / {args.encoder_threads}",
        f"Small texture maximum: {args.small_texture_max}px",
        f"Detail threshold: {args.detail_bpp_threshold:g} PNG bits/pixel",
        f"Large texture minimum: {args.large_texture_min}px",
        f"Background connected-area threshold: {args.background_component_threshold:g}",
        f"Alpha threshold / analysis size: {args.alpha_threshold} / {args.analysis_size}px",
        "AVIF classification uses decoded RGBA PNG bytes, not AVIF bytes.",
        f"Textures: {len(jobs)}",
        *[f"Input {fmt}: {sum(j.source_format == fmt for j in jobs)}" for fmt in ("PNG", "AVIF")],
        *[f"ASTC {block}: {sum(j.block == block for j in jobs)}" for block in ASTC_FORMAT_CODES],
        f"Source bytes (retained): {sum(j.source_bytes for j in jobs)}",
        f"ASTC bytes (additional): {astc_bytes}",
        f"ImageAsset JSON files rewritten: {metadata_count}",
        f"Original metadata backup: {backup_dir}",
        f"Per-texture map: {map_path}",
        f"Intermediate PNG files (retained): {args.work_dir}",
        "Runtime requires ASTC support; metadata has no automatic image fallback.",
        f"Elapsed seconds: {elapsed:.1f}", "",
    ]
    report = "\n".join(lines)
    args.report_path.write_text(report, encoding="utf-8")
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
        description="Convert native PNG/AVIF textures to ASTC, preserving source images."
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
    started = time.monotonic()
    try:
        from PIL import features

        if not features.check("avif"):
            raise AstcError("Pillow with AVIF support is required (see scripts/requirements-astc.txt)")
        if not args.docs_dir.is_dir():
            raise AstcError(f"docs directory does not exist: {args.docs_dir}")
        encoder = shutil.which(args.encoder)
        if encoder is None:
            raise AstcError(f"ASTC encoder is unavailable: {args.encoder}")
        args.work_dir = Path(tempfile.mkdtemp(prefix="pvzge-astc-"))
        print(f"Intermediate images retained at {args.work_dir}")
        jobs = discover_jobs(args)
        # Resolve every metadata target before invoking the encoder or changing references.
        documents = plan_metadata(jobs)
        args.report_path.parent.mkdir(parents=True, exist_ok=True)
        backup_dir = Path(tempfile.mkdtemp(
            prefix="astc-original-metadata-", dir=args.report_path.parent
        ))
        for path in documents:
            backup = backup_dir / path.relative_to(args.docs_dir)
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, backup)
        astc_bytes = encode_all(jobs, encoder, args.preset, args.jobs, args.encoder_threads)
        for job in jobs:
            _validate_astc_file(job.astc_path, job)
        for path, data in documents.items():
            _write_atomic(path, data)
        write_report(args, jobs, astc_bytes, len(documents),
                     time.monotonic() - started, backup_dir)
    except (AstcError, OSError, ValueError, ImportError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
