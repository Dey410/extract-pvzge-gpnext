#!/usr/bin/env python3
"""Central MP3 -> AAC-LC policy selection for the extraction workflow.

The shell orchestrator (`transcode-assets.sh`) keeps the per-file execution
model, while this module owns every threshold used to decide whether an MP3
should be transcoded and at which AAC bitrate/sample rate/channel count.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Optional


PROFILE_COMPACT_150 = "compact-150"
SUPPORTED_PROFILES = frozenset({PROFILE_COMPACT_150})

# A transcode must be expected to save at least 10% before it is attempted.
MIN_EXPECTED_SAVING_RATIO = 0.10
# After encoding, the temporary AAC is accepted only when it actually saves at
# least 10% against the original MP3.  This is the "至少节省约 10%" gate.
MIN_SIZE_SAVING_RATIO = 0.10
MAX_CHANNELS = 2


class AudioPolicyError(ValueError):
    """Raised when the probe metadata cannot drive a policy decision."""


def _int_or_none(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _compact_150_targets(
    duration: float,
    sample_rate: int,
    channels: int,
    bitrate: Optional[int],
) -> tuple[int, int, int, str]:
    """Return (target bitrate bps, target sample rate, target channels, class)."""
    target_channels = min(channels, MAX_CHANNELS)
    # AAC file size is controlled by the target bitrate; do not upsample and
    # do not lower the sample rate just to shrink the file.
    target_sample_rate = sample_rate
    channel_label = "mono" if target_channels == 1 else "stereo"

    if duration < 5:
        duration_label = "short"
        if target_channels == 1:
            if sample_rate <= 16000:
                target_bitrate = 32000
            elif sample_rate <= 24000:
                target_bitrate = 40000
            elif sample_rate <= 32000:
                target_bitrate = 48000
            else:
                target_bitrate = 56000
        elif sample_rate <= 24000:
            target_bitrate = 48000
        elif sample_rate <= 32000:
            target_bitrate = 56000
        else:
            target_bitrate = 64000
    elif duration < 30:
        duration_label = "medium"
        if target_channels == 1:
            if sample_rate <= 16000:
                target_bitrate = 32000
            elif sample_rate <= 24000:
                target_bitrate = 40000
            elif sample_rate <= 32000:
                target_bitrate = 48000
            else:
                target_bitrate = 56000
        elif sample_rate <= 24000:
            target_bitrate = 48000
        elif sample_rate <= 32000:
            target_bitrate = 56000
        else:
            target_bitrate = 72000
    else:
        duration_label = "long"
        if target_channels == 1:
            if sample_rate <= 16000:
                target_bitrate = 40000
            elif sample_rate <= 24000:
                target_bitrate = 48000
            elif sample_rate <= 32000:
                target_bitrate = 56000
            else:
                target_bitrate = 64000
        elif bitrate is None or bitrate <= 0:
            # Long stereo with an unreliable bitrate: pick the most
            # conservative tier and let the actual-size gate decide.
            target_bitrate = 72000
        elif bitrate <= 96000:
            target_bitrate = 72000
        elif bitrate <= 128000:
            target_bitrate = 80000
        else:
            target_bitrate = 96000

    policy_class = f"{duration_label}_{channel_label}_{target_bitrate // 1000}k"
    return target_bitrate, target_sample_rate, target_channels, policy_class


def select_audio_policy(
    meta: dict[str, Any],
    profile: str = PROFILE_COMPACT_150,
    min_expected_saving_ratio: float = MIN_EXPECTED_SAVING_RATIO,
) -> dict[str, Any]:
    """Choose keep/transcode for one audio asset.

    Expected input keys: duration, sample_rate, channels, bitrate,
    input_size.  Returns the policy object used by the transcoding worker and
    by tests.
    """
    if profile not in SUPPORTED_PROFILES:
        raise AudioPolicyError(f"unsupported audio profile: {profile}")

    duration = _float_or_none(meta.get("duration"))
    sample_rate = _int_or_none(meta.get("sample_rate"))
    channels = _int_or_none(meta.get("channels"))
    bitrate = _int_or_none(meta.get("bitrate"))

    if duration is None or duration <= 0:
        raise AudioPolicyError("duration is missing or not positive")
    if sample_rate is None or sample_rate <= 0:
        raise AudioPolicyError("sample rate is missing or not positive")
    if channels is None or channels <= 0:
        raise AudioPolicyError("channels is missing or not positive")

    target_bitrate, target_sample_rate, target_channels, policy_class = (
        _compact_150_targets(duration, sample_rate, channels, bitrate)
    )

    expected_saving = None
    reason = policy_class
    action = "transcode"
    if bitrate is not None and bitrate > 0:
        expected_saving = round(1.0 - target_bitrate / bitrate, 6)
        if expected_saving <= 0:
            action = "keep"
            reason = "target_bitrate_gte_source"
        elif expected_saving < min_expected_saving_ratio:
            action = "keep"
            reason = "expected_saving_too_small"

    return {
        "action": action,
        "reason": reason,
        "policy_class": policy_class,
        "profile": profile,
        "target_bitrate": target_bitrate,
        "target_sample_rate": target_sample_rate,
        "target_channels": target_channels,
        "expected_saving": expected_saving,
    }


def _cmd_config(args: argparse.Namespace) -> int:
    if args.key == "min_size_saving_ratio":
        print(f"{MIN_SIZE_SAVING_RATIO:.2f}")
    elif args.key == "min_expected_saving_ratio":
        print(f"{MIN_EXPECTED_SAVING_RATIO:.2f}")
    else:
        print(f"error: unknown config key: {args.key}", file=sys.stderr)
        return 1
    return 0


def _cmd_select(args: argparse.Namespace) -> int:
    meta = {
        "duration": _float_or_none(args.duration),
        "sample_rate": _int_or_none(args.sample_rate),
        "channels": _int_or_none(args.channels),
        "bitrate": _int_or_none(args.bitrate),
        "input_size": _int_or_none(args.input_size),
    }
    try:
        policy = select_audio_policy(
            meta,
            profile=args.profile,
            min_expected_saving_ratio=float(args.min_expected_saving_ratio),
        )
    except (AudioPolicyError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(policy, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    config_parser = subparsers.add_parser("config", help="print a policy constant")
    config_parser.add_argument("key", choices=["min_size_saving_ratio", "min_expected_saving_ratio"])
    config_parser.set_defaults(func=_cmd_config)

    select_parser = subparsers.add_parser("select", help="select a policy for one asset")
    select_parser.add_argument("--duration", required=True)
    select_parser.add_argument("--sample-rate", required=True)
    select_parser.add_argument("--channels", required=True)
    select_parser.add_argument("--bitrate", required=False)
    select_parser.add_argument("--input-size", required=False)
    select_parser.add_argument("--profile", default=PROFILE_COMPACT_150)
    select_parser.add_argument(
        "--min-expected-saving-ratio",
        default=str(MIN_EXPECTED_SAVING_RATIO),
    )
    select_parser.set_defaults(func=_cmd_select)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
