import json
import struct
import tempfile
import unittest
from pathlib import Path

from scripts.transcode_textures_astc import (
    ASTC_FORMAT_CODES,
    TextureJob,
    classify_block,
    discover_jobs,
    largest_connected_fraction,
    parse_astc_header,
    parse_png_header,
    rewrite_image_asset_metadata,
)


def png_header(width: int, height: int, color_type: int = 6) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + bytes((8, color_type, 0, 0, 0))
        + b"\x00\x00\x00\x00"
    )


def astc_file(width: int, height: int, block: int) -> bytes:
    def uint24(value: int) -> bytes:
        return bytes((value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF))

    blocks = ((width + block - 1) // block) * ((height + block - 1) // block)
    return (
        bytes.fromhex("13aba15c")
        + bytes((block, block, 1))
        + uint24(width)
        + uint24(height)
        + uint24(1)
        + bytes(blocks * 16)
    )


class HeaderTests(unittest.TestCase):
    def test_cocos_astc_4x4_format_code(self):
        self.assertEqual(ASTC_FORMAT_CODES["4x4"], 89)

    def test_parses_png_dimensions(self):
        info = parse_png_header(png_header(1024, 512))

        self.assertEqual((info.width, info.height), (1024, 512))
        self.assertEqual(info.color_type, 6)

    def test_parses_and_validates_astc_dimensions_and_block(self):
        info = parse_astc_header(astc_file(65, 33, 6))

        self.assertEqual((info.width, info.height, info.depth), (65, 33, 1))
        self.assertEqual((info.block_x, info.block_y, info.block_z), (6, 6, 1))


class ClassificationTests(unittest.TestCase):
    def test_fullscreen_animated_vortex_atlas_uses_4x4(self):
        with tempfile.TemporaryDirectory() as temporary:
            docs = Path(temporary) / "docs"
            texture = (
                docs
                / "assets"
                / "resources"
                / "native"
                / "20"
                / "201c8e38-0573-4744-a79c-fac86dc60efd.png"
            )
            texture.parent.mkdir(parents=True)
            texture.write_bytes(png_header(1024, 512) + bytes(138_199 - 33))

            jobs = discover_jobs(
                docs_dir=docs,
                small_texture_max=512,
                detail_bpp_threshold=6.0,
                large_texture_min=2048,
                background_component_threshold=0.35,
                alpha_threshold=64,
                analysis_size=128,
            )

        self.assertEqual(jobs[0].block, "4x4")
        self.assertEqual(jobs[0].reason, "quality_override")

    def test_small_textures_use_4x4(self):
        block, reason = classify_block(
            width=256,
            height=512,
            png_size=1024,
            small_texture_max=512,
            detail_bpp_threshold=6.0,
            large_texture_min=2048,
            background_component_threshold=0.35,
            largest_alpha_component_fraction=None,
        )

        self.assertEqual(block, "4x4")
        self.assertEqual(reason, "small")

    def test_large_complex_textures_use_4x4(self):
        block, reason = classify_block(
            width=2048,
            height=2048,
            png_size=3_200_000,
            small_texture_max=512,
            detail_bpp_threshold=6.0,
            large_texture_min=2048,
            background_component_threshold=0.35,
            largest_alpha_component_fraction=None,
        )

        self.assertEqual(block, "4x4")
        self.assertEqual(reason, "detail")

    def test_large_compressible_textures_use_8x8(self):
        block, reason = classify_block(
            width=2048,
            height=2048,
            png_size=1_000_000,
            small_texture_max=512,
            detail_bpp_threshold=6.0,
            large_texture_min=2048,
            background_component_threshold=0.35,
            largest_alpha_component_fraction=0.10,
        )

        self.assertEqual(block, "8x8")
        self.assertEqual(reason, "compact")

    def test_large_connected_backgrounds_use_6x6(self):
        block, reason = classify_block(
            width=4096,
            height=4096,
            png_size=1_000_000,
            small_texture_max=512,
            detail_bpp_threshold=6.0,
            large_texture_min=2048,
            background_component_threshold=0.35,
            largest_alpha_component_fraction=0.60,
        )

        self.assertEqual(block, "6x6")
        self.assertEqual(reason, "background")

    def test_connected_area_does_not_merge_separate_islands(self):
        alpha = bytes(
            [
                255, 255, 0, 255,
                255, 255, 0, 0,
                0, 0, 0, 255,
                255, 0, 0, 0,
            ]
        )

        self.assertEqual(largest_connected_fraction(alpha, 4, 4, 64), 0.25)


class MetadataRewriteTests(unittest.TestCase):
    def test_rewrites_standalone_and_packed_image_assets(self):
        with tempfile.TemporaryDirectory() as temporary:
            docs = Path(temporary) / "docs"
            bundle = docs / "assets" / "resources"
            native = bundle / "native"
            imports = bundle / "import"
            (native / "aa").mkdir(parents=True)
            (native / "6f").mkdir(parents=True)
            (imports / "aa").mkdir(parents=True)
            (imports / "02").mkdir(parents=True)

            standalone_png = native / "aa" / "aa1111111.png"
            packed_png = (
                native
                / "6f"
                / "6f01cf7f-81bf-4a7e-bd5d-0afc19696480@b47c0@40c10.png"
            )
            standalone_png.write_bytes(png_header(64, 64))
            packed_png.write_bytes(png_header(2048, 2048))

            standalone_import = imports / "aa" / "aa1111111.json"
            standalone_import.write_text(
                json.dumps([{"fmt": "0", "w": 64, "h": 64}]),
                encoding="utf-8",
            )

            config = {
                "uuids": ["6fCompressedUuid@b47c0@40c10"],
                "packs": {"02ed09922": [0]},
            }
            (bundle / "config.json").write_text(
                json.dumps(config), encoding="utf-8"
            )
            packed_import = imports / "02" / "02ed09922.json"
            packed_import.write_text(
                json.dumps([0, 0, 0, 0, 0, [[{"fmt": "0", "w": 0, "h": 0}]]]),
                encoding="utf-8",
            )

            jobs = [
                TextureJob.for_test(standalone_png, "6x6"),
                TextureJob.for_test(packed_png, "8x8"),
            ]
            stats = rewrite_image_asset_metadata(jobs)

            standalone = json.loads(standalone_import.read_text(encoding="utf-8"))
            packed = json.loads(packed_import.read_text(encoding="utf-8"))
            self.assertEqual(
                standalone[0]["fmt"], f"7@{ASTC_FORMAT_CODES['6x6']}"
            )
            self.assertEqual(
                packed[5][0][0]["fmt"], f"7@{ASTC_FORMAT_CODES['8x8']}"
            )
            self.assertEqual(stats.standalone, 1)
            self.assertEqual(stats.packed, 1)


if __name__ == "__main__":
    unittest.main()
