#!/usr/bin/env bash
# Show processing progress: episodes fully done, steps per status, failed steps,
# and the latest temperature reading. Read-only (runs in the api container).
set -euo pipefail
source "$(dirname "$0")/common.sh"

remote "docker compose exec -T api python -" <<'PY'
import sqlite3

c = sqlite3.connect("file:/data/dp.sqlite?mode=ro", uri=True)
active = "e.retired = 0 AND e.video_id IS NOT NULL"
total = c.execute(f"SELECT COUNT(*) FROM episodes e WHERE {active}").fetchone()[0]
done = c.execute(
    f"""SELECT COUNT(*) FROM episodes e WHERE {active} AND (
          SELECT COUNT(*) FROM episode_steps s WHERE s.episode_id = e.id
          AND s.status = 'done' AND s.video_id = e.video_id) >= 5"""
).fetchone()[0]
print(f"episodes done: {done} / {total} ({100 * done / max(total, 1):.1f}%)")
print("steps:")
for step, status, n in c.execute(
    "SELECT step, status, COUNT(*) FROM episode_steps GROUP BY 1, 2 ORDER BY 1, 2"
):
    print(f"  {step:<10} {status:<8} {n}")
failed = c.execute(
    "SELECT episode_id, step, attempts, retry_after, substr(error, 1, 100) FROM episode_steps"
    " WHERE status = 'failed' ORDER BY episode_id LIMIT 20"
).fetchall()
if failed:
    print("failed (episode, step, attempts, retry after UTC, error):")
    for row in failed:
        print("  ", row)
row = c.execute(
    "SELECT at, cpu_c, gpu_c, nvme_c, state, episode_id FROM worker_health ORDER BY at DESC LIMIT 1"
).fetchone()
if row:
    print(f"last health (UTC) {row[0]}: cpu {row[1]} gpu {row[2]} nvme {row[3]} °C,"
          f" {row[4]}, episode {row[5]}")
PY
