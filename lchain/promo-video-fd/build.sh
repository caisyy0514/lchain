#!/usr/bin/env bash
# Build the promo video: ./build.sh
#   --preview   only render key frames per scene to output/preview/
#   --no-voice  background music only, no narration
# Voice can be changed via env var, e.g.: VOICE=zh-CN-YunxiNeural ./build.sh
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "== create venv"
  uv venv .venv -q --python 3.12 2>/dev/null || python3 -m venv .venv
  uv pip install --python .venv/bin/python -q -r requirements.txt 2>/dev/null || .venv/bin/python -m pip install -q -r requirements.txt
fi
PY=.venv/bin/python
VOICE="${VOICE:-zh-CN-XiaoxiaoNeural}"
RATE="${RATE:--4%}"

if [[ "${1:-}" == "--preview" ]]; then
  $PY render.py --preview && $PY contact_sheet.py
  exit 0
fi

FPS=30
DUR=$($PY -c "from render import total_duration; print(total_duration())")
echo "== duration ${DUR}s"

if [[ "${1:-}" == "--no-voice" ]]; then
  echo "== 1/4 skip narration"
  rm -f output/voice/subs.json
else
  echo "== 1/4 narration (${VOICE})"
  # also writes subtitles synced to speech: output/voice/subs.json
  $PY voice.py --voice "$VOICE" --rate="$RATE"
fi

echo "== 2/4 render frames"
$PY render.py --fps $FPS

echo "== 3/4 music"
$PY music.py "$DUR"

echo "== 4/4 mux"
if [[ "${1:-}" == "--no-voice" ]]; then
  AUDIO_IN=(-i output/music.wav)
  AUDIO_FILTER="[1:a]aresample=48000,loudnorm=I=-16:TP=-1.5:LRA=11[a]"
else
  AUDIO_IN=(-i output/music.wav -i output/voice.wav)
  # duck the music ~8dB while the narrator speaks, then normalize to -16 LUFS
  AUDIO_FILTER="[1:a]aresample=48000,volume=0.42[m];[2:a]aresample=48000,asplit=2[v1][v2];[m][v1]sidechaincompress=threshold=0.02:ratio=4:attack=80:release=600[md];[md][v2]amix=inputs=2:normalize=0,loudnorm=I=-16:TP=-1.5:LRA=11[a]"
fi
ffmpeg -y -v error -stats -framerate $FPS -i output/frames/%05d.png "${AUDIO_IN[@]}" \
  -filter_complex "$AUDIO_FILTER" -map 0:v -map "[a]" \
  -c:v libx264 -preset slow -crf 17 -pix_fmt yuv420p -movflags +faststart \
  -c:a aac -b:a 192k -ar 48000 -shortest output/fd-intro.mp4

echo "== done: $(pwd)/output/fd-intro.mp4"
