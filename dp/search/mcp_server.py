"""MCP tools over the catalog and the content index (Streamable HTTP, mounted by dp.search.app)."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from dp.config import get_settings
from dp.search import catalog, content
from dp.search.deps import DatabaseNotReady, open_db

# Used when a series has no seed/mcp_instructions.md (the series' own guidance for the
# model: what the show is, double roles, how specials are numbered...).
INSTRUCTIONS_DEFAULT = """\
Baza znanja jedne serije: katalog svih epizoda, registar likova i, za obrađene epizode,
transkript (sa govornikom prepoznatim po glasu) i opis epizode.

Kad korisnik opiše epizodu po sećanju: razloži opis na kratke signale, pozovi
search_episodes više puta (jedan signal po upitu), za najbolje kandidate pročitaj
get_transcript_excerpt oko vraćenog minuta i vrati najviše 3 kandidata sa linkom na minut.
Ako nijedan ne odgovara, reci to.
"""


def _instructions() -> str:
    path = get_settings().seed_dir / "mcp_instructions.md"
    return path.read_text(encoding="utf-8") if path.exists() else INSTRUCTIONS_DEFAULT


mcp = MCPServer(name=get_settings().series_name, instructions=_instructions())


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    # Only ToolError messages reach the client; other exceptions become a generic error.
    try:
        with open_db() as conn:
            yield conn
    except DatabaseNotReady as e:
        raise ToolError(str(e)) from e


@mcp.tool()
def search_episodes(query: str, limit: int = 10) -> dict[str, Any]:
    """Search episodes by content: what happens, what is said, who tells what story.

    Searches the transcripts and episode descriptions by words (diacritics-insensitive)
    and by meaning. Use short queries, one memory or quote each, and run several.
    Returns indexed_episodes (how many episodes are searchable) and results, best first:
    episode, title, season, air date, minute and YouTube link of the best matching
    moment, what matched (transkript, opis, priča, imitacija, replika), a snippet and a score.
    limit is 1-20.
    """
    if not query.strip():
        raise ToolError("query must not be empty")
    with _db() as conn:
        return {
            "indexed_episodes": content.indexed_count(conn),
            "results": content.search_content(conn, query, max(1, min(limit, 20))),
        }


@mcp.tool()
def search_titles(
    query: str = "", season: int | None = None, year: int | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    """Search episode titles (case- and diacritics-insensitive; all words must match).

    Optional filters: season number, year of air date; limit is 1-100. Returns id,
    kind (episode or special), season, title, air date, duration in seconds and YouTube link.
    """
    if season is not None and not catalog.SEASON_MIN <= season <= catalog.SEASON_MAX:
        raise ToolError(f"season must be {catalog.SEASON_MIN}-{catalog.SEASON_MAX}")
    if year is not None and not catalog.YEAR_MIN <= year <= catalog.YEAR_MAX:
        raise ToolError(f"year must be {catalog.YEAR_MIN}-{catalog.YEAR_MAX}")
    with _db() as conn:
        return catalog.search_titles(conn, query, season, year, max(1, min(limit, 100)))


@mcp.tool()
def get_episode(episode: int) -> dict[str, Any] | None:
    """Get one episode by its running number (e.g. 2036), with its description when the
    episode has been processed (summary, topics, characters, stories with their minute,
    imitations, quotes); description is null otherwise.

    New Year and other specials have no running number; their ids start at 10001
    and the result has kind='special'.
    """
    if not 1 <= episode <= catalog.EPISODE_ID_MAX:
        return None
    with _db() as conn:
        ep = catalog.get_episode(conn, episode)
        if ep is not None:
            ep["description"] = content.description(conn, episode)
        return ep


@mcp.tool()
def get_transcript_excerpt(
    episode: int, start_s: int = 0, end_s: int | None = None
) -> dict[str, Any] | None:
    """Transcript lines of an episode between two times in seconds (at most 5 minutes
    per call; default: 5 minutes from start_s), each with time m:ss, speaker found by
    voice (a trailing "?" means unsure, null means too short or not one of the three
    main actors) and text, plus a YouTube link to start_s. None if the episode has no
    transcript yet. The transcript is automatic and has recognition errors.
    """
    if not 1 <= episode <= catalog.EPISODE_ID_MAX:
        return None
    with _db() as conn:
        return content.transcript_excerpt(conn, episode, start_s, end_s)


@mcp.tool()
def find_character(name: str) -> list[dict[str, Any]]:
    """Look up characters by name or nickname (diacritics-insensitive substring match).

    Returns canonical id, name, kind (glavni, na_ekranu, samo_u_prici, samo_glas),
    who plays them (for double roles), short description and all aliases.
    """
    with _db() as conn:
        return catalog.find_characters(conn, name)


@mcp.tool()
def catalog_info() -> dict[str, Any]:
    """Catalog numbers: episodes and specials (count, hours), episode number and season
    range, first and last air date, and the first special id."""
    with _db() as conn:
        return catalog.catalog_summary(conn)
