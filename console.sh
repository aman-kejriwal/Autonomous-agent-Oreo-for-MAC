#!/bin/zsh
# macbrow console: start / stop / status / log
# Usage: ./console.sh start|stop|status|log
cd "$(dirname "$0")"
LOG="${MACBROW_LOG:-/tmp/macbrow-console.log}"
SPEECH_LOG="${MACBROW_SPEECH_LOG:-/tmp/macbrow-speech.log}"
SPEECH_PORT=8123

# Free on-device speech (MACBROW_SPEECH=local, the default): mlx-audio serves Parakeet STT and
# Kokoro TTS on an OpenAI-compatible API. Install once:
#   uv tool install --python 3.12 "mlx-audio[server]" --with "misaki[en]" \
#     --with "en_core_web_sm @ https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl"
# HF_HUB_OFFLINE=1 serves models from the local cache without a network check on every load.
start_speech() {
  if ! curl -s -o /dev/null -m 2 "http://127.0.0.1:$SPEECH_PORT/docs"; then
    local server=$(command -v mlx_audio.server || echo ~/.local/bin/mlx_audio.server)
    [ -x "$server" ] || { echo "mlx_audio.server not found; see the install line in $0"; return 1; }
    HF_HUB_OFFLINE=${HF_HUB_OFFLINE:-1} nohup "$server" --host 127.0.0.1 --port $SPEECH_PORT > "$SPEECH_LOG" 2>&1 &
    for i in {1..30}; do curl -s -o /dev/null -m 2 "http://127.0.0.1:$SPEECH_PORT/docs" && break; sleep 1; done
  fi
  # First request per model loads it (~5 s); do it now instead of on the first spoken turn.
  local tts_model=${MACBROW_TTS_MODEL:-mlx-community/Kokoro-82M-bf16} stt_model=${MACBROW_STT_MODEL:-mlx-community/parakeet-tdt-0.6b-v2}
  local wav="${TMPDIR:-/tmp}/macbrow-warm-$$.wav"
  curl -sf -m 120 "http://127.0.0.1:$SPEECH_PORT/v1/audio/speech" -H 'Content-Type: application/json' \
    -d "{\"model\":\"$tts_model\",\"input\":\"ready\",\"voice\":\"${MACBROW_VOICE:-af_heart}\",\"response_format\":\"wav\"}" -o "$wav" \
    && curl -sf -m 120 "http://127.0.0.1:$SPEECH_PORT/v1/audio/transcriptions" -F "file=@$wav" -F "model=$stt_model" -o /dev/null \
    || { echo "local speech server not responding; see $SPEECH_LOG"; rm -f "$wav"; return 1; }
  rm -f "$wav"; echo "local speech ready on :$SPEECH_PORT (log: $SPEECH_LOG)"
}

case "${1:-start}" in
  start)
    if pgrep -f "agent.py console" >/dev/null; then echo "already running (pid $(pgrep -f 'agent.py console' | head -1))"; exit 0; fi
    set -a; [ -f .env.local ] && source .env.local; set +a
    # Keys exported only in the interactive shell profile (e.g. ~/.zshrc) aren't visible to a
    # detached start; pull them in when missing.
    required=(TYPESAFE_API_KEY); [ "${MACBROW_SPEECH:-local}" = gradium ] && required+=(GRADIUM_API_KEY)
    for v in $required; do
      if [ -z "${(P)v}" ]; then
        val=$(zsh -ic "print -r -- \${$v}" 2>/dev/null); [ -n "$val" ] && export "$v=$val"
      fi
    done
    missing=(); for v in $required; do [ -z "${(P)v}" ] && missing+=("$v"); done
    if [ ${#missing[@]} -gt 0 ]; then echo "missing: ${missing[*]} (set in .env.local or your shell profile)"; exit 1; fi
    if [ "${MACBROW_SPEECH:-local}" = local ]; then start_speech || exit 1; fi
    nohup uv run python agent.py console > "$LOG" 2>&1 &
    sleep 3; echo "started (pid $!), log: $LOG" ;;
  stop)
    pkill -INT -f "agent.py console" 2>/dev/null && sleep 2; pkill -9 -f "agent.py console" 2>/dev/null; pkill -f "mlx_audio.server --host 127.0.0.1 --port $SPEECH_PORT" 2>/dev/null; echo "stopped" ;;
  status)
    pgrep -fl "agent.py console" || echo "not running" ;;
  log)
    sed 's/\x1b\[[0-9;]*[a-zA-Z]//g' "$LOG" | grep -E "user_transcript|macbrow\.router +route|\"role\": \"assistant\"" | sed -E 's/^ *[0-9:.]* *(DEBUG|INFO) *//' ;;
  *) echo "usage: $0 start|stop|status|log"; exit 1 ;;
esac
