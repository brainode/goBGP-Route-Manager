# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Is

**goBGP Route Manager** — a FastAPI + Jinja web app that manages BGP route announcements by domain/site. It stores desired routing state in SQLite and pushes that state to a [goBGP](https://github.com/osrg/gobgp) daemon over gRPC (with `gobgp` CLI fallback).

## Running in Development

```bash
cp .env.example .env
mkdir -p data
docker compose --profile dev up --build -d --remove-orphans
```

UI available at `http://localhost:8000`. The `dev` profile runs both `gobgp-dev` and `route-manager-dev` containers over a bridge network.

Switch to prod (host networking):
```bash
docker compose --profile dev down
docker compose --profile prod up --build -d --remove-orphans
```

## Running Tests

```bash
pip install -r requirements-dev.txt
pytest tests/
```

Run a single test:
```bash
pytest tests/test_app.py::test_build_optimized_route_plan_dedups_by_next_hop
```

Tests use `monkeypatch` + `importlib.reload` to spin up a fresh app with a temp SQLite DB per test — no mocking of the database layer.

## Building Images

```bash
docker build -t gobgp-route-manager:latest -f Dockerfile .
docker build -t gobgp-daemon:latest -f Dockerfile.gobgp .
```

Override goBGP version:
```bash
docker build --build-arg GOBGP_VERSION=4.3.0 -t gobgp-route-manager:latest -f Dockerfile .
```

Both images support `amd64` and `arm64`.

## Code Architecture

```
app/main.py          # FastAPI app, all route handlers, background sync logic
app/gobgp_client.py  # GoBGPClient: gRPC-first, CLI fallback, stable add/del/list/status API
app/discovery.py     # Domain → DNS → IP → ASN → prefix pipeline; multi-provider fallback
app/models.py        # SQLAlchemy ORM: Site, NextHop, Prefix, Setting, Job, JobLog
app/database.py      # Engine, SessionLocal, get_db() for FastAPI DI; enables SQLite FK pragma
app/templates/       # Jinja2 server-rendered HTML (Foundation CSS)
app/gobgp_api/       # Third-party goBGP protobuf generated files (MIT licensed)
gobgp/gobgpd.toml    # goBGP daemon config (ASN, router-id, peers)
```

`app/routes.py` is a leftover Flask prototype — it is **not** used by the running application.

### Key Architectural Patterns

**`GoBGPClient` (gateway/adapter)** — all route operations go through this object. It hides whether the actual transport is gRPC, `gobgp` CLI, or legacy CLI syntax. The rest of the app never touches gRPC directly.

**Discovery pipeline** — `discover_domain(domain, mode)` runs a multi-step chain: DNS → IP → ASN via IPinfo → prefixes via IPinfo/RIPEstat/BGPView (depending on `mode`). Three modes: `network_info`, `rdap`, `asn_prefixes`. Mode is stored in the `settings` table.

**Site is the aggregate root** — `Prefix` rows are children of `Site` and are cascade-deleted with it. `NextHop` is a shared lookup entity referenced by many sites.

**Background sync via FastAPI `BackgroundTasks`** — route apply/withdraw runs in background threads via `_sync_site_by_id(site_id)`. Not durable — if the process dies, pending sync is lost.

**`Job` / `JobLog` tables** — longer operations (rediscover, rediscover-all, apply-current) are tracked as `Job` rows. `LoggingList` in `main.py` writes each log entry to `job_logs` immediately on `append()`.

**Schema creation on startup** — `Base.metadata.create_all(engine)` runs at import time. There is no Alembic migration layer.

### Data Flow: Site Enable/Disable Toggle

1. `POST /sites/{site_id}/toggle` flips `site.enabled` in SQLite
2. Schedules `_sync_site_by_id(site.id)` as a background task
3. `_sync_site()` iterates all active prefixes and calls `GoBGPClient.add_route()` or `del_route()` for each

### Discovery Source Tracking

`Prefix.source` is either `"manual"` or `"discovery"`. On rediscovery, only `source="discovery"` prefixes are reconciled — manual prefixes are preserved.

## Environment Variables

Copy `.env.example` to `.env`. Critical ones:

| Variable | Default | Notes |
|---|---|---|
| `GOBGP_ENABLED` | `false` | Set `true` to actually push routes |
| `GOBGP_HOST` | `gobgp` | DNS name or IP of the goBGP daemon |
| `GOBGP_USE_GRPC` | `true` | `false` in dev/prod compose overrides |
| `IPINFO_TOKEN` | empty | Optional — needed for higher rate limits |
| `DB_HOST_DIR` | `./data` | Must exist before starting containers |

## Deployment Profiles

| Profile | Network | goBGP host seen by app |
|---|---|---|
| `dev` | bridge | `gobgp` (docker DNS) |
| `prod` | host | `127.0.0.1` |

For split deployment (goBGP on VPS, Route Manager elsewhere over WireGuard), see `MIKROTIK_DEPLOYMENT.md` and `DOCKER_DESKTOP_TO_MIKROTIK_CHEATSHEET.md`.
