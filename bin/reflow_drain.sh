#!/usr/bin/env bash
# Attended reflow drain on the LIVE store (docs/RELEASE-RUNBOOK.md §8,
# "Draining the backlog faster (attended)").
#
#   bin/reflow_drain.sh [--max-owners N] [--source reflow:drive ...] [--no-backup-check]
#
# Boots the daemon out (launchctl stop is NOT enough -- KeepAlive relaunches it;
# see CLAUDE.md "STORE CORRUPTION INCIDENT — 2026-09-10"), confirms no daemon
# process is left, snapshots the store with VACUUM INTO, runs
# bin/reflow_drain.py --yes, and ALWAYS bootstraps the daemon back on exit --
# but only once the drain process itself is gone, so there are never two writers.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.mcpbrain"
DOMAIN="gui/$(id -u)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
APP_DIR="${MCPBRAIN_HOME:-$HOME/Library/Application Support/mcpbrain}"
STORE="$APP_DIR/brain.sqlite3"
SNAP=""
CLEANED=0

caffeinate -dimsu -w $$ &

daemon_pids() {
    pgrep -f "mcpbrain daemon" || true
}

cleanup() {
    local rc=$?
    [ "$CLEANED" = 1 ] && return
    CLEANED=1
    trap - EXIT INT TERM
    # Never hand the store back while the drain could still be writing.
    local i
    for i in $(seq 1 120); do
        pgrep -f "bin/reflow_drain.py" >/dev/null 2>&1 || break
        [ "$i" = 1 ] && echo "waiting for the drain process to exit..."
        sleep 1
    done
    if pgrep -f "bin/reflow_drain.py" >/dev/null 2>&1; then
        echo "!! the drain process is still running -- NOT restarting the daemon." >&2
        echo "!! When it has exited: launchctl bootstrap $DOMAIN $PLIST" >&2
        exit 1
    fi
    echo
    echo "restarting the daemon: launchctl bootstrap $DOMAIN $PLIST"
    if ! launchctl bootstrap "$DOMAIN" "$PLIST" 2>/tmp/reflow_drain_bootstrap.$$; then
        if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
            echo "(already loaded)"
        else
            echo "!! bootstrap FAILED: $(cat /tmp/reflow_drain_bootstrap.$$)" >&2
            echo "!! re-run by hand: launchctl bootstrap $DOMAIN $PLIST" >&2
        fi
    fi
    rm -f /tmp/reflow_drain_bootstrap.$$
    echo "confirm it is healthy:  mcpbrain doctor   (Reflow line; version; integrity)"
    if [ -n "$SNAP" ]; then
        echo "snapshot kept at: $SNAP"
        echo "delete it once the daemon has completed a backup after this run:  rm \"$SNAP\""
    fi
    exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [ ! -f "$STORE" ]; then
    echo "no store at $STORE" >&2
    exit 2
fi
if [ ! -f "$PLIST" ]; then
    echo "no launch agent at $PLIST -- refusing (could not restart the daemon afterwards)" >&2
    exit 2
fi

echo "stopping the daemon: launchctl bootout $DOMAIN/$LABEL"
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || echo "(not loaded)"

gone=0
for _ in $(seq 1 30); do
    if [ -z "$(daemon_pids)" ]; then gone=1; break; fi
    sleep 1
done
if [ "$gone" != 1 ]; then
    echo "a daemon process is still alive after 30 s: $(daemon_pids | tr '\n' ' ')-- aborting" >&2
    exit 1
fi
echo "no daemon process running."

store_bytes=$(stat -f%z "$STORE")
if [ -f "$STORE-wal" ]; then
    store_bytes=$((store_bytes + $(stat -f%z "$STORE-wal")))
fi
free_bytes=$(( $(df -k "$APP_DIR" | awk 'NR==2 {print $4}') * 1024 ))
need=$(( store_bytes * 2 ))
if [ "$free_bytes" -lt "$need" ]; then
    echo "not enough free disk for a snapshot: $((free_bytes / 1048576)) MB free," \
         "need $((need / 1048576)) MB (2x the store) -- aborting" >&2
    exit 1
fi

snap_path="$STORE.pre-reflow-drain-$(date +%s)"
echo "snapshotting the store to $snap_path ..."
if ! sqlite3 "$STORE" "VACUUM INTO '$snap_path'" || [ ! -s "$snap_path" ]; then
    echo "snapshot failed or is empty -- aborting" >&2
    rm -f "$snap_path"
    exit 1
fi
SNAP="$snap_path"
echo "snapshot: $SNAP ($(( $(stat -f%z "$SNAP") / 1048576 )) MB)"

cd "$REPO"
uv run python bin/reflow_drain.py --yes "$@"
