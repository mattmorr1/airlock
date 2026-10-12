#!/usr/bin/env bash
# Airlock demo stack: target-app Mongo (:27018), control API (:8000), job worker, live dashboard (:8099). Ctrl-C stops all.
# Usage: scripts/up.sh          deterministic demo (stub planner + scripted coder; attempt 1 faulty, repair fixed)
#        scripts/up.sh --real   real Claude planner + real Claude Agent SDK coding workers
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "copy .env.example to .env and set AIRLOCK_MONGO_URI" >&2; exit 1; }

export AIRLOCK_EXECUTION_MODE=local AIRLOCK_HUD_FOLLOW_LATEST=1
export AIRLOCK_ROOTS="${AIRLOCK_ROOTS:-{\"repo_full\": \"$PWD/target-app\"}}"  # cwd -> project for /airlock:run
export TARGET_MONGO_URI="${TARGET_MONGO_URI:-mongodb://127.0.0.1:27018/?replicaSet=rs0}"
export AIRLOCK_TOKEN_OPERATOR="${AIRLOCK_TOKEN_OPERATOR:-op-secret}" AIRLOCK_TOKEN_BRIDGE="${AIRLOCK_TOKEN_BRIDGE:-bridge-secret}"
export AIRLOCK_TOKEN_HOOK="${AIRLOCK_TOKEN_HOOK:-hook-secret}" AIRLOCK_TOKEN_READER="${AIRLOCK_TOKEN_READER:-reader-secret}"
if [ "${1:-}" = "--real" ]; then unset AIRLOCK_STUB_PLAN AIRLOCK_DEV_FIXTURE; else export AIRLOCK_STUB_PLAN=1 AIRLOCK_DEV_FIXTURE=auto; fi

RUN="uv run --quiet --env-file .env"
LOGS="${TMPDIR:-/tmp}/airlock-demo"
mkdir -p "$LOGS/db"
trap 'kill $(jobs -p) 2>/dev/null' EXIT

if ! nc -z 127.0.0.1 27018 2>/dev/null; then
  command -v mongod >/dev/null || { echo "the target app needs mongod on :27018: brew tap mongodb/brew && brew install mongodb-community" >&2; exit 1; }
  mongod --dbpath "$LOGS/db" --port 27018 --replSet rs0 --bind_ip 127.0.0.1 >"$LOGS/mongod.log" 2>&1 &
  until nc -z 127.0.0.1 27018 2>/dev/null; do sleep 0.3; done
  $RUN python -c "
from pymongo import MongoClient
from pymongo.errors import OperationFailure
try: MongoClient(port=27018, directConnection=True).admin.command('replSetInitiate')
except OperationFailure as e:
    if e.code != 23: raise  # 23 = AlreadyInitialized
"
fi

$RUN python -m uvicorn api.main:app --port 8000 >"$LOGS/api.log" 2>&1 &
$RUN python -m controller.worker_main >"$LOGS/worker.log" 2>&1 &
$RUN python -m uvicorn ui.server:app --port 8099 >"$LOGS/ui.log" 2>&1 &
until curl -sf 127.0.0.1:8099/healthz >/dev/null && curl -s -o /dev/null 127.0.0.1:8000/v1/tasks/x; do sleep 0.5; done

echo "airlock up ($([ -n "${AIRLOCK_DEV_FIXTURE:-}" ] && echo 'scripted demo mode' || echo 'REAL Claude planner + workers'))"
echo "  dashboard  http://127.0.0.1:8099"
echo "  api        http://127.0.0.1:8000   logs: $LOGS/*.log"
echo "  next       uv run --env-file .env python scripts/run_via_api.py   (or /airlock:run in Claude Code)"
wait
