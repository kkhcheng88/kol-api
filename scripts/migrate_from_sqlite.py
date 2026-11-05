#!/usr/bin/env python3
"""Copy posts from a local SQLite DB into the shared KOL API or directly into Postgres.

Examples:
  python scripts/migrate_from_sqlite.py --sqlite /path/to/kol.sqlite \\
      --api-url https://kol.casy.hk --token "$KOL_API_TOKEN"

  python scripts/migrate_from_sqlite.py --sqlite /path/to/kol.sqlite \\
      --database-url "$DATABASE_URL"
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from typing import Any

COLUMNS = (
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


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _row_to_payload(row: sqlite3.Row, available: set[str]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for column in COLUMNS:
        payload[column] = _as_str(row[column]) if column in available else None
    return payload


def _read_sqlite(path: str) -> list[dict[str, Any]]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        info = conn.execute("PRAGMA table_info(posts)").fetchall()
        if not info:
            raise SystemExit(f"no posts table in {path}")
        available = {str(col["name"]) for col in info}
        missing = {"id", "source", "external_id"} - available
        if missing:
            raise SystemExit(f"sqlite posts table missing columns: {sorted(missing)}")
        rows = conn.execute("SELECT * FROM posts").fetchall()
    finally:
        conn.close()
    return [_row_to_payload(row, available) for row in rows]


def _upsert_via_api(api_url: str, token: str, payload: dict[str, Any]) -> None:
    req = urllib.request.Request(
        api_url.rstrip("/") + "/posts/upsert",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            if resp.status not in (200, 201):
                body = resp.read().decode("utf-8", errors="replace")
                raise SystemExit(f"upsert failed for {payload.get('id')}: {resp.status} {body}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(
            f"upsert failed for {payload.get('id')}: {exc.code} {body}"
        ) from exc


def _upsert_via_postgres(database_url: str, rows: list[dict[str, Any]]) -> None:
    try:
        import psycopg
    except ImportError as exc:
        raise SystemExit(
            "psycopg is required for --database-url (pip install -r requirements.txt)"
        ) from exc

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            for payload in rows:
                cur.execute(UPSERT_SQL, payload)
        conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", required=True, help="Path to local kol.sqlite")
    parser.add_argument(
        "--api-url",
        default=os.environ.get("KOL_API_URL", ""),
        help="API base URL (default: $KOL_API_URL)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("KOL_API_TOKEN", ""),
        help="Bearer token (default: $KOL_API_TOKEN)",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", ""),
        help="Write directly to Postgres (default: $DATABASE_URL)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    rows = _read_sqlite(args.sqlite)
    print(f"read {len(rows)} posts from {args.sqlite}", file=sys.stderr)
    if args.dry_run:
        return

    if args.database_url:
        _upsert_via_postgres(args.database_url, rows)
        print(f"upserted {len(rows)} posts via DATABASE_URL", file=sys.stderr)
        return

    if not args.api_url or not args.token:
        raise SystemExit("provide --api-url and --token, or --database-url")

    for i, payload in enumerate(rows, start=1):
        if not payload.get("id") or not payload.get("source") or not payload.get("external_id"):
            print(f"skip incomplete row {i}", file=sys.stderr)
            continue
        if payload.get("author") is None:
            payload["author"] = ""
        if payload.get("created_at") is None:
            payload["created_at"] = ""
        if payload.get("text") is None:
            payload["text"] = ""
        _upsert_via_api(args.api_url, args.token, payload)
        if i % 100 == 0:
            print(f"upserted {i}/{len(rows)}", file=sys.stderr)
    print(f"upserted {len(rows)} posts via {args.api_url}", file=sys.stderr)


if __name__ == "__main__":
    main()
