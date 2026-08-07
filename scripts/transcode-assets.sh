#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DOCS_DIR="${1:-docs}"
REPORT_PATH="${2:-reports/transcode-summary.txt}"

AVIF_QUALITY="${AVIF_QUALITY:-60}"
AVIF_ALPHA_QUALITY="${AVIF_ALPHA_QUALITY:-80}"
AVIF_SPEED="${AVIF_SPEED:-6}"
TRANSCODE_JOBS="${TRANSCODE_JOBS:-$(nproc)}"
MAX_PERCENT="${MAX_PERCENT:-30}"
ENABLE_PNG_TRANSCODE="${ENABLE_PNG_TRANSCODE:-1}"
# Audio transcoding is lossy and irreversible; keep it opt-in.
ENABLE_MP3_TRANSCODE="${ENABLE_MP3_TRANSCODE:-0}"
AUDIO_TRANSCODE_PROFILE="${AUDIO_TRANSCODE_PROFILE:-compact-150}"
AUDIO_MIN_SIZE_SAVING_RATIO="${AUDIO_MIN_SIZE_SAVING_RATIO:-}"
AUDIO_MIN_EXPECTED_SAVING_RATIO="${AUDIO_MIN_EXPECTED_SAVING_RATIO:-}"
AUDIO_POLICY_SCRIPT="$SCRIPT_DIR/audio_policy.py"
AUDIO_WORKER_SCRIPT="$SCRIPT_DIR/audio_transcode_worker.py"

is_enabled() {
  case "$1" in
    1|true|TRUE|True|yes|YES|Yes|on|ON|On) return 0 ;;
    *) return 1 ;;
  esac
}

if [[ ! -d "$DOCS_DIR" ]]; then
  echo "error: docs directory does not exist: $DOCS_DIR" >&2
  exit 1
fi

required_commands=(find stat grep head awk date)
if is_enabled "$ENABLE_PNG_TRANSCODE"; then
  required_commands+=(avifenc)
fi
if is_enabled "$ENABLE_MP3_TRANSCODE"; then
  required_commands+=(ffmpeg ffprobe python3)
fi
for command_name in "${required_commands[@]}"; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "error: required command is unavailable: $command_name" >&2
    exit 1
  fi
done

if [[ ! "$TRANSCODE_JOBS" =~ ^[1-9][0-9]*$ ]]; then
  echo "error: TRANSCODE_JOBS must be a positive integer" >&2
  exit 1
fi

if [[ ! "$MAX_PERCENT" =~ ^[1-9][0-9]*$ ]] || ((MAX_PERCENT > 100)); then
  echo "error: MAX_PERCENT must be an integer from 1 to 100" >&2
  exit 1
fi

if is_enabled "$ENABLE_MP3_TRANSCODE"; then
  if [[ "$AUDIO_TRANSCODE_PROFILE" != "compact-150" ]]; then
    echo "error: unsupported audio transcode profile: $AUDIO_TRANSCODE_PROFILE" >&2
    exit 1
  fi
  if [[ -z "$AUDIO_MIN_SIZE_SAVING_RATIO" ]]; then
    AUDIO_MIN_SIZE_SAVING_RATIO="$(
      python3 "$AUDIO_POLICY_SCRIPT" config min_size_saving_ratio
    )"
  fi
  if [[ -z "$AUDIO_MIN_EXPECTED_SAVING_RATIO" ]]; then
    AUDIO_MIN_EXPECTED_SAVING_RATIO="$(
      python3 "$AUDIO_POLICY_SCRIPT" config min_expected_saving_ratio
    )"
  fi
  if ! awk -v ratio="$AUDIO_MIN_SIZE_SAVING_RATIO" \
    'BEGIN { exit !(ratio + 0 > 0 && ratio + 0 < 1) }'; then
    echo "error: AUDIO_MIN_SIZE_SAVING_RATIO must be between 0 and 1" >&2
    exit 1
  fi
  if ! awk -v ratio="$AUDIO_MIN_EXPECTED_SAVING_RATIO" \
    'BEGIN { exit !(ratio + 0 > 0 && ratio + 0 < 1) }'; then
    echo "error: AUDIO_MIN_EXPECTED_SAVING_RATIO must be between 0 and 1" >&2
    exit 1
  fi
fi

mkdir -p "$(dirname -- "$REPORT_PATH")"
started_at="$(date +%s)"

has_magic() {
  local file="$1"
  local magic="$2"
  LC_ALL=C head -c 32 -- "$file" | grep -aFq -- "$magic"
}

has_mp3_magic() {
  local file="$1"
  local hex
  hex="$(LC_ALL=C od -An -tx1 -N3 "$file" 2>/dev/null | tr -d ' \n')"
  case "$hex" in
    494433*) return 0 ;;  # ID3 tag
    ff[ef]*) return 0 ;;  # MPEG audio frame sync
    *) return 1 ;;
  esac
}

scan_mp3_after() {
  local file
  local size

  mp3_after_count=0
  mp3_after_bytes=0
  mp3_after_m4a_count=0
  mp3_after_m4a_bytes=0
  mp3_after_mp3_count=0
  mp3_after_mp3_bytes=0
  mp3_after_invalid_count=0

  while IFS= read -r -d '' file; do
    size="$(stat -c '%s' -- "$file")"
    ((mp3_after_count += 1))
    ((mp3_after_bytes += size))

    if has_magic "$file" "ftypM4A"; then
      ((mp3_after_m4a_count += 1))
      ((mp3_after_m4a_bytes += size))
    elif has_mp3_magic "$file"; then
      ((mp3_after_mp3_count += 1))
      ((mp3_after_mp3_bytes += size))
    else
      ((mp3_after_invalid_count += 1))
    fi
  done < <(find "$DOCS_DIR" -type f -iname "*.mp3" -print0)
}

scan_media() {
  local extension="$1"
  local magic="$2"
  local file
  local size

  SCAN_COUNT=0
  SCAN_BYTES=0
  SCAN_ENCODED_COUNT=0
  SCAN_ENCODED_BYTES=0

  while IFS= read -r -d '' file; do
    size="$(stat -c '%s' -- "$file")"
    ((SCAN_COUNT += 1))
    ((SCAN_BYTES += size))

    if has_magic "$file" "$magic"; then
      ((SCAN_ENCODED_COUNT += 1))
      ((SCAN_ENCODED_BYTES += size))
    fi
  done < <(find "$DOCS_DIR" -type f -iname "*${extension}" -print0)
}

png_before_count=0
png_before_bytes=0
png_already_encoded_count=0
png_already_encoded_bytes=0
mp3_before_count=0
mp3_before_bytes=0
mp3_already_encoded_count=0
mp3_already_encoded_bytes=0

if is_enabled "$ENABLE_PNG_TRANSCODE"; then
  scan_media ".png" "ftypavif"
  png_before_count="$SCAN_COUNT"
  png_before_bytes="$SCAN_BYTES"
  png_already_encoded_count="$SCAN_ENCODED_COUNT"
  png_already_encoded_bytes="$SCAN_ENCODED_BYTES"
fi

if is_enabled "$ENABLE_MP3_TRANSCODE"; then
  scan_media ".mp3" "ftypM4A"
  mp3_before_count="$SCAN_COUNT"
  mp3_before_bytes="$SCAN_BYTES"
  mp3_already_encoded_count="$SCAN_ENCODED_COUNT"
  mp3_already_encoded_bytes="$SCAN_ENCODED_BYTES"
fi

if is_enabled "$ENABLE_PNG_TRANSCODE" && ((png_before_count == 0)); then
  echo "error: expected PNG files below $DOCS_DIR" >&2
  exit 1
fi

if is_enabled "$ENABLE_MP3_TRANSCODE" && ((mp3_before_count == 0)); then
  echo "error: expected MP3 files below $DOCS_DIR" >&2
  exit 1
fi

png_to_encode_count=0
png_to_encode_bytes=0
mp3_to_encode_count=0

if is_enabled "$ENABLE_PNG_TRANSCODE"; then
  png_to_encode_count=$((png_before_count - png_already_encoded_count))
  png_to_encode_bytes=$((png_before_bytes - png_already_encoded_bytes))
fi

if is_enabled "$ENABLE_MP3_TRANSCODE"; then
  mp3_to_encode_count=$((mp3_before_count - mp3_already_encoded_count))
fi

AVIFENC_MODERN=0
if is_enabled "$ENABLE_PNG_TRANSCODE" && avifenc --help 2>&1 | grep -q -- "--qcolor"; then
  AVIFENC_MODERN=1
fi

# Ubuntu 24.04 ships libavif 1.0.4, whose CLI uses 0..63 quantizers.
# These values are the nearest equivalents of color quality 60 and alpha 80.
AVIF_COLOR_QUANTIZER=25
AVIF_ALPHA_QUANTIZER=13

export AVIF_QUALITY AVIF_ALPHA_QUALITY AVIF_SPEED
export AVIFENC_MODERN AVIF_COLOR_QUANTIZER AVIF_ALPHA_QUANTIZER

if is_enabled "$ENABLE_MP3_TRANSCODE"; then
  AUDIO_DECISIONS_LOG="${REPORT_PATH}.audio-decisions.log"
  AUDIO_BITRATE_LOG="${REPORT_PATH}.audio-bitrate-distribution.log"
  : > "$AUDIO_DECISIONS_LOG"
  : > "$AUDIO_BITRATE_LOG"
  export AUDIO_WORKER_SCRIPT AUDIO_DECISIONS_LOG AUDIO_TRANSCODE_PROFILE
  export AUDIO_MIN_SIZE_SAVING_RATIO AUDIO_MIN_EXPECTED_SAVING_RATIO
fi

transcode_png() {
  local file="$1"
  local temporary="${file}.transcoding.${BASHPID}.avif"

  if has_magic "$file" "ftypavif"; then
    return 0
  fi

  if [[ "$AVIFENC_MODERN" == "1" ]]; then
    if ! avifenc \
      --jobs 1 \
      --speed "$AVIF_SPEED" \
      --qcolor "$AVIF_QUALITY" \
      --qalpha "$AVIF_ALPHA_QUALITY" \
      -- "$file" "$temporary" >/dev/null; then
      rm -f -- "$temporary"
      return 1
    fi
  else
    if ! avifenc \
      --jobs 1 \
      --speed "$AVIF_SPEED" \
      --min "$AVIF_COLOR_QUANTIZER" \
      --max "$AVIF_COLOR_QUANTIZER" \
      --minalpha "$AVIF_ALPHA_QUANTIZER" \
      --maxalpha "$AVIF_ALPHA_QUANTIZER" \
      -- "$file" "$temporary" >/dev/null; then
      rm -f -- "$temporary"
      return 1
    fi
  fi

  if [[ ! -s "$temporary" ]] || ! has_magic "$temporary" "ftypavif"; then
    echo "error: avifenc produced an invalid file for $file" >&2
    rm -f -- "$temporary"
    return 1
  fi

  mv -f -- "$temporary" "$file"
}

transcode_mp3() {
  local file="$1"
  python3 "$AUDIO_WORKER_SCRIPT" \
    --file "$file" \
    --decisions-log "$AUDIO_DECISIONS_LOG" \
    --profile "$AUDIO_TRANSCODE_PROFILE" \
    --min-size-saving-ratio "$AUDIO_MIN_SIZE_SAVING_RATIO" \
    --min-expected-saving-ratio "$AUDIO_MIN_EXPECTED_SAVING_RATIO"
}

export -f has_magic transcode_png transcode_mp3

if is_enabled "$ENABLE_PNG_TRANSCODE"; then
  echo "Transcoding $png_to_encode_count PNG files with $TRANSCODE_JOBS workers"
  find "$DOCS_DIR" -type f -iname "*.png" -print0 \
    | xargs -0 -r -n 1 -P "$TRANSCODE_JOBS" bash -c 'transcode_png "$1"' _
fi

if is_enabled "$ENABLE_MP3_TRANSCODE"; then
  echo "Applying audio profile $AUDIO_TRANSCODE_PROFILE to $mp3_to_encode_count MP3 files with $TRANSCODE_JOBS workers"
  find "$DOCS_DIR" -type f -iname "*.mp3" -print0 \
    | xargs -0 -r -n 1 -P "$TRANSCODE_JOBS" bash -c 'transcode_mp3 "$1"' _
fi

png_after_count=0
png_after_bytes=0
png_after_encoded_count=0
png_after_encoded_bytes=0
mp3_after_count=0
mp3_after_bytes=0
mp3_after_m4a_count=0
mp3_after_m4a_bytes=0
mp3_after_mp3_count=0
mp3_after_mp3_bytes=0
mp3_after_invalid_count=0
png_new_encoded_bytes=0

if is_enabled "$ENABLE_PNG_TRANSCODE"; then
  scan_media ".png" "ftypavif"
  png_after_count="$SCAN_COUNT"
  png_after_bytes="$SCAN_BYTES"
  png_after_encoded_count="$SCAN_ENCODED_COUNT"
  png_after_encoded_bytes="$SCAN_ENCODED_BYTES"

  if ((png_after_count != png_before_count || png_after_encoded_count != png_after_count)); then
    echo "error: PNG count or AVIF magic validation failed" >&2
    exit 1
  fi
  png_new_encoded_bytes=$((png_after_bytes - png_already_encoded_bytes))
fi

if is_enabled "$ENABLE_MP3_TRANSCODE"; then
  scan_mp3_after

  if ((mp3_after_count != mp3_before_count)); then
    echo "error: MP3 file count changed during transcode" >&2
    exit 1
  fi
  if ((mp3_after_invalid_count != 0)); then
    echo "error: MP3 files are neither MP3 nor M4A/AAC" >&2
    exit 1
  fi
  if ((mp3_after_m4a_count + mp3_after_mp3_count != mp3_after_count)); then
    echo "error: MP3/AAC final validation failed" >&2
    exit 1
  fi
fi

percentage() {
  local after="$1"
  local before="$2"
  awk -v after="$after" -v before="$before" \
    'BEGIN { if (before == 0) print "n/a"; else printf "%.2f%%", after * 100 / before }'
}

if is_enabled "$ENABLE_PNG_TRANSCODE" && ((png_to_encode_count > 0 && png_new_encoded_bytes * 100 > png_to_encode_bytes * MAX_PERCENT)); then
  echo "error: converted AVIF files exceed ${MAX_PERCENT}% of their PNG inputs" \
    "(input=$png_to_encode_bytes bytes, output=$png_new_encoded_bytes bytes," \
    "ratio=$(percentage "$png_new_encoded_bytes" "$png_to_encode_bytes"))" >&2
  exit 1
fi

mp3_decision_total=0
mp3_decision_duration=0
mp3_already_aac_count=0
mp3_keep_gte_count=0
mp3_keep_expected_count=0
mp3_transcoded_count=0
mp3_rejected_count=0
mp3_unreadable_count=0
mp3_accepted_bytes=0

if is_enabled "$ENABLE_MP3_TRANSCODE"; then
  read -r \
    mp3_decision_total \
    mp3_decision_duration \
    mp3_already_aac_count \
    mp3_keep_gte_count \
    mp3_keep_expected_count \
    mp3_transcoded_count \
    mp3_rejected_count \
    mp3_unreadable_count \
    mp3_accepted_bytes <<<"$(
      awk -F '\t' '
        {
          if ($6 != "-") total_duration += $6
          if ($1 == "transcode") {
            transcoded++
            if ($4 != "-") {
              bitrate_count[$4]++
              bitrate_duration[$4] += ($6 != "-" ? $6 : 0)
              accepted_bytes += $5
            }
          } else if ($1 == "already_aac") {
            already_aac++
          } else if ($1 == "keep") {
            if ($2 == "target_bitrate_gte_source") keep_gte++
            else if ($2 == "expected_saving_too_small") keep_expected++
            else if ($2 == "rejected_after_size_compare") rejected++
          } else if ($1 == "unreadable") {
            unreadable++
          }
        }
        END {
          printf "%d %.6f %d %d %d %d %d %d %d\n",
            NR, total_duration, already_aac, keep_gte, keep_expected,
            transcoded, rejected, unreadable, accepted_bytes
        }
      ' "$AUDIO_DECISIONS_LOG"
    )"

  if ((mp3_decision_total != mp3_before_count)); then
    echo "error: audio decision log is incomplete" >&2
    exit 1
  fi
  if ((mp3_accepted_bytes != mp3_after_m4a_bytes - mp3_already_encoded_bytes)); then
    echo "error: accepted AAC byte accounting mismatch" >&2
    exit 1
  fi

  awk -F '\t' '
    $1 == "transcode" && $4 != "-" {
      bitrate = $4
      count[bitrate]++
      duration[bitrate] += ($6 != "-" ? $6 : 0)
    }
    END {
      for (bitrate in count) {
        printf "%d %d %.6f\n", bitrate / 1000, count[bitrate], duration[bitrate]
      }
    }
  ' "$AUDIO_DECISIONS_LOG" | sort -n > "$AUDIO_BITRATE_LOG"
fi

finished_at="$(date +%s)"
elapsed_seconds=$((finished_at - started_at))
avifenc_version="n/a"
ffmpeg_version="n/a"
if is_enabled "$ENABLE_PNG_TRANSCODE"; then
  avifenc_version_output="$(avifenc --version 2>&1)"
  avifenc_version="${avifenc_version_output%%$'\n'*}"
fi
if is_enabled "$ENABLE_MP3_TRANSCODE"; then
  ffmpeg_version_output="$(ffmpeg -version 2>&1)"
  ffmpeg_version="${ffmpeg_version_output%%$'\n'*}"
fi

{
  echo "PvZGE in-place media transcode summary"
  echo ""
  echo "Settings:"
  if is_enabled "$ENABLE_PNG_TRANSCODE"; then
    echo "  PNG transcode: enabled"
  else
    echo "  PNG transcode: disabled"
  fi
  if is_enabled "$ENABLE_MP3_TRANSCODE"; then
    echo "  MP3 transcode: enabled"
    echo "  Audio transcode profile: $AUDIO_TRANSCODE_PROFILE"
    echo "  AAC codec/profile: AAC-LC (native ffmpeg)"
    echo "  Minimum size saving threshold: $(awk -v r="$AUDIO_MIN_SIZE_SAVING_RATIO" 'BEGIN { printf "%.0f%%", r * 100 }')"
    echo "  Minimum expected saving threshold: $(awk -v r="$AUDIO_MIN_EXPECTED_SAVING_RATIO" 'BEGIN { printf "%.0f%%", r * 100 }')"
    echo "  Sample rate policy: preserve input sample rate, no upsampling"
    echo "  Channel policy: preserve input channels, max 2"
  else
    echo "  MP3 transcode: disabled"
    echo "  Audio transcode profile: $AUDIO_TRANSCODE_PROFILE (unused)"
    echo "  AAC codec/profile: AAC-LC (unused)"
    echo "  Minimum size saving threshold: n/a"
    echo "  Minimum expected saving threshold: n/a"
    echo "  Sample rate policy: preserve input sample rate (unused)"
    echo "  Channel policy: preserve input channels, max 2 (unused)"
  fi
  echo "  AVIF color quality: $AVIF_QUALITY"
  echo "  AVIF alpha quality: $AVIF_ALPHA_QUALITY"
  echo "  AVIF speed: $AVIF_SPEED"
  echo "  Parallel workers: $TRANSCODE_JOBS"
  echo "  Maximum PNG converted/input ratio: $MAX_PERCENT%"
  echo ""
  if is_enabled "$ENABLE_PNG_TRANSCODE"; then
    echo "PNG -> AVIF (original .png paths retained):"
    echo "  Files: $png_before_count"
    echo "  Newly converted: $png_to_encode_count"
    echo "  Before bytes: $png_before_bytes"
    echo "  After bytes: $png_after_bytes"
    echo "  After / before: $(percentage "$png_after_bytes" "$png_before_bytes")"
  else
    echo "PNG -> AVIF (original .png paths retained): disabled"
  fi
  echo ""
  if is_enabled "$ENABLE_MP3_TRANSCODE"; then
    echo "MP3 -> M4A/AAC (original .mp3 paths retained):"
    echo "  Before files: $mp3_before_count"
    echo "  Before bytes: $mp3_before_bytes"
    echo "  Already AAC before: $mp3_already_encoded_count"
    echo ""
    echo "Decisions:"
    echo "  Already AAC (no re-encode): $mp3_already_aac_count"
    echo "  Kept MP3 (target bitrate >= source): $mp3_keep_gte_count"
    echo "  Kept MP3 (expected saving too small): $mp3_keep_expected_count"
    echo "  Transcoded: $mp3_transcoded_count"
    echo "  Rejected after actual size comparison: $mp3_rejected_count"
    echo "  Kept MP3 (unreadable analysis, safe keep): $mp3_unreadable_count"
    echo ""
    echo "Target bitrate distribution (accepted transcodes):"
    if [[ -s "$AUDIO_BITRATE_LOG" ]]; then
      while read -r kbps count duration_sum; do
        printf '  %sk: %s files / %.1f s\n' "$kbps" "$count" "$duration_sum"
      done < "$AUDIO_BITRATE_LOG"
    else
      echo "  (none)"
    fi
    echo ""
    echo "Result:"
    echo "  Final MP3 count: $mp3_after_mp3_count"
    echo "  Final AAC/M4A count: $mp3_after_m4a_count"
    echo "  MP3 bytes: $mp3_after_mp3_bytes"
    echo "  AAC bytes: $mp3_after_m4a_bytes"
    echo "  Total bytes: $mp3_after_bytes"
    echo "  Saved bytes: $((mp3_before_bytes - mp3_after_bytes))"
    echo "  After / before: $(percentage "$mp3_after_bytes" "$mp3_before_bytes")"
    if awk -v d="$mp3_decision_duration" 'BEGIN { exit !(d > 0) }'; then
      estimated_kbps="$(
        awk -v bytes="$mp3_after_bytes" -v duration="$mp3_decision_duration" \
          'BEGIN { printf "%.1f", bytes * 8 / duration / 1000 }'
      )"
      echo "  Estimated average bitrate: ${estimated_kbps} kbps"
    else
      echo "  Estimated average bitrate: n/a"
    fi
  else
    echo "MP3 -> M4A/AAC (original .mp3 paths retained): disabled"
  fi
  echo ""
  echo "Elapsed seconds: $elapsed_seconds"
  echo "avifenc: $avifenc_version"
  echo "ffmpeg: $ffmpeg_version"
} >"$REPORT_PATH"

cat "$REPORT_PATH"
