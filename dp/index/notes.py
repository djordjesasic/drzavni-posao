"""The owner's notes on episodes (seed/notes.yaml): what neither the transcript nor the
frames catch ("Torbica only pretends to be on drugs"). Each note is its own search window.

Format, one key per episode:

    254:
      - Torbica glumi da je drogiran da bi sam pojeo ovčiju glavu
      - {tekst: "Ljubi ovčiju glavu", minut: "5:54"}
"""

import re
from pathlib import Path
from typing import Any

import yaml

NOTES_FILE = "notes.yaml"
_MMSS = re.compile(r"^\s*(\d+):(\d{2})\s*$")


def _ms(minute: Any) -> int | None:
    m = _MMSS.match(str(minute)) if minute not in (None, "") else None
    if minute not in (None, "") and not m:
        raise ValueError(f"minut {minute!r}: expected m:ss")
    return (int(m.group(1)) * 60 + int(m.group(2))) * 1000 if m else None


def parse(raw: Any) -> dict[int, list[dict[str, Any]]]:
    """{episode: [{"text", "start_ms"}]}; raises ValueError naming the bad entry."""
    out: dict[int, list[dict[str, Any]]] = {}
    for key, items in (raw or {}).items():
        try:
            eid = int(key)
        except (TypeError, ValueError):
            raise ValueError(f"{key!r}: the key must be an episode number") from None
        for item in items if isinstance(items, list) else [items]:
            if isinstance(item, str):
                text, start = item, None
            elif isinstance(item, dict) and isinstance(item.get("tekst"), str):
                text, start = item["tekst"], _ms(item.get("minut"))
            else:
                raise ValueError(f"episode {eid}: {item!r} is neither text nor {{tekst, minut}}")
            if text.strip():
                out.setdefault(eid, []).append({"text": text.strip(), "start_ms": start})
    return out


def load(seed_dir: Path) -> dict[int, list[dict[str, Any]]]:
    path = seed_dir / NOTES_FILE
    if not path.exists():
        return {}
    return parse(yaml.safe_load(path.read_text(encoding="utf-8")))


def windows(notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"kind": "note", "start_ms": n["start_ms"], "end_ms": None, "text": n["text"]}
        for n in notes
    ]
