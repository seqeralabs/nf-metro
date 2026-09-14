#!/usr/bin/env bash
# Converts a Playwright-recorded .webm into the mp4 committed at
# website/public/assets/live_demo.mp4. See the `live-demo-video` skill for
# the full recording recipe this is one step of.
#
# Usage: scripts/convert_demo_video.sh <input.webm> <output.mp4>
set -euo pipefail

if [ $# -ne 2 ]; then
  echo "usage: $0 <input.webm> <output.mp4>" >&2
  exit 1
fi

IN="$1"
OUT="$2"

if command -v ffmpeg >/dev/null 2>&1; then
  FFMPEG=ffmpeg
else
  # No system ffmpeg (this machine doesn't have one via brew) - use the
  # prebuilt binary pip's imageio-ffmpeg bundles, in a throwaway venv so this
  # doesn't touch the system Python or Homebrew.
  VENV_PARENT="$(mktemp -d)"
  trap 'rm -rf "$VENV_PARENT"' EXIT
  VENV="$VENV_PARENT/ffmpeg-venv"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q imageio-ffmpeg
  FFMPEG="$("$VENV/bin/python" -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())')"
fi

# 2x speed-up (the real run takes ~50s; the doc clip loops so shorter reads
# better), 15fps (enough for the ring style's marching-dash animation),
# h264/yuv420p + faststart for broad <video> compatibility, no audio track.
"$FFMPEG" -y -i "$IN" \
  -filter:v "setpts=0.5*PTS,fps=15" \
  -c:v libx264 -preset slow -crf 26 -pix_fmt yuv420p -movflags +faststart -an \
  "$OUT"
