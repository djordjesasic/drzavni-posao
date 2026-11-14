import re
from pathlib import Path

import pytest

from dp.catalog.characters import load_seed, parse_seed
from dp.db import connect, migrate
from dp.search.catalog import find_characters

SEED = Path(__file__).parent.parent / "seed" / "characters.yaml"


def test_seed_parses() -> None:
    chars = parse_seed(SEED)
    ids = {c.id for c in chars}
    assert len(chars) == len(ids) >= 36
    stevica = next(c for c in chars if c.id == "stevica_cvarkov")
    assert stevica.played_by == "cvarkov"
    assert "Stevica" in stevica.aliases


def test_load_seed_is_idempotent(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    migrate(conn)
    chars = parse_seed(SEED)
    load_seed(conn, chars)
    load_seed(conn, chars)
    assert conn.execute("SELECT count(*) FROM characters").fetchone()[0] == len(chars)
    row = conn.execute(
        "SELECT character_id FROM aliases WHERE alias_fold = ?", ("cevara",)
    ).fetchone()
    assert row["character_id"] == "cevara_marjanovic"


def test_seed_reload_keeps_non_seed_aliases(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    migrate(conn)
    chars = parse_seed(SEED)
    load_seed(conn, chars)
    with conn:
        conn.execute(
            """INSERT INTO aliases (character_id, alias, alias_fold, source)
               VALUES ('cevara_marjanovic', 'Čeki', 'ceki', 'pipeline')"""
        )
        # A non-seed alias that duplicates a seed alias must not break the reload.
        conn.execute(
            "UPDATE aliases SET source='owner' WHERE character_id='stevica_cvarkov' "
            "AND alias='Stevica'"
        )
    load_seed(conn, chars)
    row = conn.execute(
        "SELECT character_id, source FROM aliases WHERE alias_fold='ceki'"
    ).fetchone()
    assert tuple(row) == ("cevara_marjanovic", "pipeline")
    row = conn.execute(
        "SELECT source FROM aliases WHERE character_id='stevica_cvarkov' AND alias='Stevica'"
    ).fetchone()
    assert row["source"] == "owner"


def test_removed_seed_character_is_reported_and_kept(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    migrate(conn)
    chars = parse_seed(SEED)
    assert load_seed(conn, chars).orphaned == []
    # Drop a character that nobody refers to via played_by.
    referenced = {c.played_by for c in chars}
    gone = next(c for c in chars if c.id not in referenced and not c.played_by)
    result = load_seed(conn, [c for c in chars if c.id != gone.id])
    assert result.loaded == len(chars) - 1
    assert result.orphaned == [gone.id]
    assert conn.execute("SELECT 1 FROM characters WHERE id=?", (gone.id,)).fetchone()
    # Seed aliases of the orphan are gone, so its old names no longer resolve to it.
    assert not conn.execute("SELECT 1 FROM aliases WHERE character_id=?", (gone.id,)).fetchone()
    assert gone.id not in {c["id"] for c in find_characters(conn, gone.name)}


def test_orphan_keeps_non_seed_aliases(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    migrate(conn)
    chars = parse_seed(SEED)
    load_seed(conn, chars)
    referenced = {c.played_by for c in chars}
    gone = next(c for c in chars if c.id not in referenced and not c.played_by)
    with conn:
        conn.execute(
            "INSERT INTO aliases (character_id, alias, alias_fold, source) "
            "VALUES (?, 'Owner Name', 'owner name', 'owner')",
            (gone.id,),
        )
    load_seed(conn, [c for c in chars if c.id != gone.id])
    rows = conn.execute("SELECT alias FROM aliases WHERE character_id=?", (gone.id,)).fetchall()
    assert [r["alias"] for r in rows] == ["Owner Name"]


def test_existing_character_becomes_seed(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    migrate(conn)
    chars = parse_seed(SEED)
    c = chars[0]
    with conn:
        conn.execute(
            "INSERT INTO characters (id, name, kind, source) VALUES (?, ?, ?, 'pipeline')",
            (c.id, c.name, c.kind),
        )
    load_seed(conn, chars)
    row = conn.execute("SELECT source FROM characters WHERE id=?", (c.id,)).fetchone()
    assert row["source"] == "seed"


def test_empty_seed_is_refused(tmp_path: Path) -> None:
    for text in ("", "glavni: []\n"):
        seed = tmp_path / "seed.yaml"
        seed.write_text(text, encoding="utf-8")
        with pytest.raises(ValueError, match="no characters"):
            parse_seed(seed)
    conn = connect(tmp_path / "t.sqlite")
    migrate(conn)
    with pytest.raises(ValueError, match="empty seed"):
        load_seed(conn, [])


BASE = "glavni:\n  - id: a\n    ime: A\n    vrsta: glavni\n"


@pytest.mark.parametrize(
    ("text", "match"),
    [
        pytest.param("- a\n- b\n", "top level must be a mapping", id="top-level-list"),
        pytest.param("glavni: abc\n", "group 'glavni': must be a list", id="group-string"),
        pytest.param(
            "glavni:\n  - abc\n", "item 1 in group 'glavni': must be a mapping", id="item-string"
        ),
        pytest.param(
            "glavni:\n  - ime: A\n    vrsta: glavni\n",
            "item 1 in group 'glavni': missing 'id'",
            id="missing-id",
        ),
        pytest.param(
            'glavni:\n  - id: ""\n    ime: A\n    vrsta: glavni\n',
            "item 1 in group 'glavni': 'id' must not be empty",
            id="empty-id",
        ),
        pytest.param(
            "glavni:\n  - id: a\n    vrsta: glavni\n",
            "a in group 'glavni': missing 'ime'",
            id="missing-ime",
        ),
        pytest.param(
            "glavni:\n  - id: a\n    ime: A\n",
            "a in group 'glavni': missing 'vrsta'",
            id="missing-vrsta",
        ),
        pytest.param(
            "glavni:\n  - id: 007\n    ime: A\n    vrsta: glavni\n",
            "item 1 in group 'glavni': 'id' must be a string, got 7 (int); quote it",
            id="id-int",
        ),
        pytest.param(
            "glavni:\n  - id: a\n    ime: yes\n    vrsta: glavni\n",
            "a in group 'glavni': 'ime' must be a string, got True (bool); quote it",
            id="ime-bool",
        ),
        pytest.param(
            BASE + "    nadimci: Cvare\n",
            "a in group 'glavni': 'nadimci' must be a list of strings",
            id="nadimci-string",
        ),
        pytest.param(
            BASE + "    nadimci: [007]\n",
            "a in group 'glavni': 'nadimci' entry must be a string, got 7 (int); quote it",
            id="nadimci-int",
        ),
        pytest.param(
            BASE + "    nadimci: [yes]\n",
            "a in group 'glavni': 'nadimci' entry must be a string, got True (bool)",
            id="nadimci-bool",
        ),
        pytest.param(
            BASE + "    igra: 5\n",
            "a in group 'glavni': 'igra' must be a string, got 5 (int); quote it",
            id="igra-int",
        ),
        pytest.param(
            BASE + "    opis: [x]\n",
            "a in group 'glavni': 'opis' must be a string, got ['x'] (list)",
            id="opis-list",
        ),
    ],
)
def test_malformed_seed_is_refused(tmp_path: Path, text: str, match: str) -> None:
    seed = tmp_path / "seed.yaml"
    seed.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=re.escape(match)):
        parse_seed(seed)


def test_quoted_string_values_are_accepted(tmp_path: Path) -> None:
    # "da" is a plain string in YAML 1.1 (only yes/no/on/off/true/false are bools).
    seed = tmp_path / "seed.yaml"
    seed.write_text(
        BASE + "    nadimci: [da, '007']\n    igra: null\n    opis: ''\n",
        encoding="utf-8",
    )
    (char,) = parse_seed(seed)
    assert char.aliases == ["A", "da", "007"]
    assert char.played_by is None
    assert char.description is None


def test_minimal_valid_seed_parses(tmp_path: Path) -> None:
    seed = tmp_path / "seed.yaml"
    seed.write_text(BASE + "prazna:\n", encoding="utf-8")
    (char,) = parse_seed(seed)
    assert (char.id, char.name, char.kind) == ("a", "A", "glavni")
    assert char.played_by is None
    assert char.description is None
    assert char.aliases == ["A"]


def test_jove_resolves_to_dragan_and_jovo_is_separate(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    migrate(conn)
    load_seed(conn, parse_seed(SEED))
    owner = conn.execute(
        "SELECT character_id FROM aliases WHERE alias_fold='dragan jove torbica'"
    ).fetchone()
    assert owner["character_id"] == "torbica"
    assert conn.execute("SELECT 1 FROM characters WHERE id='jovo_torbica'").fetchone()
