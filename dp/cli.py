"""Command-line entry point: `python -m dp.cli <command>` (run inside the worker image)."""

import argparse
import json
import logging
import signal
import sqlite3
from pathlib import Path

from dp import pilot, pipeline, worker
from dp.catalog import characters, classify, export, youtube
from dp.config import get_settings
from dp.db import active_video_id, connect, migrate
from dp.enrich import frames
from dp.enrich import run as enrich_run
from dp.eval import run as eval_run
from dp.index import embed as embed_client
from dp.index import search as index_search
from dp.services import check_services
from dp.transcribe import download, speakers
from dp.transcribe import steps as transcribe_steps

log = logging.getLogger("dp")


def _db() -> sqlite3.Connection:
    conn = connect(get_settings().db)
    migrate(conn)
    return conn


def cmd_init(_: argparse.Namespace) -> None:
    print(f"schema version {migrate(connect(get_settings().db))}")


def cmd_check_services(_: argparse.Namespace) -> None:
    for name, result in check_services(get_settings()).items():
        print(f"{name:18} {result}")


def cmd_seed_characters(_: argparse.Namespace) -> None:
    s = get_settings()
    r = characters.load_seed(_db(), characters.parse_seed(s.seed_characters))
    print(f"loaded {r.loaded} characters")
    if r.orphaned:
        print(
            f"WARNING: {len(r.orphaned)} seed characters are no longer in the seed file "
            f"and were kept in the database: {', '.join(r.orphaned)}"
        )


def cmd_catalog_fetch(_: argparse.Namespace) -> None:
    s = get_settings()
    try:
        counts = youtube.fetch_channel(s.youtube_channel, s.catalog_dir)
    except youtube.FetchIncomplete as e:
        for name, err in e.errors.items():
            print(f"FAILED  {name}: {err}")
        raise SystemExit("catalog fetch incomplete; rerun to fetch only the missing ones") from e
    for name, n in counts.items():
        print(f"{n:6}  {name}")


def cmd_catalog_import(args: argparse.Namespace) -> None:
    conn = _db()
    try:
        n = youtube.import_raw(conn, get_settings().catalog_dir, force=args.force)
    except youtube.TooManyMissing as e:
        raise SystemExit(f"catalog import refused: {e}") from e
    gone = conn.execute("SELECT count(*) FROM videos WHERE missing=1").fetchone()[0]
    print(f"imported {n} videos, {gone} missing from the channel")


def cmd_catalog_build(args: argparse.Namespace) -> None:
    conn = _db()
    busy = conn.execute(
        "SELECT episode_id, step FROM episode_steps WHERE status='running' "
        "AND updated_at > datetime('now', '-6 hours')"
    ).fetchall()
    if busy and not args.force:
        # A re-point while a step runs leaves the old video's data behind (review, phase 1).
        raise SystemExit(
            f"steps are running {[tuple(b) for b in busy]}: stop the worker first "
            "(scripts/worker.sh stop), or use --force"
        )
    r = classify.build_episodes(conn)
    print(f"episodes: {r.episodes}, specials: {r.specials}")
    print(f"videos by kind: {dict(sorted(r.by_kind.items()))}")
    print(f"duplicate episode numbers: {r.duplicates}")
    print(f"missing numbers ({len(r.missing_numbers)}): {r.missing_numbers}")
    print(f"season inferred from neighbours: {r.season_inferred}")
    print(f"videos missing from the channel: {r.missing_videos}")
    print(f"steps reset (video changed, processing redone): {r.steps_reset}")
    no_season = [
        x[0] for x in conn.execute("SELECT id FROM episodes WHERE season IS NULL AND retired=0")
    ]
    print(f"no season: {no_season}")
    odd = conn.execute(
        """SELECT title, playlists FROM videos
           WHERE kind != 'episode' AND missing=0 AND playlists LIKE '%DRŽAVNI POSAO %'"""
    ).fetchall()
    print(f"non-episodes inside season playlists: {[tuple(o) for o in odd]}")


def cmd_catalog_stats(_: argparse.Namespace) -> None:
    conn = _db()
    sql = "SELECT duration_s FROM episodes WHERE duration_s AND kind='episode' AND retired=0"
    durs = [r[0] for r in conn.execute(sql)]
    if not durs:
        print("no episodes with duration; run catalog-build first")
        return
    durs.sort()
    total = sum(durs)
    q = lambda p: durs[min(len(durs) - 1, int(p * len(durs)))]  # noqa: E731
    mean_min = total / len(durs) / 60
    print(f"episodes with duration: {len(durs)}, total {total / 3600:.1f} h")
    print(f"mean {mean_min:.2f} min")
    print(f"min {durs[0]}s p10 {q(0.1)}s p50 {q(0.5)}s p90 {q(0.9)}s max {durs[-1]}s")
    print("histogram (minutes):")
    for lo in range(0, durs[-1] // 60 + 1):
        n = sum(1 for d in durs if lo * 60 <= d < (lo + 1) * 60)
        if n:
            print(f"  {lo:2}-{lo + 1:2} min  {n:5}")
    print("per season: episodes, hours, first-last episode, first-last date")
    for row in conn.execute(
        """SELECT season, count(*), sum(duration_s)/3600.0, min(id), max(id),
                  min(air_date), max(air_date) FROM episodes WHERE kind='episode' AND retired=0
           GROUP BY season ORDER BY season"""
    ):
        print("  ", tuple(row))
    print("specials: id, season, title, air date, duration")
    for row in conn.execute(
        """SELECT id, season, title, air_date, duration_s FROM episodes
           WHERE kind='special' AND retired=0"""
    ):
        print("  ", tuple(row))


def cmd_export_episodes(args: argparse.Namespace) -> None:
    n = export.export_episodes(_db(), Path(args.path))
    print(f"exported {n} episodes to {args.path}")


def cmd_pilot_select(_: argparse.Namespace) -> None:
    conn = _db()
    chosen = pilot.select(conn, pilot.parse_owner(get_settings().seed_pilot))
    pilot.store(conn, chosen)
    for p in chosen:
        ep = conn.execute(
            "SELECT season, title, duration_s FROM episodes WHERE id=?", (p.episode_id,)
        ).fetchone()
        season = "-" if ep["season"] is None else ep["season"]
        dur = "-" if ep["duration_s"] is None else ep["duration_s"]
        print(f"{p.grp:17} {p.episode_id:6}  s{season!s:<3} {dur!s:>5}s  {ep['title']}")
    print(f"{len(chosen)} pilot episodes stored")


def _episode_ids(conn: sqlite3.Connection, args: argparse.Namespace) -> list[int]:
    if args.episodes:
        return [int(x) for x in args.episodes.split(",")]
    return [r[0] for r in conn.execute("SELECT episode_id FROM pilot ORDER BY episode_id")]


def _download_pending(conn: sqlite3.Connection, eid: int, force: bool) -> bool:
    """True when run_step will really hit YouTube for this episode."""
    vid = active_video_id(conn, eid)
    # No active video: run_episode rejects it before any request.
    return vid is not None and pipeline.will_run(conn, eid, "download", vid, force)


def cmd_run(args: argparse.Namespace) -> None:
    s = get_settings()
    conn = _db()
    steps = args.steps.split(",")
    unknown = set(steps) - set(pipeline.STEPS)
    if unknown:
        raise SystemExit(f"unknown steps: {sorted(unknown)}; known: {pipeline.STEPS}")
    if args.rebuild_index and "index" not in steps:
        # Otherwise it would do nothing and exit 0, leaving every vector from the old model.
        raise SystemExit("--rebuild-index only acts on the index step: add --steps index")
    ids = _episode_ids(conn, args)[: args.limit or None]
    failed = []
    for n, eid in enumerate(ids):
        attempted = False
        try:
            # A failed download is a request to YouTube too, so it also needs the pause.
            attempted = "download" in steps and _download_pending(conn, eid, args.force)
            res = transcribe_steps.run_episode(
                s, conn, eid, steps, force=args.force, rebuild_index=args.rebuild_index
            )
            print(f"{eid}: {json.dumps(res, ensure_ascii=False)}", flush=True)
        except Exception as e:  # keep going; the failure is recorded in episode_steps
            log.exception("episode %s failed", eid)
            failed.append(eid)
            print(f"{eid}: FAILED {type(e).__name__}: {e}", flush=True)
        if attempted and n < len(ids) - 1:
            transcribe_steps.polite_pause(s)
    if failed:
        raise SystemExit(f"{len(failed)} episode(s) failed: {failed}")


def cmd_label_transcripts(_: argparse.Namespace) -> None:
    res = transcribe_steps.label_transcripts(get_settings(), _db())
    print(f"labelled: {res['labelled']}, already labelled: {res['already_labelled']}, "
          f"skipped: {len(res['skipped'])}")  # fmt: skip
    for name, reason in res["skipped"].items():
        print(f"  skipped {name}: {reason}")


def cmd_enrich(args: argparse.Namespace) -> None:
    s = get_settings()
    conn = _db()
    ids = _episode_ids(conn, args)[: args.limit or None]
    failed = []
    for eid in ids:
        info = enrich_run.enrich_episode(s, conn, eid, args.model, force=args.force)
        print(f"{eid}: {json.dumps(info, ensure_ascii=False)}", flush=True)
        if info and "error" in info:
            failed.append(eid)
    if failed:
        raise SystemExit(f"{len(failed)} episode(s) failed: {failed}")


def cmd_fetch_video(args: argparse.Namespace) -> None:
    """Download only the video of episodes (no step changes), with the YouTube pause."""
    s = get_settings()
    conn = _db()
    if s.video_height <= 0:
        raise SystemExit("[download] video_height is 0 in config.toml")
    ids = _episode_ids(conn, args)[: args.limit or None]
    failed = []
    for n, eid in enumerate(ids):
        vid = active_video_id(conn, eid)
        if vid is None:
            print(f"{eid}: not in the catalog, skipped", flush=True)
            continue
        if frames.video_path(s.data_dir, eid).exists() and not args.force:
            print(f"{eid}: video already there", flush=True)
            continue
        if n:
            transcribe_steps.polite_pause(s)
        try:
            info = download.fetch_video(vid, eid, s.data_dir, s.video_height)
            print(f"{eid}: {json.dumps(info)}", flush=True)
        except Exception:
            log.exception("episode %s: video not downloaded", eid)
            failed.append(eid)
    if failed:
        raise SystemExit(f"{len(failed)} episode(s) failed: {failed}")


def cmd_voice_profiles(_: argparse.Namespace) -> None:
    s = get_settings()
    info = speakers.build_profiles(
        s, _db(), speakers.get_embedder(s), s.seed_characters.parent / "voices.yaml"
    )
    print(json.dumps(info, ensure_ascii=False, indent=1))


def cmd_eval(args: argparse.Namespace) -> None:
    s = get_settings()
    res = eval_run.evaluate(
        _db(), Path(args.questions), lambda texts: embed_client.embed(s.embed_url, texts)
    )
    for q in res["questions"]:
        mark = "-" if not q["in_index"] else (q["rank"] or "x")
        print(f"[{mark:>2}] {q['expected']}  top={q['top']}  {q['query'][:70]}")
    print(f"top1 {res['top1']}  top5 {res['top5']}  top20 {res['top20']} (questions in the index)")


def cmd_search(args: argparse.Namespace) -> None:
    s = get_settings()
    hits = index_search.search(
        _db(), args.query, lambda t: embed_client.embed(s.embed_url, t), limit=args.limit
    )
    for h in hits:
        t = h["start_s"]
        print(f"{h['episode_id']:>5} {t // 60}:{t % 60:02d} {h['kind']:<10} {h['snippet'][:90]!r}")


def cmd_backup(args: argparse.Namespace) -> None:
    src = connect(get_settings().db, readonly=True)
    dst = sqlite3.connect(args.path)
    src.backup(dst)
    dst.close()
    print(f"backup written to {args.path}")


def cmd_worker(args: argparse.Namespace) -> None:
    def stop(signum: int, _frame: object) -> None:
        # docker stop sends SIGTERM to PID 1: leave cleanly, the step goes back to pending.
        raise SystemExit(f"signal {signum}")

    signal.signal(signal.SIGTERM, stop)
    s = get_settings()
    res = worker.run(s, _db(), limit=args.limit or None, limits=worker.Limits(**s.worker_limits))
    print(json.dumps(res))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    p = argparse.ArgumentParser(prog="dp")
    sub = p.add_subparsers(required=True)
    sub.add_parser("init").set_defaults(func=cmd_init)
    sub.add_parser("check-services").set_defaults(func=cmd_check_services)
    sub.add_parser("seed-characters").set_defaults(func=cmd_seed_characters)
    sub.add_parser("catalog-fetch").set_defaults(func=cmd_catalog_fetch)
    imp = sub.add_parser("catalog-import")
    imp.add_argument(
        "--force", action="store_true", help="accept even if many videos become missing"
    )
    imp.set_defaults(func=cmd_catalog_import)
    cb = sub.add_parser("catalog-build")
    cb.add_argument("--force", action="store_true", help="even while steps are running")
    cb.set_defaults(func=cmd_catalog_build)
    sub.add_parser("catalog-stats").set_defaults(func=cmd_catalog_stats)
    sub.add_parser("pilot-select").set_defaults(func=cmd_pilot_select)
    run = sub.add_parser("run")
    run.add_argument("--steps", default="download,prepare,transcribe")
    run.add_argument("--episodes", help="comma-separated ids; default: the pilot set")
    run.add_argument("--limit", type=int, default=0)
    run.add_argument("--force", action="store_true", help="redo steps already done")
    run.add_argument(
        "--rebuild-index",
        action="store_true",
        help="recompute all embeddings; use after changing the embedding model. Needs index "
        "in --steps (e.g. --steps index --rebuild-index) and runs it even if done. "
        "--force alone updates the index incrementally",
    )
    run.set_defaults(func=cmd_run)
    sub.add_parser(
        "label-transcripts",
        help="one-off: write video_id into pilot transcripts whose transcribe step is done "
        "for the episode's current video (safe to repeat)",
    ).set_defaults(func=cmd_label_transcripts)
    en = sub.add_parser("enrich", help="describe episodes with an LLM ([enrich.models])")
    en.add_argument("--model", required=True, help="config name, e.g. qwen or glm")
    en.add_argument("--episodes", help="comma-separated ids; default: the pilot set")
    en.add_argument("--limit", type=int, default=0)
    en.add_argument("--force", action="store_true", help="redo episodes already described")
    en.set_defaults(func=cmd_enrich)
    fv = sub.add_parser("fetch-video", help="download only the video (for frames)")
    fv.add_argument("--episodes", help="comma-separated ids; default: the pilot set")
    fv.add_argument("--limit", type=int, default=0)
    fv.add_argument("--force", action="store_true", help="download again if present")
    fv.set_defaults(func=cmd_fetch_video)
    sub.add_parser(
        "voice-profiles", help="build actor voice profiles from seed/voices.yaml"
    ).set_defaults(func=cmd_voice_profiles)
    ev = sub.add_parser("eval", help="test questions through search")
    ev.add_argument("--questions", default="/app/seed/test_questions.yaml")
    ev.set_defaults(func=cmd_eval)
    se = sub.add_parser("search", help="search the index from the command line")
    se.add_argument("query")
    se.add_argument("--limit", type=int, default=10)
    se.set_defaults(func=cmd_search)
    ex = sub.add_parser("export-episodes")
    ex.add_argument("path")
    ex.set_defaults(func=cmd_export_episodes)
    b = sub.add_parser("backup")
    b.add_argument("path")
    b.set_defaults(func=cmd_backup)
    wk = sub.add_parser("worker", help="phase 2: process episodes until all are done")
    wk.add_argument("--limit", type=int, default=0, help="stop after this many episodes")
    wk.set_defaults(func=cmd_worker)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
