#!/bin/bash
# Watchdog: shows a macOS notification if the mail summarizer has not finished
# successfully within MAX_AGE seconds — whether launchd stopped firing, the
# wrapper failed to start, or main.py keeps crashing. Runs from its own
# launchd agent, hourly. 8h = two missed 4-hour slots (a night of sleep alone
# doesn't trip it because the missed run fires on wake).
MAX_AGE="${MAX_AGE:-28800}"
OK_FILE="$HOME/.mail_summarizer_skills_last_ok"
LOCK_DIR="$HOME/.mail_summarizer_skills.lock"
LOG="$HOME/Library/Logs/mail-summarizer-watchdog.log"

# A run in progress is not a failure.
OLD_PID=$(cat "$LOCK_DIR/pid" 2>/dev/null)
[ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null && exit 0

LAST=$(cat "$OK_FILE" 2>/dev/null || echo 0)
[[ $LAST =~ ^[0-9]+$ ]] || LAST=0   # a corrupt stamp counts as "never succeeded"
AGE=$(( $(date +%s) - LAST ))
if (( AGE > MAX_AGE )); then
    if (( LAST == 0 )); then SINCE="has never succeeded"; else SINCE="last succeeded $((AGE / 3600))h ago"; fi
    MSG="Mail summarizer $SINCE. Check ~/Library/Logs/mail-summarizer-skills.log and 'launchctl print gui/$(id -u)/com.dron.mail-summarizer'."
    echo "$(date '+%F %T') $MSG" >> "$LOG"
    osascript -e "display notification \"$MSG\" with title \"Mail summarizer stopped\" sound name \"Basso\"" \
        || echo "$(date '+%F %T') could not show notification" >> "$LOG"
fi
