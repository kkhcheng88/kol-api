# kol-api

Shared KOL posts store (PostgreSQL + HTTPS API) for Macro / Karst / Stock / Value.

Multiple Grok Bots upsert and query the same posts over a tiny authenticated HTTP API. Do not point bots at a local SQLite file.

## Endpoints

Auth: `Authorization: Bearer $KOL_API_TOKEN` on every route except `/healthz`. Missing or wrong token → `401`.

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/healthz` | `{"ok": true}` — no auth |
| `POST` | `/posts/upsert` | JSON body; upsert by `id` (`ON CONFLICT DO UPDATE`). Returns `{"ok": true, "id": "..."}` |
| `GET` | `/posts?source=&author=&q=&since=&limit=` | `q` is `text ILIKE`; `since` is `created_at >=`; default `limit` 50, max 500. Returns `{"posts": [...]}` |
| `GET` | `/stats` | `{"counts": {"x": n, ...}, "total": n}` |

`UNIQUE(source, external_id)`. A second `id` with the same pair returns `409`.

### Upsert body

```json
{
  "id": "x:123",
  "source": "x",
  "author": "some_kol",
  "author_id": "44196397",
  "external_id": "123",
  "created_at": "2026-01-01T00:00:00Z",
  "text": "post body",
  "url": "https://x.com/i/status/123",
  "raw_path": null,
  "metrics_json": "{\"likes\":1}",
  "saved_at": "2026-01-01T00:01:00Z",
  "mode": "backfill"
}
```

`author_id`, `url`, `raw_path`, `metrics_json`, `saved_at`, and `mode` are optional. `source` is `x` | `discord` | `zsxq`.

## Local run

Requires Python 3.12 and PostgreSQL.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export DATABASE_URL="postgresql://kol:kol@127.0.0.1:5432/kol"
export KOL_API_TOKEN="change-me"
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

On startup the app creates `posts` plus indexes if they do not exist.

## Coolify on Hostinger VPS

Suggested public hostname: **`kol.casy.hk`**. Point the DNS A/AAAA record at the VPS, then attach that domain to this Coolify resource with HTTPS (Let's Encrypt).

1. In Coolify, create a **PostgreSQL** database resource on the same server / Docker network as the API. Leave it **unpublished**. Do not open `5432` on the VPS firewall or Hostinger panel. Bots never connect to Postgres; only this API does.
2. Create a **Dockerfile** application from this repo. Build context `.`, Dockerfile `Dockerfile`, listen port **8080**.
3. Join the API to the same internal Docker network as the database. Set:

   | Variable | Required | Notes |
   | --- | --- | --- |
   | `DATABASE_URL` | yes | Coolify-internal DSN, e.g. `postgresql://USER:PASS@SERVICE:5432/DB`. Host must be the private DB hostname, not a public IP. |
   | `KOL_API_TOKEN` | yes | Long random secret (`openssl rand -hex 32`). Same value in every bot. |
   | `PORT` | no | Defaults to `8080`. |

4. Health check path: `/healthz` (the image also `HEALTHCHECK`s that URL).
5. Generate HTTPS on `kol.casy.hk`. Do not put a public TCP proxy in front of Postgres.

Bots should call `https://kol.casy.hk` with the bearer token. Example:

```bash
curl -fsS https://kol.casy.hk/healthz
curl -fsS -H "Authorization: Bearer $KOL_API_TOKEN" https://kol.casy.hk/stats
```

## Migrate from local SQLite

```bash
python scripts/migrate_from_sqlite.py \
  --sqlite /path/to/karst-sources/kol/kol.sqlite \
  --api-url https://kol.casy.hk \
  --token "$KOL_API_TOKEN"
```

Or write straight to Postgres (from a machine that can reach it — typically not the public internet):

```bash
python scripts/migrate_from_sqlite.py \
  --sqlite /path/to/karst-sources/kol/kol.sqlite \
  --database-url "$DATABASE_URL"
```

## Schema

Matches the SQLite `posts` table:

| Column | Type |
| --- | --- |
| `id` | `TEXT PRIMARY KEY` |
| `source` | `TEXT NOT NULL` |
| `author` | `TEXT` |
| `author_id` | `TEXT` |
| `external_id` | `TEXT NOT NULL` |
| `created_at` | `TEXT` (ISO 8601) |
| `text` | `TEXT` |
| `url` | `TEXT` |
| `raw_path` | `TEXT` (optional; often null on remote) |
| `metrics_json` | `TEXT` (JSON string or null) |
| `saved_at` | `TEXT` |
| `mode` | `TEXT` (e.g. `backfill`) |

Plus `UNIQUE(source, external_id)` and indexes on `source`, `author`, `created_at`.
