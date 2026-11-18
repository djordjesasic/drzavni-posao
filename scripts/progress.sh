#!/usr/bin/env bash
# Show processing progress: episodes fully done, steps per status, failed steps, the
# latest temperature reading, and what happened in the last N hours (default 12) with
# an estimate of the time left. Read-only (runs in the api container).
#   bash scripts/progress.sh        # last 12 h
#   bash scripts/progress.sh 24     # last 24 h
set -euo pipefail
source "$(dirname "$0")/common.sh"

hours="${1:-12}"
if ! [[ "$hours" =~ ^[0-9]+$ ]] || ((hours < 1)); then
  echo "usage: $0 [hours]" >&2
  exit 2
fi

remote "docker compose exec -T api python - $hours" <<'PY'
import sqlite3
import sys

hours = int(sys.argv[1])
since = f"-{hours} hours"
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

# --- the last N hours -------------------------------------------------------------
print(f"\nlast {hours} h:")
n_done = c.execute(
    "SELECT COUNT(*) FROM episode_steps WHERE step='index' AND status='done' "
    "AND updated_at > datetime('now', ?)", (since,)
).fetchone()[0]
print(f"  episodes finished: {n_done} ({n_done / hours:.1f} per hour)")
print("  average time per step (s):")
for step in ("download", "prepare", "transcribe", "speakers", "index"):
    n, avg = c.execute(
        "SELECT COUNT(*), AVG(elapsed_s) FROM episode_steps WHERE step=? AND status='done' "
        "AND updated_at > datetime('now', ?)", (step, since)
    ).fetchone()
    if n:
        print(f"    {step:<10} {avg:7.1f}  ({n} episodes)")
audio, speed = c.execute(
    "SELECT AVG(json_extract(info, '$.audio_s')), AVG(json_extract(info, '$.speed_x')) "
    "FROM episode_steps WHERE step='transcribe' AND status='done' "
    "AND updated_at > datetime('now', ?)", (since,)
).fetchone()
if audio:
    print(f"  average episode: {audio / 60:.1f} min of audio; Whisper {speed:.2f}x real time")
# One health row per minute; 'hot' and 'cooled' are minutes the worker waited for heat.
states = dict(c.execute(
    "SELECT state, COUNT(*) FROM worker_health WHERE at > datetime('now', ?) GROUP BY 1",
    (since,),
).fetchall())
logged = sum(states.values())
waited = states.get("hot", 0) + states.get("cooled", 0)
print(f"  worker minutes logged: {logged}; waiting for heat: {waited}"
      f" ({100 * waited / max(logged, 1):.0f}%); idle: {states.get('idle', 0)}")
print("  (minutes not logged at all are mostly the daily pause 18:55-20:00)")
mx = c.execute(
    "SELECT MAX(cpu_c), MAX(gpu_c), MAX(nvme_c) FROM worker_health WHERE at > datetime('now', ?)",
    (since,),
).fetchone()
if mx[0] is not None:
    print(f"  max temperatures: cpu {mx[0]:.0f}, gpu {mx[1]:.0f}, nvme {mx[2]:.0f} °C")
if n_done:
    left_h = (total - done) / (n_done / hours)
    print(f"  at this pace: {total - done} episodes left, ~{left_h:.0f} h (~{left_h / 24:.1f} days)")
PY
