#!/bin/bash
# Clawd full animation playback test script
# Usage: bash test-demo.sh [seconds per animation, default 8]

DELAY=${1:-8}

SVGS=(
  "clawd-idle-living.svg"
  "clawd-sleeping.svg"
  "clawd-working-thinking.svg"
  "clawd-working-typing.svg"
  "clawd-working-juggling.svg"
  "clawd-working-sweeping.svg"
  "clawd-working-building.svg"
  "clawd-working-debugger.svg"
  "clawd-working-wizard.svg"
  "clawd-working-carrying.svg"
  "clawd-working-conducting.svg"
  "clawd-working-confused.svg"
  "clawd-working-overheated.svg"
  "clawd-error.svg"
  "clawd-working-ultrathink.svg"
  "clawd-happy.svg"
  "clawd-notification.svg"
  "clawd-disconnected.svg"
)

echo "=== Clawd Demo: ${#SVGS[@]} animations, ${DELAY}s each ==="
for i in "${!SVGS[@]}"; do
  svg="${SVGS[$i]}"
  echo "[$((i+1))/${#SVGS[@]}] $svg"
  curl -s -X POST http://127.0.0.1:23333/state \
    -H "Content-Type: application/json" \
    -d "{\"state\":\"working\",\"svg\":\"$svg\"}"
  sleep "$DELAY"
done
echo "=== DONE ==="
