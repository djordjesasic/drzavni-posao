"""FastAPI app: HTTP API plus the MCP endpoint at /mcp, both behind a bearer token."""

import hmac
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from mcp.server.transport_security import TransportSecuritySettings

from dp.config import get_settings
from dp.search import catalog
from dp.search.deps import DatabaseNotReady, db_conn
from dp.search.mcp_server import mcp

PUBLIC_PATHS = {"/health"}

mcp_app = mcp.streamable_http_app(
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(allowed_hosts=get_settings().api_allowed_hosts),
)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="Državni posao", lifespan=lifespan)


@app.exception_handler(DatabaseNotReady)
async def database_not_ready(_: Request, exc: DatabaseNotReady) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=503)


@app.middleware("http")
async def require_token(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    if request.url.path in PUBLIC_PATHS:
        return await call_next(request)
    token = get_settings().api_token
    if not token:
        return JSONResponse({"detail": "DP_API_TOKEN is not configured"}, status_code=503)
    # "<scheme> <token>"; the scheme is required and case-insensitive (RFC 7235).
    parts = request.headers.get("authorization", "").split(None, 1)
    scheme, given = parts if len(parts) == 2 else ("", "")
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        given.strip().encode(), token.encode()
    ):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


Conn = Annotated[sqlite3.Connection, Depends(db_conn)]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/catalog")
def catalog_info(conn: Conn) -> dict[str, Any]:
    return catalog.catalog_summary(conn)


@app.get("/episodes")
def search_episodes(
    conn: Conn,
    q: str = "",
    season: Annotated[int | None, Query(ge=catalog.SEASON_MIN, le=catalog.SEASON_MAX)] = None,
    year: Annotated[int | None, Query(ge=catalog.YEAR_MIN, le=catalog.YEAR_MAX)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[dict[str, Any]]:
    return catalog.search_titles(conn, q, season, year, limit)


@app.get("/episodes/{episode_id}")
def get_episode(
    episode_id: Annotated[int, Path(ge=1, le=catalog.EPISODE_ID_MAX)], conn: Conn
) -> dict[str, Any]:
    ep = catalog.get_episode(conn, episode_id)
    if ep is None:
        raise HTTPException(404, "episode not found")
    return ep


@app.get("/characters")
def characters(conn: Conn, q: str = "") -> list[dict[str, Any]]:
    return catalog.find_characters(conn, q)


# Mounted last so the API routes above take precedence; serves /mcp.
app.mount("/", mcp_app)
