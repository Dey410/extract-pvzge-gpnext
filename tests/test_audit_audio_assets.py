import unittest

from scripts.audit_audio_assets import classify, parse_afinfo, pcm_bytes


class ParseAfInfoTests(unittest.TestCase):
    def test_parses_duration_format_and_bitrate(self):
        text = (
            "File: /tmp/a.mp3\n"
            "File type ID: MPG3\n"
            "Num Tracks: 1\n"
            "Data format: 2 ch,  44100 Hz, 'mp3 ' (0x00000000)\n"
            "estimated duration: 3.006000 sec\n"
            "audio bytes: 27453\n"
            "bit rate: 73081 bits per second\n"
        )
        parsed = parse_afinfo(text)
        self.assertEqual(parsed["duration"], 3.006)
        self.assertEqual(parsed["sample_rate"], 44100)
        self.assertEqual(parsed["channels"], 2)
        self.assertEqual(parsed["bit_rate"], 73081)

    def test_ignores_unparseable_lines(self):
        self.assertEqual(parse_afinfo("not audio\n"), {})


class ClassifyTests(unittest.TestCase):
    def test_short_candidate(self):
        self.assertEqual(
            classify(100_000, 3, 256 * 1024, 10),
            "short_candidate",
        )

    def test_size_over(self):
        self.assertEqual(
            classify(300_000, 3, 256 * 1024, 10),
            "size_over",
        )

    def test_long_candidate(self):
        self.assertEqual(
            classify(100_000, 20, 256 * 1024, 10),
            "long_candidate",
        )

    def test_unreadable(self):
        self.assertEqual(
            classify(0, None, 256 * 1024, 10),
            "unreadable",
        )


class PcmBytesTests(unittest.TestCase):
    def test_stereo_44k_float32(self):
        self.assertEqual(pcm_bytes(1, 44100, 2), 352_800)

    def test_mono_16k_float32(self):
        self.assertEqual(pcm_bytes(1, 16000, 1), 64_000)


if __name__ == "__main__":
    unittest.main()
