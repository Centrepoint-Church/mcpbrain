#!/usr/bin/env bash
# Attended reflow drain on the LIVE store (docs/RELEASE-RUNBOOK.md §8,
# "Draining the backlog faster (attended)"). Run it inside tmux/screen.
#
#   bin/reflow_drain.sh [--max-owners N] [--source reflow:drive ...] [--no-backup-check]
#
# 1. bin/reflow_drain.py --check (refusal gates) -- a refusal costs nothing;
# 2. boots the daemon out (launchctl stop is NOT enough -- KeepAlive relaunches
#    it; CLAUDE.md "STORE CORRUPTION INCIDENT — 2026-09-10") and confirms no
#    daemon process is left;
# 3. snapshots the store (sqlite3 -readonly ... VACUUM INTO);
# 4. runs bin/reflow_drain.py --yes with the INSTALLED tool's interpreter (the
#    daemon's exact code and dependencies; never `uv run`);
# 5. on ANY exit bootstraps the daemon back exactly once -- after the drain
#    process is gone, and BEFORE printing anything (a closed terminal must not
#    be able to kill this script between the bootout and the bootstrap) --
#    EXCEPT when the drain reports a store-check failure (exit 5): then the
#    daemon stays down and the exact bootstrap command is printed and logged.
set -euo pipefail
# A dead terminal must turn writes into errors, never kill us (SIGPIPE).
trap '' PIPE

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DRAIN="$REPO/bin/reflow_drain.py"
PY="${MCPBRAIN_PY:-$HOME/.local/share/uv/tools/mcpbrain/bin/python}"
LABEL="com.mcpbrain"
DOMAIN="gui/$(id -u)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
APP_DIR="${MCPBRAIN_HOME:-$HOME/Library/Application Support/mcpbrain}"
STORE="$APP_DIR/brain.sqlite3"
LOG="$APP_DIR/logs/reflow_drain.log"
BOOTSTRAP_CMD="launchctl bootstrap $DOMAIN \"$PLIST\""
EXIT_STORE_CHECK=5
SNAP=""
BOOTED_OUT=0
CLEANED=0
DRAIN_RC=""

mkdir -p "$APP_DIR/logs" 2>/dev/null || true

# Every line goes to the log first, then (best effort) to the terminal.
say() {
    { printf '%s %s\n' "$(date '+%F %T')" "$*" >>"$LOG"; } 2>/dev/null || true
    { printf '%s\n' "$*"; } 2>/dev/null || true
}

drain_running() {
    # The interpreter running THIS script with --yes -- never an editor
    # that merely has the file open.
    pgrep -f "python[0-9.]* .*bin/reflow_drain\.py .*--yes" >/dev/null 2>&1
}

cleanup() {
    local rc=$?
    set +e
    trap '' INT TERM HUP PIPE
    [ "$CLEANED" = 1 ] && exit "$rc"
    CLEANED=1
    [ -n "$DRAIN_RC" ] && rc="$DRAIN_RC"
    if [ "$BOOTED_OUT" = 1 ]; then
        # Never hand the store back while the drain could still be writing.
        local i
        for i in $(seq 1 120); do
            drain_running || break
            sleep 1
        done
        if drain_running; then
            say "!! the drain process is still running -- daemon NOT restarted."
            say "!! once it has exited: $BOOTSTRAP_CMD"
        elif [ "$rc" = "$EXIT_STORE_CHECK" ]; then
            say "!! store check FAILED (integrity_check / foreign_key_check): the daemon"
            say "!! was deliberately NOT restarted. Investigate first (CLAUDE.md, 2026-09-10"
            say "!! incident rules; snapshot below). Only then: $BOOTSTRAP_CMD"
        else
            # Bootstrap FIRST, output after.
            local err
            err=$(launchctl bootstrap "$DOMAIN" "$PLIST" 2>&1)
            local brc=$?
            if [ "$brc" = 0 ]; then
                say "daemon restarted ($BOOTSTRAP_CMD)."
            elif launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
                say "daemon was already loaded ($err)."
            else
                say "!! bootstrap FAILED: $err"
                say "!! re-run by hand: $BOOTSTRAP_CMD"
            fi
            say "confirm it is healthy:  mcpbrain doctor   (Reflow line; version; integrity)"
        fi
    fi
    if [ -n "$SNAP" ]; then
        say "snapshot kept at: $SNAP"
        say "delete it once the daemon has completed a backup after this run:  rm \"$SNAP\""
    fi
    say "log: $LOG"
    exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

say "reflow drain wrapper starting (store: $STORE)"
if [ ! -x "$PY" ]; then
    say "no installed mcpbrain interpreter at $PY (uv tool install mcpbrain first)"
    exit 2
fi
if [ ! -f "$STORE" ]; then
    say "no store at $STORE"
    exit 2
fi
if [ ! -f "$PLIST" ]; then
    say "no launch agent at $PLIST -- refusing (could not restart the daemon afterwards)"
    exit 2
fi

# 1. gates, before anything is stopped
if ! "$PY" -I "$DRAIN" --check "$@"; then
    say "refused by the pre-flight check; nothing was stopped or written."
    exit 2
fi

caffeinate -dimsu -w $$ &

# 2. stop the daemon
say "stopping the daemon: launchctl bootout $DOMAIN/$LABEL"
BOOTED_OUT=1
launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || say "(not loaded)"
gone=0
for _ in $(seq 1 30); do
    if ! pgrep -f "mcpbrain[ .]daemon" >/dev/null 2>&1; then gone=1; break; fi
    sleep 1
done
if [ "$gone" != 1 ]; then
    say "a daemon process is still alive after 30 s: $(pgrep -f 'mcpbrain[ .]daemon' | tr '\n' ' ')-- aborting"
    exit 1
fi
say "no daemon process running."

# 3. snapshot
store_bytes=$(stat -f%z "$STORE")
if [ -f "$STORE-wal" ]; then
    store_bytes=$((store_bytes + $(stat -f%z "$STORE-wal")))
fi
free_bytes=$(( $(df -k "$APP_DIR" | awk 'NR==2 {print $4}') * 1024 ))
need=$(( store_bytes * 2 ))
if [ "$free_bytes" -lt "$need" ]; then
    say "not enough free disk for a snapshot: $((free_bytes / 1048576)) MB free, need $((need / 1048576)) MB (2x the store) -- aborting"
    exit 1
fi
snap_path="$STORE.pre-reflow-drain-$(date +%s)"
say "snapshotting the store to $snap_path ..."
if ! sqlite3 -readonly "$STORE" "VACUUM INTO '$snap_path'" || [ ! -s "$snap_path" ]; then
    say "snapshot failed or is empty -- aborting"
    rm -f "$snap_path"
    exit 1
fi
SNAP="$snap_path"
say "snapshot: $SNAP ($(( $(stat -f%z "$SNAP") / 1048576 )) MB)"

# 4. the drain. Signals now only get NOTED: the drain handles them itself and
# its exit code (a store-check failure above all) must survive them.
trap 'say "signal received; waiting for the drain to stop cleanly..."' INT TERM HUP
set +e
"$PY" -I "$DRAIN" --yes "$@"
DRAIN_RC=$?
set -e
exit "$DRAIN_RC"
