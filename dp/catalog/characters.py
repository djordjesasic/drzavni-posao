"""Load the seed character registry (seed/characters.yaml) into the database."""

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from dp.text import fold

KINDS = {"glavni", "na_ekranu", "samo_u_prici", "samo_glas"}


@dataclass
class Character:
    id: str
    name: str
    kind: str
    played_by: str | None
    description: str | None
    aliases: list[str] = field(default_factory=list)


def _as_str(value: object, what: str, where: str) -> str:
    if not isinstance(value, str):
        raise ValueError(
            f"{where}: {what} must be a string, got {value!r} ({type(value).__name__}); "
            "quote it in YAML"
        )
    return value


def _required_str(item: dict[str, object], key: str, where: str) -> str:
    if item.get(key) is None:
        raise ValueError(f"{where}: missing {key!r}")
    value = _as_str(item[key], f"{key!r}", where)
    if not value.strip():
        raise ValueError(f"{where}: {key!r} must not be empty")
    return value


def _optional_str(item: dict[str, object], key: str, where: str) -> str | None:
    value = item.get(key)
    if value is None:
        return None
    return _as_str(value, f"{key!r}", where) or None


def parse_seed(path: Path) -> list[Character]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is not None and not isinstance(data, dict):
        raise ValueError(
            f"{path}: top level must be a mapping of groups, got {type(data).__name__}"
        )
    chars: list[Character] = []
    seen: set[str] = set()
    for group_name, group in (data or {}).items():
        if group is None:
            continue
        if not isinstance(group, list):
            raise ValueError(
                f"group {group_name!r}: must be a list of characters, got {type(group).__name__}"
            )
        for index, item in enumerate(group, start=1):
            where = f"item {index} in group {group_name!r}"
            if not isinstance(item, dict):
                raise ValueError(f"{where}: must be a mapping, got {type(item).__name__}")
            cid = _required_str(item, "id", where)
            where = f"{cid} in group {group_name!r}"
            if cid in seen:
                raise ValueError(f"duplicate character id: {cid}")
            name = _required_str(item, "ime", where)
            kind = _required_str(item, "vrsta", where)
            if kind not in KINDS:
                raise ValueError(f"{cid}: unknown vrsta {item['vrsta']!r}")
            seen.add(cid)
            nadimci = item.get("nadimci")
            if nadimci is None:
                nadimci = []
            if not isinstance(nadimci, list):
                raise ValueError(
                    f"{where}: 'nadimci' must be a list of strings, "
                    f"got {nadimci!r} ({type(nadimci).__name__})"
                )
            aliases = [name, *(_as_str(a, "'nadimci' entry", where) for a in nadimci)]
            chars.append(
                Character(
                    id=cid,
                    name=name,
                    kind=kind,
                    played_by=_optional_str(item, "igra", where),
                    description=_optional_str(item, "opis", where),
                    aliases=list(dict.fromkeys(a.strip() for a in aliases if a.strip())),
                )
            )
    if not chars:
        raise ValueError(f"{path}: seed has no characters")
    for c in chars:
        if c.played_by and c.played_by not in seen:
            raise ValueError(f"{c.id}: igra refers to unknown id {c.played_by!r}")
    return chars


@dataclass
class SeedResult:
    loaded: int
    # Seed characters in the database that are no longer in the YAML. They are kept,
    # because other tables may reference them, but lose their seed aliases; the caller
    # should warn about them.
    orphaned: list[str]


def load_seed(conn: sqlite3.Connection, chars: list[Character]) -> SeedResult:
    """Upsert seed characters and replace their seed aliases.

    Aliases from other sources (pipeline, owner) are left untouched.
    An empty list is refused: it would orphan every seed character.
    """
    if not chars:
        raise ValueError("refusing to load an empty seed")
    with conn:
        # played_by references other rows, so insert all characters first.
        for c in chars:
            conn.execute(
                """INSERT INTO characters (id, name, kind, description, source)
                   VALUES (?, ?, ?, ?, 'seed')
                   ON CONFLICT(id) DO UPDATE SET
                     name=excluded.name, kind=excluded.kind, description=excluded.description,
                     source='seed'""",
                (c.id, c.name, c.kind, c.description),
            )
        for c in chars:
            conn.execute("UPDATE characters SET played_by=? WHERE id=?", (c.played_by, c.id))
            conn.execute("DELETE FROM aliases WHERE character_id=? AND source='seed'", (c.id,))
            # OR IGNORE: if a pipeline/owner alias already has the same (character_id, alias),
            # it is kept with its own source; the alias resolves to the same character anyway.
            conn.executemany(
                """INSERT OR IGNORE INTO aliases (character_id, alias, alias_fold, source)
                   VALUES (?, ?, ?, 'seed')""",
                [(c.id, a, fold(a)) for a in c.aliases],
            )
        ids = {c.id for c in chars}
        orphaned = [
            r[0]
            for r in conn.execute("SELECT id FROM characters WHERE source='seed' ORDER BY id")
            if r[0] not in ids
        ]
        # Orphaned characters stay (other tables may reference them), but their old seed
        # names must no longer resolve to them; non-seed aliases are kept.
        conn.executemany(
            "DELETE FROM aliases WHERE character_id=? AND source='seed'",
            [(cid,) for cid in orphaned],
        )
    return SeedResult(loaded=len(chars), orphaned=orphaned)
