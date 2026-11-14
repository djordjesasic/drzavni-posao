"""MCP tools over the catalog (Streamable HTTP, mounted by dp.search.app)."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from dp.search import catalog
from dp.search.deps import DatabaseNotReady, open_db

INSTRUCTIONS = """\
Baza znanja serije „Državni posao“. Brojeve epizoda, specijala, sezona i raspon
datuma daje alat catalog_info. Epizode imaju redni broj; specijali (kind='special')
nemaju redni broj i imaju id od 10001 naviše.
Faza 0: dostupni su samo katalog epizoda (broj, sezona, naslov, datum, YouTube link)
i registar likova. Transkripti i opisi epizoda još ne postoje, pa pretraga po sadržaju
epizode još ne radi; pretražuju se samo naslovi.
"""

mcp = MCPServer(name="drzavni-posao", instructions=INSTRUCTIONS)


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    # Only ToolError messages reach the client; other exceptions become a generic error.
    try:
        with open_db() as conn:
            yield conn
    except DatabaseNotReady as e:
        raise ToolError(str(e)) from e


@mcp.tool()
def search_episodes(
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
    """Get one episode by its running number (e.g. 2036).

    New Year and other specials have no running number; their ids start at 10001
    and the result has kind='special'.
    """
    if not 1 <= episode <= catalog.EPISODE_ID_MAX:
        return None
    with _db() as conn:
        return catalog.get_episode(conn, episode)


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
