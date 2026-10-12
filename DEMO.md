# Demo runbook (3 minutes)

## Before judging (10 minutes ahead)

```bash
scripts/up.sh                                   # terminal 1: stays running
open http://127.0.0.1:8099                      # dashboard, full screen on the second display
cd target-app && claude --plugin-dir ../plugin  # terminal 2, inside the registered repo
```

**Plugin:** two ways in. For local dev, `claude --plugin-dir ../plugin` from `target-app/`. For a clean
install, `/plugin marketplace add mattmorr1/airlock@matt-feat` then `/plugin install airlock@mongohack` —
the bridge is vendored into the plugin and launched with `uv run --with`, so the marketplace copy is
self-contained (no repo needed; ADR 0008 described the older `--project ..` layout that couldn't find
`bridge/server.py`). Either way `/airlock:run <request>` auto-registers the project from the current directory.

**HUD:** `.claude/settings.json` points the statusline at
`plugin/scripts/hud.py` and supplies the dev tokens. `up.sh` sets
`AIRLOCK_HUD_FOLLOW_LATEST=1`, so Hatch follows the newest task even when a
script started it from another terminal. Preview every mood without the API:
`AIRLOCK_HUD_DEMO=1 claude`.

**Dry run:** run Act 1 and Act 2 once. Both should end with the expected
result line. Reset by just running them again: each run creates fresh ids.

## Script

| Time | Say | Do | Audience sees |
|---|---|---|---|
| 0:00 | "Agents write code fast. Who checks it, and who checks the checker?" | (title) | |
| 0:10 | "Here's a bug every status-code test misses." | Show `demo/bulk_feature/faulty/app/bulk.py`: ownership check inside the loop, no transaction | The 403 arrives *after* Alice's row is written |
| 0:25 | **Act 1: the escape.** "Harness v1 tests status codes. Watch it pass bad code." | `uv run --env-file .env python scripts/run_fullflow.py` | Dashboard: v1 verdict **pass** on faulty code, task completes. Then evolution starts |
| 1:00 | "Airlock noticed the escape, proposed a stricter harness, and an independent evaluator ran both on held-out cases." | Point at the evolution card | **Escaped failures 3 → 0**, 0 legitimate runs blocked, v1 → v2 **promoted** |
| 1:20 | **Act 2: the catch.** "Same project, same request, now from Claude Code, with the harness it just evolved." | `/airlock:run Implement atomic bulk item updates with idempotency` (fallback: `uv run --env-file .env python scripts/run_via_api.py`) | Hatch: *thinking* → *waiting on you* |
| 1:35 | "I'm the operator. I review the contract, then approve it by its digest." | `! airlock show <task_id>`, then `! airlock -y approve <task_id>` | Constraints, acceptance and assignments printed; dashboard card shows the contract; task `approved` |
| 1:45 | "Attempt 1 is frozen and tested in a sandbox…" | (watch) | Attempt 1 → **repair_required**. The red evidence row shows `title: valid-A → PWNED`. Hatch: *alarm* |
| 2:05 | "…the repair passes, gets merged, and is validated again on the merged tree." | (watch) | Attempt 2 **pass** → integration **accepted** → task **completed**. Hatch: *success* |
| 2:25 | "Revoke voids anything in flight. Rollback is one command." | `! airlock -y revoke <project>` (optional) | Epoch bumps on the dashboard |
| 2:40 | "All of it is MongoDB: transactions for every state change, change streams driving this dashboard live, GridFS for content-addressed artifacts, Atlas as the shared record." | Show the README table | |

## If something breaks

| Symptom | Fix |
|---|---|
| Dashboard says "reconnecting" | `tail $TMPDIR/airlock-demo/ui.log`. Atlas IP access list or URI |
| Task stuck at `planned` | You haven't approved: `! airlock -y approve <task_id>` |
| Task can never finish (a delegation failed) | `! airlock -y cancel <task_id>`; `! airlock ls` lists recent tasks |
| "no project is registered for …" | Start Claude inside `target-app/` and restart `up.sh` so `AIRLOCK_ROOTS` is set |
| Stuck at `running` | `tail $TMPDIR/airlock-demo/worker.log`. Is the target mongod on :27018 up? |
| Hatch says "api unreachable" | API not up, or Claude Code wasn't started from the repo root |
| Live demo fails outright | Pick an earlier project in the dashboard's switcher: completed runs stay in Atlas |

## Real mode (record it, don't do it live)

`scripts/up.sh --real` uses the Claude planner and Claude Agent SDK coding
workers. The dashboard tags each attempt `claude-sdk · N turns · $cost`. The
planner usually splits the feature into about 4 dependent assignments, so it
takes several minutes, and a real coder may get it right on the first try (no
"catch" moment).
