#!/bin/bash
# Commits and pushes the live HTPC files to GitHub (the repo in origin) whenever they changed.
# Runs after every Claude Code response (Stop hook in ~/.claude/settings.json) and every 10 minutes (htpc-autosync.timer).
# collect.sh copies the live files in and runs check-secrets.sh: if a secret would be published, NOTHING is committed
# or pushed, and (when called from Claude Code) Claude is told to fix it before finishing.
cd "$(dirname "$0")" || exit 0
LOG="$HOME/.local/share/htpc-web/autosync.log"
mkdir -p "$(dirname "$LOG")" "$HOME/.cache/htpc"
exec 9>"$HOME/.cache/htpc/autosync.lock"
flock -n 9 || exit 0                                         # another sync is already running
log() { echo "$(date '+%F %T') $*" >> "$LOG"; }
[ -e "$HOME/.cache/htpc/autosync.paused" ] && exit 0          # paused by hand: rm ~/.cache/htpc/autosync.paused to resume

if ! out=$(./collect.sh 2>&1); then
  log "NOT committed - $out"
  if [ "$1" = "--claude" ]; then                             # exit 2 = Claude Code shows this to Claude, who must fix it
    echo "aniserver auto-sync blocked: $out" >&2
    exit 2
  fi
  exit 0
fi

git add -A
git diff --cached --quiet && exit 0                          # nothing changed
files=$(git diff --cached --name-only)
count=$(echo "$files" | wc -l)
subject="Update $(echo "$files" | head -4 | xargs -n1 basename | paste -sd, - | sed 's/,/, /g')"
[ "$count" -gt 4 ] && subject="$subject and $((count - 4)) more"
stat=$(git diff --cached --shortstat | sed 's/^ //')
trailer="Co-Authored-By: Claude <noreply@anthropic.com>"
[ -s "$HOME/.cache/htpc/claude-attribution" ] && trailer=$(cat "$HOME/.cache/htpc/claude-attribution")
git commit -q -m "$subject" -m "Auto-sync of the live HTPC files ($stat):
$(echo "$files" | sed 's/^/- /')" -m "$trailer"
if git push -q origin HEAD 2>>"$LOG"; then
  log "pushed: $subject ($stat)"
else
  log "committed locally, push failed (retried on the next sync): $subject"
fi
exit 0
