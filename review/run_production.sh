#!/bin/bash
# Launch the review scraper inside a tmux session so it survives SSH
# disconnect / VPS console close. The session is named 'review-scraper'.
#
# Usage:
#   bash review/run_production.sh           # full run, 4 rows in parallel
#   bash review/run_production.sh --rows 100  # first 100 rows only (smoke)
#   bash review/run_production.sh --start 1000 --rows 500
#
# Monitor while running:
#   tmux attach -t review-scraper           # live view
#   tail -f /var/HVAC/review/review.log     # tail the log
#   bash review/run_production.sh status    # quick state report
#   bash review/run_production.sh stop      # graceful shutdown (SIGTERM)
#
# Detach from tmux without killing the run: press Ctrl-b, then d.
set -euo pipefail

cd /var/HVAC

SESSION="review-scraper"
ACTIVATE="source venv/bin/activate"
LOG="/var/HVAC/review/review.log"

# Subcommands -------------------------------------------------------------
case "${1:-}" in
  status)
    if tmux has-session -t "$SESSION" 2>/dev/null; then
      echo "tmux session '$SESSION' exists (running)."
      echo
      echo "--- last 20 log lines ---"
      tail -20 "$LOG" 2>/dev/null || echo "(no log yet)"
      echo
      echo "--- progress snapshot ---"
      /var/HVAC/venv/bin/python -c "
import json, os
p = '/var/HVAC/review/review_progress.json'
if os.path.exists(p):
    s = json.load(open(p))
    rows = s.get('rows', {})
    done = sum(1 for r in rows.values() if r.get('row_complete'))
    total = len(rows)
    print(f'rows_tracked={total}  rows_complete={done}')
else:
    print('no progress file yet')
" 2>&1
      echo
      echo "--- running chrome instances ---"
      ps -eo pid,etime,comm | grep -E "chrome-linux64|Xvfb" | wc -l
      echo "(detached scraper still running — press Ctrl-b then d after attach)"
    else
      echo "no tmux session named '$SESSION'"
    fi
    exit 0
    ;;

  stop)
    if tmux has-session -t "$SESSION" 2>/dev/null; then
      # Send SIGTERM to the python process; signal handlers flush progress
      pid=$(tmux list-panes -t "$SESSION" -F '#{pane_pid}' | head -1)
      kids=$(pgrep -P "$pid" -f "review.runner" || true)
      if [ -n "$kids" ]; then
        echo "sending SIGTERM to runner pid(s): $kids"
        kill -TERM $kids
        sleep 5
      fi
      tmux kill-session -t "$SESSION"
      echo "session stopped"
    else
      echo "no tmux session named '$SESSION'"
    fi
    exit 0
    ;;
esac

# Default: launch -----------------------------------------------------------
if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "ERROR: tmux session '$SESSION' already running."
  echo "  attach: tmux attach -t $SESSION"
  echo "  stop:   bash $0 stop"
  exit 1
fi

ARGS="$*"

# NOTE: don't pipe the runner's stdout to $LOG with tee — the runner already
# writes to $LOG via its own FileHandler. Doubling causes every line twice.
CMD="$ACTIVATE && python -m review.runner $ARGS"
echo "Starting tmux session '$SESSION'"
echo "  command: python -m review.runner $ARGS"
echo "  log:     $LOG"
tmux new-session -d -s "$SESSION" "$CMD; echo; echo '--- runner exited; press Enter to close pane ---'; read"
sleep 1

echo
echo "✓ launched. it will keep running after you SSH out."
echo
echo "  attach:  tmux attach -t $SESSION   (detach with Ctrl-b d)"
echo "  status:  bash $0 status"
echo "  stop:    bash $0 stop"
echo "  log:     tail -f $LOG"
