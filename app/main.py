"""Minimal authenticated KOL posts API backed by PostgreSQL."""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS posts (
        id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        author TEXT,
        author_id TEXT,
        external_id TEXT NOT NULL,
        created_at TEXT,
        text TEXT,
        url TEXT,
        raw_path TEXT,
        metrics_json TEXT,
        saved_at TEXT,
        mode TEXT
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS posts_source_external_id_uidx
        ON posts (source, external_id)
    """,
    "CREATE INDEX IF NOT EXISTS posts_source_idx ON posts (source)",
    "CREATE INDEX IF NOT EXISTS posts_author_idx ON posts (author)",
    "CREATE INDEX IF NOT EXISTS posts_created_at_idx ON posts (created_at)",
)

POST_COLUMNS = (
    "id",
    "source",
    "author",
    "author_id",
    "external_id",
    "created_at",
    "text",
    "url",
    "raw_path",
    "metrics_json",
    "saved_at",
    "mode",
)

UPSERT_SQL = """
INSERT INTO posts (
    id, source, author, author_id, external_id, created_at, text,
    url, raw_path, metrics_json, saved_at, mode
) VALUES (
    %(id)s, %(source)s, %(author)s, %(author_id)s, %(external_id)s,
    %(created_at)s, %(text)s, %(url)s, %(raw_path)s, %(metrics_json)s,
    %(saved_at)s, %(mode)s
)
ON CONFLICT (id) DO UPDATE SET
    source = EXCLUDED.source,
    author = EXCLUDED.author,
    author_id = EXCLUDED.author_id,
    external_id = EXCLUDED.external_id,
    created_at = EXCLUDED.created_at,
    text = EXCLUDED.text,
    url = EXCLUDED.url,
    raw_path = EXCLUDED.raw_path,
    metrics_json = EXCLUDED.metrics_json,
    saved_at = EXCLUDED.saved_at,
    mode = EXCLUDED.mode
"""


class UpsertPost(BaseModel):
    id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    author: str
    author_id: str | None = None
    external_id: str = Field(min_length=1)
    created_at: str
    text: str
    url: str | None = None
    raw_path: str | None = None
    metrics_json: str | None = None
    saved_at: str | None = None
    mode: str | None = None


def _require_env() -> tuple[str, str]:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    token = os.environ.get("KOL_API_TOKEN", "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    if not token:
        raise RuntimeError("KOL_API_TOKEN is required")
    return database_url, token


async def _init_schema(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        async with conn.transaction():
            for statement in SCHEMA_STATEMENTS:
                await conn.execute(statement)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    database_url, _ = _require_env()
    async with AsyncConnectionPool(
        conninfo=database_url,
        min_size=1,
        max_size=10,
        kwargs={"row_factory": dict_row, "autocommit": False},
        open=False,
    ) as pool:
        await pool.open(wait=True)
        await _init_schema(pool)
        app.state.pool = pool
        yield


app = FastAPI(
    title="KOL Posts API",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail="Unauthorized",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def require_bearer(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    expected = os.environ.get("KOL_API_TOKEN", "").strip()
    if not expected:
        raise _unauthorized()
    if not authorization or not authorization.startswith("Bearer "):
        raise _unauthorized()
    provided = authorization[len("Bearer ") :].strip()
    if not provided or not secrets.compare_digest(provided, expected):
        raise _unauthorized()


def _pool(request: Request) -> AsyncConnectionPool:
    return request.app.state.pool


def _ilike_pattern(query: str) -> str:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.post("/posts/upsert", dependencies=[Depends(require_bearer)])
async def upsert_post(body: UpsertPost, request: Request) -> dict[str, Any]:
    payload = body.model_dump()
    try:
        async with _pool(request).connection() as conn:
            async with conn.transaction():
                await conn.execute(UPSERT_SQL, payload)
    except UniqueViolation as exc:
        raise HTTPException(
            status_code=409,
            detail="duplicate source+external_id",
        ) from exc
    return {"ok": True, "id": body.id}


@app.get("/posts", dependencies=[Depends(require_bearer)])
async def list_posts(
    request: Request,
    source: Annotated[str | None, Query()] = None,
    author: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
    since: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1)] = 50,
) -> dict[str, list[dict[str, Any]]]:
    limit = min(limit, 500)
    clauses: list[str] = []
    params: dict[str, Any] = {"limit": limit}

    if source:
        clauses.append("source = %(source)s")
        params["source"] = source
    if author:
        clauses.append("author = %(author)s")
        params["author"] = author
    if q:
        clauses.append("text ILIKE %(q)s ESCAPE '\\'")
        params["q"] = _ilike_pattern(q)
    if since:
        clauses.append("created_at >= %(since)s")
        params["since"] = since

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT {', '.join(POST_COLUMNS)}
        FROM posts
        {where}
        ORDER BY created_at DESC NULLS LAST, id ASC
        LIMIT %(limit)s
    """
    async with _pool(request).connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            rows = await cur.fetchall()
    return {"posts": list(rows)}


@app.get("/stats", dependencies=[Depends(require_bearer)])
async def stats(request: Request) -> dict[str, Any]:
    async with _pool(request).connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT source, COUNT(*)::int AS n FROM posts GROUP BY source"
            )
            count_rows = await cur.fetchall()
            await cur.execute("SELECT COUNT(*)::int AS total FROM posts")
            total_row = await cur.fetchone()
    counts = {row["source"]: row["n"] for row in count_rows}
    total = total_row["total"] if total_row else 0
    return {"counts": counts, "total": total}


@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=exc.headers,
    )
