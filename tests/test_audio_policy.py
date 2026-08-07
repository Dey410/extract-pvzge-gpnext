import unittest

from scripts.audio_policy import (
    MIN_EXPECTED_SAVING_RATIO,
    MIN_SIZE_SAVING_RATIO,
    AudioPolicyError,
    select_audio_policy,
)


def policy(duration, sample_rate, channels, bitrate, **extra):
    meta = {
        "duration": duration,
        "sample_rate": sample_rate,
        "channels": channels,
        "bitrate": bitrate,
        "input_size": 1024,
        **extra,
    }
    return select_audio_policy(meta)


class Compact150PolicyMatrixTests(unittest.TestCase):
    def test_short_mono_16k(self):
        result = policy(1, 16000, 1, 160000)
        self.assertEqual(result["action"], "transcode")
        self.assertEqual(result["target_bitrate"], 32000)
        self.assertEqual(result["target_sample_rate"], 16000)
        self.assertEqual(result["target_channels"], 1)
        self.assertEqual(result["policy_class"], "short_mono_32k")

    def test_short_mono_24k(self):
        result = policy(1, 24000, 1, 160000)
        self.assertEqual(result["target_bitrate"], 40000)

    def test_short_mono_32k(self):
        result = policy(1, 32000, 1, 320000)
        self.assertEqual(result["target_bitrate"], 48000)

    def test_short_mono_48k(self):
        result = policy(1, 48000, 1, 320000)
        self.assertEqual(result["target_bitrate"], 56000)

    def test_short_stereo_32k(self):
        result = policy(1, 32000, 2, 320000)
        self.assertEqual(result["target_bitrate"], 56000)
        self.assertEqual(result["target_channels"], 2)

    def test_short_stereo_48k(self):
        result = policy(1, 48000, 2, 320000)
        self.assertEqual(result["target_bitrate"], 64000)

    def test_medium_stereo_48k(self):
        result = policy(10, 48000, 2, 128000)
        self.assertEqual(result["target_bitrate"], 72000)
        self.assertEqual(result["policy_class"], "medium_stereo_72k")

    def test_long_stereo_32k_96k(self):
        result = policy(60, 32000, 2, 96000)
        self.assertEqual(result["target_bitrate"], 72000)

    def test_long_stereo_48k_96k(self):
        result = policy(180, 48000, 2, 96000)
        self.assertEqual(result["target_bitrate"], 72000)

    def test_long_stereo_48k_128k(self):
        result = policy(180, 48000, 2, 128000)
        self.assertEqual(result["target_bitrate"], 80000)

    def test_long_stereo_48k_320k(self):
        result = policy(180, 48000, 2, 320000)
        self.assertEqual(result["target_bitrate"], 96000)

    def test_long_mono_uses_sample_rate(self):
        result = policy(180, 24000, 1, 128000)
        self.assertEqual(result["target_bitrate"], 48000)
        result = policy(180, 48000, 1, 128000)
        self.assertEqual(result["target_bitrate"], 64000)

    def test_keeps_when_target_bitrate_gte_source(self):
        result = policy(60, 32000, 2, 64000)
        self.assertEqual(result["action"], "keep")
        self.assertEqual(result["reason"], "target_bitrate_gte_source")
        self.assertEqual(result["target_bitrate"], 72000)

    def test_keeps_when_expected_saving_too_small(self):
        result = policy(60, 32000, 1, 60000)
        self.assertEqual(result["action"], "keep")
        self.assertEqual(result["reason"], "expected_saving_too_small")
        self.assertLess(result["expected_saving"], MIN_EXPECTED_SAVING_RATIO)

    def test_preserves_input_sample_rate(self):
        for sample_rate in (16000, 24000, 32000, 44100, 48000):
            result = policy(1, sample_rate, 1, 320000)
            self.assertEqual(result["target_sample_rate"], sample_rate)

    def test_limits_channels_to_stereo(self):
        result = policy(1, 48000, 6, 320000)
        self.assertEqual(result["target_channels"], 2)

    def test_rejects_missing_duration(self):
        with self.assertRaises(AudioPolicyError):
            policy(None, 48000, 2, 128000)

    def test_rejects_invalid_channels(self):
        with self.assertRaises(AudioPolicyError):
            policy(1, 48000, 0, 128000)

    def test_rejects_unknown_profile(self):
        with self.assertRaises(AudioPolicyError):
            select_audio_policy(
                {
                    "duration": 1,
                    "sample_rate": 48000,
                    "channels": 1,
                    "bitrate": 128000,
                },
                profile="unknown",
            )

    def test_size_saving_constant_is_ten_percent(self):
        self.assertEqual(MIN_SIZE_SAVING_RATIO, 0.10)
        self.assertEqual(MIN_EXPECTED_SAVING_RATIO, 0.10)


if __name__ == "__main__":
    unittest.main()
