"""Prompt for the episode description (enrichment) and the JSON shape it asks for."""

import json
import sqlite3
from pathlib import Path
from typing import Any

from dp.transcribe.speakers import OTHER, SURE_SCORE

# Bump when the prompt or schema changes; results are stored per version.
PROMPT_VERSION = 2

SCHEMA: dict[str, Any] = {
    "summary": "3-5 rečenica: šta se dešava u epizodi, ko šta radi, kako se završava",
    "topics": ["glavne teme, kratko"],
    "current_event": "aktuelni događaj koji epizoda komentariše, ili null",
    "opening_scene": {
        "description": "šta se dešava na početku epizode",
        "present": ["id lika iz registra ili ime"],
        "absent_or_late": ["ko kasni ili ga nema, ako se to pominje"],
    },
    "characters": [
        {
            "id": "id iz registra ili null ako lik nije u registru",
            "name": "ime kako se pominje",
            "presence": "na_ekranu | pominje_se | imitiran",
        }
    ],
    "stories": [
        {
            "teller": "ko priča (id ili ime)",
            "about": ["o kome (id ili ime)"],
            "period": "iz kog perioda, npr. 'osamdesete', ili null",
            "summary": "kratak sadržaj priče",
            "start": "m:ss",
        }
    ],
    "imitations": [
        {
            "who": "ko imitira (id ili ime)",
            "imitates": "koga ili šta imitira",
            "style": "naglasak, dijalekt ili način govora",
            "start": "m:ss",
        }
    ],
    "quotes": [{"text": "upečatljiva replika, doslovno i kratko", "start": "m:ss"}],
    "places": ["mesta"],
    "organizations": ["firme, institucije, stranke"],
    "objects": ["važni predmeti"],
    "public_figures": ["stvarne javne ličnosti koje se pominju (političari, poznati)"],
    "unknown_names": ["izmišljeni likovi koji nisu u registru"],
    "uncertain": ["mesta gde nisi siguran, npr. nejasan deo transkripta"],
}

_STR: dict[str, Any] = {"type": "string"}
_STR_OR_NULL: dict[str, Any] = {"type": ["string", "null"]}
_STR_LIST: dict[str, Any] = {"type": "array", "items": _STR}


def _obj(props: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object", "properties": props, "required": list(props),
        "additionalProperties": False,
    }  # fmt: skip


# The same shape as SCHEMA, as JSON Schema. llama-server turns it into a grammar and the
# Anthropic API takes it as a structured output, so no model can drift from the keys.
JSON_SCHEMA: dict[str, Any] = _obj(
    {
        "summary": _STR,
        "topics": _STR_LIST,
        "current_event": _STR_OR_NULL,
        "opening_scene": _obj(
            {"description": _STR, "present": _STR_LIST, "absent_or_late": _STR_LIST}
        ),
        "characters": {
            "type": "array",
            "items": _obj(
                {
                    "id": _STR_OR_NULL,
                    "name": _STR,
                    "presence": {"enum": ["na_ekranu", "pominje_se", "imitiran"]},
                }
            ),
        },
        "stories": {
            "type": "array",
            "items": _obj(
                {
                    "teller": _STR,
                    "about": _STR_LIST,
                    "period": _STR_OR_NULL,
                    "summary": _STR,
                    "start": _STR,
                }
            ),
        },
        "imitations": {
            "type": "array",
            "items": _obj({"who": _STR, "imitates": _STR, "style": _STR, "start": _STR}),
        },
        "quotes": {"type": "array", "items": _obj({"text": _STR, "start": _STR})},
        "places": _STR_LIST,
        "organizations": _STR_LIST,
        "objects": _STR_LIST,
        "public_figures": _STR_LIST,
        "unknown_names": _STR_LIST,
        "uncertain": _STR_LIST,
    }
)
assert list(JSON_SCHEMA["properties"]) == list(SCHEMA)

# Used when a series has no seed/enrich_system.md; that file holds the series' own text
# (what the show is, who the main characters are, how speaker tags behave).
SYSTEM_DEFAULT = """Ti si urednik baze znanja o jednoj seriji. Dobijaš automatski transkript \
jedne epizode (Whisper, sa greškama), registar likova i kratku pozadinu serije.

Pravila:
- Odgovori isključivo jednim JSON objektom tačno po zadatoj šemi, bez ikakvog drugog teksta.
- Za likove iz registra koristi njihov id. Ne izmišljaj likove ni veze među njima.
- Ako nešto nije jasno iz transkripta, ne nagađaj: izostavi ili navedi u uncertain.
- Vremena (start) uzmi iz oznaka [m:ss] u transkriptu.
- Prazne liste ostavi prazne; ne popunjavaj ih na silu."""
SYSTEM_FILE = "enrich_system.md"


def load_system(seed_dir: Path) -> str:
    """The series' system prompt (seed/enrich_system.md), else SYSTEM_DEFAULT."""
    path = seed_dir / SYSTEM_FILE
    return path.read_text(encoding="utf-8").rstrip("\n") if path.exists() else SYSTEM_DEFAULT


FRAMES_SYSTEM = """

Posle transkripta dobijaš i kadrove iz videa, svaki sa vremenom [m:ss]. Iskoristi ih za ono \
što se vidi, a ne čuje: rekviziti i hrana, kostimi i prerušavanja, mesto radnje (kancelarija \
ili napolju), ko je u kadru, gegovi bez reči, i kad neko glumi ili se pretvara. To upiši u \
summary i opening_scene, važne predmete u objects. Lik prepoznaj po izgledu samo ako je jasno \
iz konteksta (ime, transkript); inače ga opiši rečima („čovek u vampirskom ogrtaču“) i navedi \
u uncertain. Kadar ne opisuj sam za sebe: povezuj ga sa onim što se govori.
Poenta je često pred kraj epizode: ako poslednji kadrovi otkrivaju zašto se neko onako \
ponašao (glumio, pretvarao se, imao skriveni razlog), napiši to izričito u summary. Hranu i \
predmete imenuj što konkretnije (npr. „pečeni pileći batak“, ne samo „hrana“) i reci ko ih \
drži."""


def _registry(conn: sqlite3.Connection) -> str:
    lines = []
    for r in conn.execute("SELECT id, name, kind, played_by, description FROM characters"):
        aliases = [
            a[0]
            for a in conn.execute(
                "SELECT alias FROM aliases WHERE character_id=? ORDER BY alias", (r["id"],)
            )
            if a[0] != r["name"]
        ]
        parts = [f"{r['id']}: {r['name']} ({r['kind']})"]
        if aliases:
            parts.append("zovu ga: " + ", ".join(aliases))
        if r["played_by"]:
            parts.append(f"igra ga glumac lika {r['played_by']}")
        if r["description"]:
            parts.append(r["description"])
        lines.append("- " + "; ".join(parts))
    return "\n".join(lines)


def _mmss(ms: int) -> str:
    s = ms // 1000
    return f"{s // 60}:{s % 60:02d}"


def _voice(actor: str | None, score: float | None) -> str:
    """Speaker tag from the voice step: "<cvarkov>", "<cvarkov?>" if unsure, "<drugi>"."""
    if actor is None:
        return ""
    if actor == OTHER:
        return "<drugi> "
    unsure = "?" if score is not None and score < SURE_SCORE else ""
    return f"<{actor}{unsure}> "


def transcript(conn: sqlite3.Connection, episode_id: int) -> str:
    rows = conn.execute(
        "SELECT start_ms, text, actor, actor_score FROM segments WHERE episode_id=? ORDER BY idx",
        (episode_id,),
    )
    return "\n".join(
        f"[{_mmss(r['start_ms'])}] {_voice(r['actor'], r['actor_score'])}{r['text']}" for r in rows
    )


def build_parts(
    conn: sqlite3.Connection, episode_id: int, background: str = "", system: str = SYSTEM_DEFAULT
) -> tuple[str, str, str]:
    """(system, stable, episode): `stable` (registry + schema) is the same for every
    episode and comes first, so prompt caches can reuse it."""
    ep = conn.execute(
        "SELECT id, title, air_date FROM episodes WHERE id=?", (episode_id,)
    ).fetchone()
    if ep is None:
        raise ValueError(f"episode {episode_id} not in the catalog")
    text = transcript(conn, episode_id)
    if not text:
        raise ValueError(f"episode {episode_id} has no transcript")
    stable = (
        (f"POZADINA SERIJE:\n{background.strip()}\n\n" if background.strip() else "")
        + f"REGISTAR LIKOVA:\n{_registry(conn)}\n\n"
        + f"ŠEMA ODGOVORA:\n{json.dumps(SCHEMA, ensure_ascii=False, indent=1)}\n\n"
    )
    episode = (
        f"EPIZODA {ep['id']}: „{ep['title']}“, emitovana {ep['air_date'] or 'nepoznato'}\n"
        f"TRANSKRIPT:\n{text}"
    )
    return system, stable, episode


def to_messages(parts: tuple[str, str, str]) -> list[dict[str, str]]:
    """Chat messages for OpenAI-compatible APIs."""
    system, stable, episode = parts
    return [{"role": "system", "content": system}, {"role": "user", "content": stable + episode}]


def build_messages(conn: sqlite3.Connection, episode_id: int) -> list[dict[str, str]]:
    return to_messages(build_parts(conn, episode_id))
