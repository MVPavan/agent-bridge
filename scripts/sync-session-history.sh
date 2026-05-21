#!/usr/bin/env bash
# Re-sync Claude Code session history from the old (bodha) project slug
# to this project's slug, so `claude /resume` from /data/codes/agent-bridge
# finds the conversation that built this bridge.
#
# Why this exists:
# - Claude Code stores per-project session JSONLs under
#   ~/.claude/projects/<slug>/, where <slug> is the working-directory path
#   with `/` replaced by `-`.
# - This bridge was built inside /data/codes/bodha (slug -data-codes-bodha)
#   before being moved to /data/codes/agent-bridge (slug -data-codes-agent-bridge).
# - The session JSONL is still being appended to its old slug for as long
#   as you keep the bodha session open. After you end that session, run
#   this script one more time to copy the final tail.
#
# This script is idempotent — re-run as often as you like. It only COPIES;
# the bodha originals are left in place as backup.

set -euo pipefail

SRC="${HOME}/.claude/projects/-data-codes-bodha"
DST="${HOME}/.claude/projects/-data-codes-agent-bridge"

# Identify agent-bridge sessions by content match (>= 50 hits filters out
# incidental mentions from unrelated chats).
THRESHOLD=50

mkdir -p "$DST"

echo "Scanning ${SRC} for agent-bridge sessions (>=${THRESHOLD} content hits)..."
moved=0
for f in "$SRC"/*.jsonl; do
  [ -e "$f" ] || continue
  count=$(grep -c "agent-bridge\|/data/codes/agent-bridge\|/data/codes/bodha/agent-bridge\|agent_bridge" "$f" 2>/dev/null || true)
  count=${count:-0}
  if [ "$count" -ge "$THRESHOLD" ]; then
    sid=$(basename "$f" .jsonl)
    echo "  copying $sid ($count hits, $(stat -c %s "$f") bytes)"
    cp -a "$f" "$DST/"
    if [ -d "$SRC/$sid" ]; then
      cp -a "$SRC/$sid" "$DST/"
      echo "    + sibling subagent dir"
    fi
    moved=$((moved + 1))
  fi
done

echo
if [ "$moved" -eq 0 ]; then
  echo "Nothing matched. Either the bodha slug is gone, or no session has >=${THRESHOLD} agent-bridge hits."
else
  echo "Synced $moved session(s) to ${DST}."
  echo
  echo "Next step: open Claude Code from /data/codes/agent-bridge and /resume:"
  echo "  cd /data/codes/agent-bridge && claude   # then run /resume"
fi

# Optional cleanup hint — never deleting automatically.
cat <<HINT

Originals at ${SRC} are PRESERVED. Once you have confirmed /resume works
from the new location, you can safely delete the bodha copies with:

  for f in $DST/*.jsonl; do
    sid=\$(basename "\$f" .jsonl)
    rm -f "${SRC}/\$sid.jsonl"
    [ -d "${SRC}/\$sid" ] && rm -rf "${SRC}/\$sid"
  done

(or just leave them — they're per-user state, not in any repo.)
HINT
