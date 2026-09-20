#!/bin/bash
# Clawd ONESHOT gate test script
# Usage:
#   bash test-oneshot-gate.sh               # all 5 states, 6s interval
#   bash test-oneshot-gate.sh error         # error only
#   bash test-oneshot-gate.sh notification
#   bash test-oneshot-gate.sh sweeping
#   bash test-oneshot-gate.sh attention
#   bash test-oneshot-gate.sh carrying
#   bash test-oneshot-gate.sh all 10        # all, 10s interval
#
# Test scenarios:
#   1) Turn off the matching row in Animation Map -> run script -> pet should skip that animation (gate active)
#   2) Turn the switch back on -> run script -> pet should play that animation again (reverse check)

STATE=${1:-all}
DELAY=${2:-6}
AGENT=${3:-claude-code}
URL="http://127.0.0.1:23333/state"

# state -> event mapping (matches event names in agents/claude-code.js)
get_event() {
  case $1 in
    error)        echo "PostToolUseFailure" ;;
    notification) echo "Notification" ;;
    sweeping)     echo "PreCompact" ;;
    attention)    echo "Stop" ;;
    carrying)     echo "WorktreeCreate" ;;
    *) echo "" ;;
  esac
}

send_state() {
  local state=$1
  local event=$(get_event "$state")
  local sid="test-${state}-$(date +%s)"
  local payload="{\"state\":\"$state\",\"event\":\"$event\",\"session_id\":\"$sid\",\"agent_id\":\"$AGENT\"}"
  printf "→ [%-13s] event=%-20s " "$state" "$event"
  curl -s -X POST "$URL" -H "Content-Type: application/json" -d "$payload" -w "HTTP %{http_code}\n"
}

# Health check
if ! curl -s "$URL" | grep -q '"ok":true'; then
  echo "✗ Clawd service not running (expected 127.0.0.1:23333). Start with npm start"
  exit 1
fi

if [ "$STATE" = "all" ]; then
  echo "=== Clawd ONESHOT gate full test: 5 states, ${DELAY}s interval ==="
  for s in error notification sweeping attention carrying; do
    send_state "$s"
    sleep "$DELAY"
  done
  echo "=== Done ==="
else
  event=$(get_event "$STATE")
  if [ -z "$event" ]; then
    echo "✗ Unknown state: $STATE"
    echo "  valid values: error | notification | sweeping | attention | carrying | all"
    exit 1
  fi
  send_state "$STATE"
fi
