import base64
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from scripts.transcode_textures_astc import (
    AstcError, _validate_astc_file, classify_block, decode_uuid,
    discover_jobs, encode_texture, largest_connected_fraction, main,
    parse_args, parse_astc_header, plan_metadata,
)

try:
    from PIL import Image, features
    HAS_AVIF = features.check("avif")
except ImportError:
    HAS_AVIF = False


class PolicyTests(unittest.TestCase):
    def test_reference_classification_and_precedence(self):
        cases = [
            (512, 512, 1024, None, "4x4", "small"),
            (2048, 2048, 3_200_000, 0.6, "4x4", "detail"),
            (4096, 4096, 1_000_000, 0.60, "6x6", "background"),
            (2048, 2048, 1_000_000, 0.10, "8x8", "compact"),
        ]
        for width, height, size, alpha, block, reason in cases:
            with self.subTest(block=block, reason=reason):
                self.assertEqual(classify_block(
                    width=width, height=height, png_size=size,
                    small_texture_max=512, detail_bpp_threshold=6,
                    large_texture_min=2048, background_component_threshold=0.35,
                    largest_alpha_component_fraction=alpha,
                ), (block, reason))

    def test_alpha_islands_are_not_merged(self):
        self.assertEqual(largest_connected_fraction(
            bytes([255, 255, 0, 255, 255, 255, 0, 0,
                   0, 0, 0, 255, 255, 0, 0, 0]), 4, 4, 64,
        ), 0.25)

    def test_cocos_uuid_with_subassets(self):
        self.assertEqual(decode_uuid("fcmR3XADNLgJ1ByKhqcC5Z@a@b"),
                         "fc991dd7-0033-4b80-9d41-c8a86a702e59@a@b")

    def test_rejects_invalid_astc(self):
        for data in (b"not astc", bytes.fromhex("13aba15c") + bytes(12)):
            with self.assertRaises(AstcError):
                parse_astc_header(data)


@unittest.skipUnless(HAS_AVIF, "install scripts/requirements-astc.txt for AVIF tests")
class TextureTests(unittest.TestCase):
    def setUp(self):
        # Retain fixtures; repository policy prohibits recursive deletion.
        self.root = Path(tempfile.mkdtemp(prefix="pvzge-astc-test-"))
        self.docs = self.root / "docs"
        self.bundle = self.docs / "assets" / "resources"
        self.args = parse_args([str(self.docs), str(self.root / "reports" / "summary.txt"),
                                "--jobs", "1"])
        self.args.work_dir = self.root / "decoded"
        self.args.work_dir.mkdir()

    def texture(self, stem="aa1111", extension=".png", fmt="AVIF", size=(65, 33)):
        path = self.bundle / "native" / stem[:2] / (stem + extension)
        path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGBA", size, (80, 140, 200, 255))
        image.paste((0, 0, 0, 0), (0, 0, size[0] // 2, size[1]))
        image.save(path, format=fmt)
        return path

    def metadata(self, source):
        relative = source.relative_to(self.bundle / "native")
        path = (self.bundle / "import" / relative).with_suffix(".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('[{"fmt":"0","w":65,"h":33}]', encoding="utf-8")
        return path

    def test_detects_disguised_avif_and_preserves_alpha_and_source(self):
        source = self.texture()
        before = source.read_bytes()
        jobs = discover_jobs(self.args)
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job.source_format, "AVIF")
        self.assertEqual((job.width, job.height), (65, 33))
        self.assertEqual(job.png_size, job.png_path.stat().st_size)
        with Image.open(job.png_path) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.mode, "RGBA")
            self.assertEqual(image.getchannel("A").getextrema(), (0, 255))
        self.assertEqual(source.read_bytes(), before)

    def test_mixed_png_and_explicit_avif_skip_non_native_images(self):
        self.texture("aa1111", fmt="PNG")
        self.texture("bb2222", extension=".avif")
        (self.docs / "splash.png").write_bytes(b"not a native texture")
        jobs = discover_jobs(self.args)
        self.assertEqual([j.source_format for j in jobs], ["PNG", "AVIF"])

    def test_vortex_override(self):
        self.texture("201c8e38-0573-4744-a79c-fac86dc60efd", size=(1024, 512))
        job = discover_jobs(self.args)[0]
        self.assertEqual((job.block, job.reason), ("4x4", "quality_override"))

    def test_rejects_duplicate_outputs(self):
        self.texture()
        self.texture(extension=".avif")
        with self.assertRaisesRegex(AstcError, "duplicate sources"):
            discover_jobs(self.args)

    def test_rejects_existing_astc_without_overwriting(self):
        source = self.texture()
        output = source.with_suffix(".astc")
        output.write_bytes(b"existing asset")
        with self.assertRaisesRegex(AstcError, "already exists"):
            discover_jobs(self.args)
        self.assertEqual(output.read_bytes(), b"existing asset")

    def test_rejects_corrupt_input(self):
        source = self.texture()
        source.write_bytes(b"broken AVIF")
        with self.assertRaises(OSError):
            discover_jobs(self.args)

    def test_rejects_animated_avif(self):
        source = self.texture()
        frames = [Image.new("RGBA", (64, 64), color) for color in ("red", "blue")]
        frames[0].save(source, format="AVIF", save_all=True,
                       append_images=frames[1:], duration=100)
        with self.assertRaisesRegex(AstcError, "animated"):
            discover_jobs(self.args)

    def test_plans_standalone_and_packed_metadata_without_writing(self):
        standalone = self.texture()
        metadata = self.metadata(standalone)
        original = metadata.read_bytes()
        # Same prefix and suffix: exact UUID matching must distinguish these.
        uuids = ["fc991dd7-0033-4b80-9d41-c8a86a702e59",
                 "fc991dd7-0033-4b80-9d41-c8a86a702e58"]
        packed_sources = [self.texture(value + "@a") for value in uuids]
        compressed = [value[:2] + base64.b64encode(
            bytes.fromhex(value.replace("-", "")[2:])
        ).decode() + "@a" for value in uuids]
        (self.bundle / "config.json").write_text(json.dumps({
            "uuids": compressed, "packs": {"02pack": [0, 1]},
        }))
        pack = self.bundle / "import" / "02" / "02pack.json"
        pack.parent.mkdir(parents=True)
        pack.write_text(json.dumps([0, 0, 0, 0, 0, [
            [{"fmt": "0"}], [{"fmt": "0"}],
        ]]))
        jobs = discover_jobs(self.args)
        jobs = [replace(job, block="6x6" if job.source_path == packed_sources[0]
                        else "8x8") for job in jobs]
        planned = plan_metadata(jobs)
        self.assertEqual(json.loads(planned[pack])[5][0][0]["fmt"], "7@93")
        self.assertEqual(json.loads(planned[pack])[5][1][0]["fmt"], "7@96")
        self.assertEqual(json.loads(planned[metadata])[0]["fmt"], "7@96")
        self.assertEqual(metadata.read_bytes(), original)

    def test_missing_metadata_fails_before_encoder(self):
        self.texture()
        with patch("scripts.transcode_textures_astc.shutil.which", return_value="encoder"), \
             patch("scripts.transcode_textures_astc.encode_all") as encode, \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main([str(self.docs)]), 1)
        encode.assert_not_called()

    def test_encoding_failure_preserves_original_metadata(self):
        source = self.texture()
        metadata = self.metadata(source)
        before = metadata.read_bytes()
        with patch("scripts.transcode_textures_astc.shutil.which", return_value="encoder"), \
             patch("scripts.transcode_textures_astc.encode_all", side_effect=AstcError("failed")), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main([str(self.docs), str(self.args.report_path)]), 1)
        self.assertEqual(metadata.read_bytes(), before)

    def test_rejects_truncated_encoder_output(self):
        self.texture()
        job = discover_jobs(self.args)[0]

        def bad_encoder(command, **kwargs):
            Path(command[3]).write_bytes(bytes.fromhex("13aba15c") + bytes(12))
            return subprocess.CompletedProcess(command, 0, "")

        with patch("scripts.transcode_textures_astc.subprocess.run", side_effect=bad_encoder):
            with self.assertRaises(AstcError):
                encode_texture(job, "encoder", "fast", 1)
        self.assertFalse(job.astc_path.exists())

    @unittest.skipUnless(shutil.which(os.environ.get("ASTCENC", "astcenc")),
                         "astcenc required for real encoding test")
    def test_real_encoder_end_to_end_and_alpha_decode(self):
        source = self.texture()
        metadata = self.metadata(source)
        source_before, metadata_before = source.read_bytes(), metadata.read_bytes()
        encoder = shutil.which(os.environ.get("ASTCENC", "astcenc"))
        with contextlib.redirect_stdout(io.StringIO()):
            result = main([str(self.docs), str(self.args.report_path), "--jobs", "1"])
        self.assertEqual(result, 0)
        output = source.with_suffix(".astc")
        info = parse_astc_header(output.read_bytes())
        self.assertEqual((info.width, info.height, info.block_x, info.block_y), (65, 33, 4, 4))
        self.assertEqual(output.stat().st_size, info.expected_size)
        self.assertEqual(json.loads(metadata.read_text())[0]["fmt"], "7@89")
        self.assertEqual(source.read_bytes(), source_before)
        backup = next(self.args.report_path.parent.glob("astc-original-metadata-*"))
        self.assertEqual((backup / metadata.relative_to(self.docs)).read_bytes(), metadata_before)
        self.assertIn("Input AVIF: 1", self.args.report_path.read_text())
        decoded = self.root / "decoded-astc.png"
        subprocess.run([encoder, "-dl", str(output), str(decoded)], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        with Image.open(decoded) as image:
            self.assertEqual(image.size, (65, 33))
            self.assertEqual(image.getchannel("A").getextrema(), (0, 255))


if __name__ == "__main__":
    unittest.main()
