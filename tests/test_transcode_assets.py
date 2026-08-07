import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
TRANSCODE_SCRIPT = REPO_ROOT / "scripts" / "transcode-assets.sh"


def write_stub(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def write_mp3(path: Path, size: int = 400) -> None:
    path.write_bytes(b"ID3" + b"M" * max(0, size - 3))


class TranscodeAssetsScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.docs = self.root / "docs"
        self.docs.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.logs = self.root / "logs"
        self.logs.mkdir()
        self.report = self.root / "transcode-summary.txt"
        self._install_stubs()

        env = os.environ.copy()
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        env["STUB_LOG_DIR"] = str(self.logs)
        env["TRANSCODE_JOBS"] = "2"
        self.env = env

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, **extra_env):
        env = dict(self.env)
        env.update(extra_env)
        return subprocess.run(
            ["bash", str(TRANSCODE_SCRIPT), str(self.docs), str(self.report)],
            cwd=self.root,
            env=env,
            text=True,
            capture_output=True,
        )

    def _install_stubs(self):
        # GNU-style `stat -c '%s' -- file`, which BSD stat does not support.
        write_stub(
            self.bin / "stat",
            """#!/usr/bin/env bash
file=""
for arg in "$@"; do
  [[ "$arg" == -* ]] || file="$arg"
done
wc -c < "$file"
""",
        )
        # BSD grep does not accept `--`; strip it before delegating.
        write_stub(
            self.bin / "grep",
            """#!/usr/bin/env bash
args=()
for arg in "$@"; do
  if [[ "$arg" == "--" ]]; then
    continue
  elif [[ "$arg" == --* ]]; then
    args+=("-e" "${arg#--}")
  else
    args+=("$arg")
  fi
done
exec /usr/bin/grep "${args[@]}"
""",
        )
        write_stub(
            self.bin / "avifenc",
            """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${STUB_LOG_DIR:?}/avifenc.log"
for arg in "$@"; do
  case "$arg" in
    --help) printf '%s\\n' "--qcolor"; exit 0 ;;
    --version) printf '%s\\n' "stub-avifenc 1.0"; exit 0 ;;
  esac
done
out=""
for arg in "$@"; do
  out="$arg"
done
printf 'ftypavif' > "$out"
printf 'PADDING' >> "$out"
""",
        )
        write_stub(
            self.bin / "ffmpeg",
            """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${STUB_LOG_DIR:?}/ffmpeg.log"
for arg in "$@"; do
  if [[ "$arg" == "-version" || "$arg" == "--version" ]]; then
    printf '%s\\n' "stub-ffmpeg 6.0"
    exit 0
  fi
done
out=""
for arg in "$@"; do
  out="$arg"
done
printf 'ftypM4A' > "$out"
if [[ "$*" == *"reject.mp3"* ]]; then
  dd if=/dev/zero bs=950 count=1 2>/dev/null >> "$out"
else
  printf 'PADDING' >> "$out"
fi
""",
        )
        write_stub(
            self.bin / "ffprobe",
            """#!/usr/bin/env bash
case "$*" in
  *"high.mp3"*)
    printf '%s\\n' '{"streams":[{"codec_type":"audio","duration":"6.0","sample_rate":"48000","channels":2,"bit_rate":"320000"}],"format":{"duration":"6.0","bit_rate":"320000"}}'
    ;;
  *"low.mp3"*)
    printf '%s\\n' '{"streams":[{"codec_type":"audio","duration":"1.0","sample_rate":"16000","channels":1,"bit_rate":"160000"}],"format":{"duration":"1.0","bit_rate":"160000"}}'
    ;;
  *"keep.mp3"*)
    printf '%s\\n' '{"streams":[{"codec_type":"audio","duration":"60.0","sample_rate":"32000","channels":2,"bit_rate":"64000"}],"format":{"duration":"60.0","bit_rate":"64000"}}'
    ;;
  *"expected.mp3"*)
    printf '%s\\n' '{"streams":[{"codec_type":"audio","duration":"60.0","sample_rate":"32000","channels":1,"bit_rate":"60000"}],"format":{"duration":"60.0","bit_rate":"60000"}}'
    ;;
  *"reject.mp3"*)
    printf '%s\\n' '{"streams":[{"codec_type":"audio","duration":"60.0","sample_rate":"48000","channels":2,"bit_rate":"320000"}],"format":{"duration":"60.0","bit_rate":"320000"}}'
    ;;
  *"already.mp3"*)
    printf '%s\\n' '{"streams":[{"codec_type":"audio","duration":"5.0","sample_rate":"44100","channels":1,"bit_rate":"96000"}],"format":{"duration":"5.0","bit_rate":"96000"}}'
    ;;
  *)
    printf '%s\\n' '{"streams":[{"codec_type":"audio","duration":"2.0","sample_rate":"44100","channels":1,"bit_rate":"128000"}],"format":{"duration":"2.0","bit_rate":"128000"}}'
    ;;
esac
""",
        )

    def test_mp3_transcode_can_be_disabled_independently(self):
        png = self.docs / "sprite.png"
        png.write_bytes(b"P" * 300)
        mp3 = self.docs / "bgm.mp3"
        mp3.write_bytes(b"M" * 400)

        # No ffmpeg/ffprobe available at all: with the MP3 switch off they
        # must not be required, and MP3 files must be left untouched.
        (self.bin / "ffmpeg").unlink()
        (self.bin / "ffprobe").unlink()

        result = self.run_script(ENABLE_MP3_TRANSCODE="0")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.logs / "avifenc.log").exists())
        self.assertFalse((self.logs / "ffmpeg.log").exists())
        self.assertTrue(png.read_bytes().startswith(b"ftypavif"))
        self.assertEqual(mp3.read_bytes(), b"M" * 400)
        report = self.report.read_text(encoding="utf-8")
        self.assertIn("MP3 transcode: disabled", report)
        self.assertIn(
            "MP3 -> M4A/AAC (original .mp3 paths retained): disabled", report
        )

    def test_default_behavior_is_mp3_safe_opt_in(self):
        png = self.docs / "sprite.png"
        png.write_bytes(b"P" * 300)
        mp3 = self.docs / "bgm.mp3"
        mp3.write_bytes(b"M" * 400)

        (self.bin / "ffmpeg").unlink()
        (self.bin / "ffprobe").unlink()

        result = self.run_script()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.logs / "ffmpeg.log").exists())
        self.assertEqual(mp3.read_bytes(), b"M" * 400)
        report = self.report.read_text(encoding="utf-8")
        self.assertIn("MP3 transcode: disabled", report)
        self.assertIn("Audio transcode profile: compact-150 (unused)", report)

    def test_enabled_transcode_applies_compact_150_and_accepts_output(self):
        png = self.docs / "sprite.png"
        png.write_bytes(b"P" * 300)
        high = self.docs / "high.mp3"
        write_mp3(high)
        low = self.docs / "low.mp3"
        write_mp3(low)

        result = self.run_script(ENABLE_MP3_TRANSCODE="1")

        self.assertEqual(result.returncode, 0, result.stderr)
        log = (self.logs / "ffmpeg.log").read_text(encoding="utf-8")
        high_line = next(
            line for line in log.splitlines() if "high.mp3" in line
        )
        low_line = next(
            line for line in log.splitlines() if "low.mp3" in line
        )
        # 6 s / 48 kHz stereo / 320 kbps -> medium stereo 72k, keep 48 kHz.
        self.assertIn("-b:a 72k", high_line)
        self.assertIn("-ar 48000", high_line)
        self.assertIn("-ac 2", high_line)
        # 1 s / 16 kHz mono / 160 kbps -> short mono 32k, keep 16 kHz.
        self.assertIn("-b:a 32k", low_line)
        self.assertIn("-ar 16000", low_line)
        self.assertIn("-ac 1", low_line)
        self.assertTrue(high.read_bytes().startswith(b"ftypM4A"))
        self.assertTrue(low.read_bytes().startswith(b"ftypM4A"))
        report = self.report.read_text(encoding="utf-8")
        self.assertIn("Audio transcode profile: compact-150", report)
        self.assertIn("Transcoded: 2", report)
        self.assertIn("Final AAC/M4A count: 2", report)
        self.assertIn("72k: 1 files / 6.0 s", report)
        self.assertIn("32k: 1 files / 1.0 s", report)

    def test_keeps_mp3_when_target_bitrate_gte_source(self):
        mp3 = self.docs / "keep.mp3"
        original = b"ID3" + b"K" * 397
        mp3.write_bytes(original)

        result = self.run_script(
            ENABLE_MP3_TRANSCODE="1",
            ENABLE_PNG_TRANSCODE="0",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(mp3.read_bytes(), original)
        log = (self.logs / "ffmpeg.log").read_text(encoding="utf-8")
        self.assertNotIn("keep.mp3", log)
        report = self.report.read_text(encoding="utf-8")
        self.assertIn(
            "Kept MP3 (target bitrate >= source): 1", report
        )

    def test_keeps_mp3_when_expected_saving_too_small(self):
        mp3 = self.docs / "expected.mp3"
        original = b"ID3" + b"E" * 397
        mp3.write_bytes(original)

        result = self.run_script(
            ENABLE_MP3_TRANSCODE="1",
            ENABLE_PNG_TRANSCODE="0",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(mp3.read_bytes(), original)
        log = (self.logs / "ffmpeg.log").read_text(encoding="utf-8")
        self.assertNotIn("expected.mp3", log)
        report = self.report.read_text(encoding="utf-8")
        self.assertIn(
            "Kept MP3 (expected saving too small): 1", report
        )

    def test_rejects_temporary_aac_when_actual_saving_too_small(self):
        mp3 = self.docs / "reject.mp3"
        original = b"ID3" + b"R" * 997
        mp3.write_bytes(original)

        result = self.run_script(
            ENABLE_MP3_TRANSCODE="1",
            ENABLE_PNG_TRANSCODE="0",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(mp3.read_bytes(), original)
        log = (self.logs / "ffmpeg.log").read_text(encoding="utf-8")
        self.assertIn("reject.mp3", log)
        report = self.report.read_text(encoding="utf-8")
        self.assertIn(
            "Rejected after actual size comparison: 1", report
        )
        self.assertIn("Final MP3 count: 1", report)
        self.assertIn("Final AAC/M4A count: 0", report)

    def test_already_aac_is_not_reencoded(self):
        mp3 = self.docs / "already.mp3"
        original = b"ftypM4A" + b"A" * 400
        mp3.write_bytes(original)

        result = self.run_script(
            ENABLE_MP3_TRANSCODE="1",
            ENABLE_PNG_TRANSCODE="0",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(mp3.read_bytes(), original)
        log = (self.logs / "ffmpeg.log").read_text(encoding="utf-8")
        self.assertNotIn("already.mp3", log)
        report = self.report.read_text(encoding="utf-8")
        self.assertIn("Already AAC (no re-encode): 1", report)

    def test_final_resources_can_mix_mp3_and_aac(self):
        keep = self.docs / "keep.mp3"
        write_mp3(keep)
        already = self.docs / "already.mp3"
        already.write_bytes(b"ftypM4A" + b"A" * 400)
        high = self.docs / "high.mp3"
        write_mp3(high)

        result = self.run_script(
            ENABLE_MP3_TRANSCODE="1",
            ENABLE_PNG_TRANSCODE="0",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.report.read_text(encoding="utf-8")
        self.assertIn("Final MP3 count: 1", report)
        self.assertIn("Final AAC/M4A count: 2", report)
        self.assertIn("Kept MP3 (target bitrate >= source): 1", report)
        self.assertIn("Already AAC (no re-encode): 1", report)
        self.assertIn("Transcoded: 1", report)


if __name__ == "__main__":
    unittest.main()
