"""Pick the 40 pilot episodes (PLAN.md, faza 1) and store them in the pilot table.

Owner's 10 come from seed/pilot.yaml; the other 30 are chosen deterministically:
10 evenly spread over episodes 1–50 and 20 evenly spread over the rest of the series.
"""

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import yaml

OWNER_GROUPS = {"gosti_i_dvostruke_uloge": "owner_guests", "zapamcene": "owner_remembered"}


@dataclass(frozen=True)
class PilotEpisode:
    episode_id: int
    grp: str
    reason: str | None


def parse_owner(path: Path) -> list[PilotEpisode]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: list[PilotEpisode] = []
    for key, grp in OWNER_GROUPS.items():
        for item in data.get(key) or []:
            out.append(PilotEpisode(int(item["epizoda"]), grp, item.get("zasto")))
    return out


def spread(candidates: list[int], k: int) -> list[int]:
    """k items evenly spaced through candidates (middle of each of k equal bins)."""
    if k >= len(candidates):
        return list(candidates)
    n = len(candidates)
    return [candidates[int((i + 0.5) * n / k)] for i in range(k)]


def select(conn: sqlite3.Connection, owner: list[PilotEpisode]) -> list[PilotEpisode]:
    active = {
        r[0]
        for r in conn.execute("SELECT id FROM episodes WHERE retired=0 AND video_id IS NOT NULL")
    }
    unknown = [p.episode_id for p in owner if p.episode_id not in active]
    if unknown:
        raise ValueError(f"pilot.yaml refers to episodes not in the catalog: {unknown}")
    taken = {p.episode_id for p in owner}
    regular = sorted(
        r[0]
        for r in conn.execute(
            "SELECT id FROM episodes WHERE retired=0 AND kind='episode' AND video_id IS NOT NULL"
        )
        if r[0] not in taken
    )
    first = spread([e for e in regular if e <= 50], 10)
    rest = spread([e for e in regular if e > 50], 20)
    return [
        *owner,
        *(PilotEpisode(e, "first_50", None) for e in first),
        *(PilotEpisode(e, "spread", None) for e in rest),
    ]


def store(conn: sqlite3.Connection, pilot: list[PilotEpisode]) -> None:
    with conn:
        conn.execute("DELETE FROM pilot")
        conn.executemany(
            "INSERT INTO pilot (episode_id, grp, reason) VALUES (?, ?, ?)",
            [(p.episode_id, p.grp, p.reason) for p in pilot],
        )
