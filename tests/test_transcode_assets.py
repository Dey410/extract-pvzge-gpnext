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
  [[ "$arg" == "--" ]] || args+=("$arg")
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
printf 'PADDING' >> "$out"
""",
        )
        write_stub(
            self.bin / "ffprobe",
            """#!/usr/bin/env bash
printf '44100\\n'
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

    def test_default_behavior_transcodes_png_and_mp3(self):
        png = self.docs / "sprite.png"
        png.write_bytes(b"P" * 300)
        mp3 = self.docs / "bgm.mp3"
        mp3.write_bytes(b"M" * 400)

        result = self.run_script()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(png.read_bytes().startswith(b"ftypavif"))
        self.assertTrue(mp3.read_bytes().startswith(b"ftypM4A"))
        self.assertTrue((self.logs / "ffmpeg.log").exists())
        report = self.report.read_text(encoding="utf-8")
        self.assertIn("PNG transcode: enabled", report)
        self.assertIn("MP3 transcode: enabled", report)
        self.assertIn("Newly converted: 1", report)


if __name__ == "__main__":
    unittest.main()
