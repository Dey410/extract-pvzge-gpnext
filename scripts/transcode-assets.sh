#!/usr/bin/env bash

set -euo pipefail

DOCS_DIR="${1:-docs}"
REPORT_PATH="${2:-reports/transcode-summary.txt}"

AVIF_QUALITY="${AVIF_QUALITY:-60}"
AVIF_ALPHA_QUALITY="${AVIF_ALPHA_QUALITY:-80}"
AVIF_SPEED="${AVIF_SPEED:-6}"
AAC_BITRATE="${AAC_BITRATE:-24k}"
TRANSCODE_JOBS="${TRANSCODE_JOBS:-$(nproc)}"
MAX_PERCENT="${MAX_PERCENT:-30}"

if [[ ! -d "$DOCS_DIR" ]]; then
  echo "error: docs directory does not exist: $DOCS_DIR" >&2
  exit 1
fi

for command_name in avifenc ffmpeg ffprobe find stat grep head awk date; do
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

mkdir -p "$(dirname -- "$REPORT_PATH")"
started_at="$(date +%s)"

has_magic() {
  local file="$1"
  local magic="$2"
  LC_ALL=C head -c 32 -- "$file" | grep -aFq -- "$magic"
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

scan_media ".png" "ftypavif"
png_before_count="$SCAN_COUNT"
png_before_bytes="$SCAN_BYTES"
png_already_encoded_count="$SCAN_ENCODED_COUNT"
png_already_encoded_bytes="$SCAN_ENCODED_BYTES"

scan_media ".mp3" "ftypM4A"
mp3_before_count="$SCAN_COUNT"
mp3_before_bytes="$SCAN_BYTES"
mp3_already_encoded_count="$SCAN_ENCODED_COUNT"
mp3_already_encoded_bytes="$SCAN_ENCODED_BYTES"

if ((png_before_count == 0 || mp3_before_count == 0)); then
  echo "error: expected both PNG and MP3 files below $DOCS_DIR" >&2
  exit 1
fi

png_to_encode_count=$((png_before_count - png_already_encoded_count))
png_to_encode_bytes=$((png_before_bytes - png_already_encoded_bytes))
mp3_to_encode_count=$((mp3_before_count - mp3_already_encoded_count))
mp3_to_encode_bytes=$((mp3_before_bytes - mp3_already_encoded_bytes))

if avifenc --help 2>&1 | grep -q -- "--qcolor"; then
  AVIFENC_MODERN=1
else
  AVIFENC_MODERN=0
fi

# Ubuntu 24.04 ships libavif 1.0.4, whose CLI uses 0..63 quantizers.
# These values are the nearest equivalents of color quality 60 and alpha 80.
AVIF_COLOR_QUANTIZER=25
AVIF_ALPHA_QUANTIZER=13

export AVIF_QUALITY AVIF_ALPHA_QUALITY AVIF_SPEED
export AVIFENC_MODERN AVIF_COLOR_QUANTIZER AVIF_ALPHA_QUANTIZER
export AAC_BITRATE

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
  local temporary="${file}.transcoding.${BASHPID}.m4a"
  local input_sample_rate
  local output_sample_rate

  if has_magic "$file" "ftypM4A"; then
    return 0
  fi

  if ! input_sample_rate="$(
    ffprobe \
      -v error \
      -select_streams a:0 \
      -show_entries stream=sample_rate \
      -of default=noprint_wrappers=1:nokey=1 \
      "$file"
  )"; then
    echo "error: ffprobe could not read the sample rate for $file" >&2
    return 1
  fi

  if [[ ! "$input_sample_rate" =~ ^[1-9][0-9]*$ ]]; then
    echo "error: invalid sample rate '$input_sample_rate' for $file" >&2
    return 1
  fi

  if ((input_sample_rate > 32000)); then
    output_sample_rate=$((input_sample_rate / 2))
  else
    output_sample_rate=16000
  fi

  if ! ffmpeg \
    -nostdin \
    -hide_banner \
    -loglevel error \
    -y \
    -i "$file" \
    -map 0:a:0 \
    -vn \
    -c:a aac \
    -profile:a aac_low \
    -b:a "$AAC_BITRATE" \
    -ac 1 \
    -ar "$output_sample_rate" \
    -threads 1 \
    -map_metadata -1 \
    -movflags +faststart \
    -f ipod \
    "$temporary"; then
    rm -f -- "$temporary"
    return 1
  fi

  if [[ ! -s "$temporary" ]] || ! has_magic "$temporary" "ftypM4A"; then
    echo "error: ffmpeg produced an invalid file for $file" >&2
    rm -f -- "$temporary"
    return 1
  fi

  mv -f -- "$temporary" "$file"
}

export -f has_magic transcode_png transcode_mp3

echo "Transcoding $png_to_encode_count PNG files with $TRANSCODE_JOBS workers"
find "$DOCS_DIR" -type f -iname "*.png" -print0 \
  | xargs -0 -r -n 1 -P "$TRANSCODE_JOBS" bash -c 'transcode_png "$1"' _

echo "Transcoding $mp3_to_encode_count MP3 files with $TRANSCODE_JOBS workers"
find "$DOCS_DIR" -type f -iname "*.mp3" -print0 \
  | xargs -0 -r -n 1 -P "$TRANSCODE_JOBS" bash -c 'transcode_mp3 "$1"' _

scan_media ".png" "ftypavif"
png_after_count="$SCAN_COUNT"
png_after_bytes="$SCAN_BYTES"
png_after_encoded_count="$SCAN_ENCODED_COUNT"
png_after_encoded_bytes="$SCAN_ENCODED_BYTES"

scan_media ".mp3" "ftypM4A"
mp3_after_count="$SCAN_COUNT"
mp3_after_bytes="$SCAN_BYTES"
mp3_after_encoded_count="$SCAN_ENCODED_COUNT"
mp3_after_encoded_bytes="$SCAN_ENCODED_BYTES"

if ((png_after_count != png_before_count || png_after_encoded_count != png_after_count)); then
  echo "error: PNG count or AVIF magic validation failed" >&2
  exit 1
fi

if ((mp3_after_count != mp3_before_count || mp3_after_encoded_count != mp3_after_count)); then
  echo "error: MP3 count or M4A magic validation failed" >&2
  exit 1
fi

png_new_encoded_bytes=$((png_after_bytes - png_already_encoded_bytes))
mp3_new_encoded_bytes=$((mp3_after_bytes - mp3_already_encoded_bytes))

percentage() {
  local after="$1"
  local before="$2"
  awk -v after="$after" -v before="$before" \
    'BEGIN { if (before == 0) print "n/a"; else printf "%.2f%%", after * 100 / before }'
}

if ((png_to_encode_count > 0 && png_new_encoded_bytes * 100 > png_to_encode_bytes * MAX_PERCENT)); then
  echo "error: converted AVIF files exceed ${MAX_PERCENT}% of their PNG inputs" \
    "(input=$png_to_encode_bytes bytes, output=$png_new_encoded_bytes bytes," \
    "ratio=$(percentage "$png_new_encoded_bytes" "$png_to_encode_bytes"))" >&2
  exit 1
fi

if ((mp3_to_encode_count > 0 && mp3_new_encoded_bytes * 100 > mp3_to_encode_bytes * MAX_PERCENT)); then
  echo "error: converted M4A files exceed ${MAX_PERCENT}% of their MP3 inputs" \
    "(input=$mp3_to_encode_bytes bytes, output=$mp3_new_encoded_bytes bytes," \
    "ratio=$(percentage "$mp3_new_encoded_bytes" "$mp3_to_encode_bytes"))" >&2
  exit 1
fi

finished_at="$(date +%s)"
elapsed_seconds=$((finished_at - started_at))
avifenc_version_output="$(avifenc --version 2>&1)"
ffmpeg_version_output="$(ffmpeg -version 2>&1)"
avifenc_version="${avifenc_version_output%%$'\n'*}"
ffmpeg_version="${ffmpeg_version_output%%$'\n'*}"

cat >"$REPORT_PATH" <<EOF
PvZGE in-place media transcode summary

Settings:
  AVIF color quality: $AVIF_QUALITY
  AVIF alpha quality: $AVIF_ALPHA_QUALITY
  AVIF speed: $AVIF_SPEED
  AAC profile / bitrate: AAC-LC / $AAC_BITRATE
  Audio channels: mono
  Audio sample rate: max(16000 Hz, input sample rate / 2)
  Parallel workers: $TRANSCODE_JOBS
  Maximum converted/input ratio: $MAX_PERCENT%

PNG -> AVIF (original .png paths retained):
  Files: $png_before_count
  Newly converted: $png_to_encode_count
  Before bytes: $png_before_bytes
  After bytes: $png_after_bytes
  After / before: $(percentage "$png_after_bytes" "$png_before_bytes")

MP3 -> M4A/AAC (original .mp3 paths retained):
  Files: $mp3_before_count
  Newly converted: $mp3_to_encode_count
  Before bytes: $mp3_before_bytes
  After bytes: $mp3_after_bytes
  After / before: $(percentage "$mp3_after_bytes" "$mp3_before_bytes")

Elapsed seconds: $elapsed_seconds
avifenc: $avifenc_version
ffmpeg: $ffmpeg_version
EOF

cat "$REPORT_PATH"
