<!-- SPDX-License-Identifier: GPL-2.0-only -->
# goBGP Route Manager

[![License: GPL-2.0-only](https://img.shields.io/badge/license-GPL--2.0--only-blue.svg)](COPYING)
![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)
![Docker Required](https://img.shields.io/badge/docker-required-2496ED?logo=docker&logoColor=white)
![AI Assisted](https://img.shields.io/badge/AI-assisted-ff6f00)

Web UI + API for managing goBGP routes by domain/site, with SQLite state and Docker-based deployment.

## Why This Project

Managing routes via ad-hoc shell scripts does not scale well. This project provides:

- Centralized route state in SQLite
- Web UI for add/delete/toggle routes
- `next-hop` catalog and reuse
- Domain discovery pipeline (`domain -> ASN -> prefixes`)
- Background sync to avoid blocking UI

## Features

- Site inventory table: `domain`, `ASN`, `prefix count`, `next-hop`, `enabled`, **`tags`**
- Prefix management per site
- Next-hop management
- **Bulk operations**: change next-hop for selected sites, mass add/set tags
- **Tag-based filtering** on the sites list
- goBGP status page (gRPC/CLI connectivity)
- Discovery modes in Settings
- One-click maintenance actions (purge inactive records)
- Background route apply/withdraw jobs
- Full configuration export/import (JSON, includes tags)
- Multi-arch container builds for `amd64` and `arm64`

## Architecture

```text
Browser
  -> FastAPI + Jinja UI (route-manager)
     -> SQLite (/data/route_manager.db)
     -> goBGP (gRPC:50051, optional CLI fallback)
     -> Discovery providers (IPinfo / RIPEstat / BGPView)
```

## Project Structure

```text
app/
  main.py                 # App bootstrap, router mounting, lifespan
  routers/                # FastAPI route handlers (sites, next_hops, settings, logs, health)
  services/               # Business logic (site_service, route_service, rediscover_service, settings_service, status_service, job_service)
  gobgp_client.py         # goBGP integration (gRPC/CLI)
  discovery.py            # domain -> ASN -> prefixes pipeline
  models.py               # SQLAlchemy models
  templates/              # Foundation-based HTML templates
  static/                 # CSS assets
  gobgp_api/              # Third-party goBGP protobuf API files (MIT)

gobgp/
  gobgpd.toml             # goBGP daemon config

docker-compose.yml        # dev/prod profiles
Dockerfile                # route-manager image
Dockerfile.gobgp          # goBGP image
```

## Docs

- `ARCHITECTURE.md` - code structure, patterns, UML, and data flow
- `MIKROTIK_DEPLOYMENT.md` - full split-deployment runbook for `VPS + WireGuard + MikroTik`
- `DOCKER_DESKTOP_TO_MIKROTIK_CHEATSHEET.md` - short operational cheat sheet for `Windows Docker Desktop -> MikroTik hAP ax3`

## Requirements

- Docker 24+
- Docker Compose v2+
- Linux host/network capabilities for BGP peering

## Build Notes

- Both Dockerfiles support `amd64` and `arm64`.
- `GOBGP_VERSION` is pinned in `Dockerfile` and `Dockerfile.gobgp`; override it explicitly if you want a different official release.

Example:

```bash
docker build --build-arg GOBGP_VERSION=4.3.0 -t gobgp-route-manager:latest -f Dockerfile .
docker build --build-arg GOBGP_VERSION=4.3.0 -t gobgp-daemon:latest -f Dockerfile.gobgp .
```

```powershell
docker build --build-arg GOBGP_VERSION=4.3.0 -t gobgp-route-manager:latest -f Dockerfile .
docker build --build-arg GOBGP_VERSION=4.3.0 -t gobgp-daemon:latest -f Dockerfile.gobgp .
```

## Quick Start (Dev Profile)

```bash
cp .env.example .env
mkdir -p data

docker compose --profile dev up --build -d --remove-orphans
```

```powershell
Copy-Item .env.example .env
mkdir data -ErrorAction SilentlyContinue

docker compose --profile dev up --build -d --remove-orphans
```

Open: `http://localhost:8000`

## Deployment Profiles

| Profile | Services | Network Mode | UI | goBGP Host |
|---|---|---|---|---|
| `dev` | `gobgp-dev`, `route-manager-dev` | bridge | `localhost:8000` | `gobgp` |
| `prod` | `gobgp-prod`, `route-manager-prod` | host | host:8000 | `127.0.0.1` |

For security, `gobgp-prod` binds gRPC to `127.0.0.1:50051` by default. To expose it intentionally on a WireGuard IP instead, set `GOBGPD_API_HOSTS=<vps_wg_ip>` before starting the prod profile.

Switch profile:

```bash
docker compose --profile dev down
docker compose --profile prod up --build -d --remove-orphans
```

```powershell
docker compose --profile dev down
docker compose --profile prod up --build -d --remove-orphans
```

## Deployment Recipes

### 1) Existing goBGP already installed on VPS

Use Route Manager only, and point it to the existing goBGP daemon.

```bash
cp .env.example .env
mkdir -p data
```

```powershell
Copy-Item .env.example .env
mkdir data -ErrorAction SilentlyContinue
```

Edit `.env`:

- `GOBGP_ENABLED=true`
- `GOBGP_HOST=127.0.0.1` (or the real goBGP host)
- `GOBGP_PORT=50051`
- `GOBGPD_API_HOSTS=127.0.0.1`

Run Route Manager container only:

```bash
docker build -t gobgp-route-manager:latest -f Dockerfile .
docker run -d --name route-manager \
  --restart unless-stopped \
  --network host \
  --env-file .env \
  -v "$(pwd)/data:/data" \
  gobgp-route-manager:latest
```

```powershell
docker build -t gobgp-route-manager:latest -f Dockerfile .
docker run -d --name route-manager `
  --restart unless-stopped `
  --network host `
  --env-file .env `
  -v ${PWD}\\data:/data `
  gobgp-route-manager:latest
```

### 2) Empty VPS (deploy everything from this project)

This starts both services from the repository.

```bash
cp .env.example .env
mkdir -p data
```

```powershell
Copy-Item .env.example .env
mkdir data -ErrorAction SilentlyContinue
```

Edit `gobgp/gobgpd.toml` with your ASN/router-id/neighbors, then:

```bash
docker compose --profile prod up --build -d --remove-orphans
```

```powershell
docker compose --profile prod up --build -d --remove-orphans
```

### 3) goBGP on VPS, Route Manager in internal network (VPS <-> router over WireGuard)

Recommended split:

- VPS: run `gobgpd` from this repo in host network.
- Internal host: run Route Manager container, connect to goBGP over WireGuard IP.
- For `MikroTik hAP ax3` specifically, use `MIKROTIK_DEPLOYMENT.md` for the full runbook and `DOCKER_DESKTOP_TO_MIKROTIK_CHEATSHEET.md` for the short operator flow.

On VPS:

```bash
export GOBGPD_API_HOSTS=<wireguard_ip_of_vps>
docker build -t gobgp-daemon:latest -f Dockerfile.gobgp .
docker run -d --name gobgp \
  --restart unless-stopped \
  --network host \
  -v "$(pwd)/gobgp/gobgpd.toml:/etc/gobgp/gobgpd.toml:ro" \
  gobgp-daemon:latest \
  -f /etc/gobgp/gobgpd.toml --api-hosts ${GOBGPD_API_HOSTS}:50051
```

```powershell
$env:GOBGPD_API_HOSTS="<wireguard_ip_of_vps>"
docker build -t gobgp-daemon:latest -f Dockerfile.gobgp .
docker run -d --name gobgp `
  --restart unless-stopped `
  --network host `
  -v ${PWD}\\gobgp\\gobgpd.toml:/etc/gobgp/gobgpd.toml:ro `
  gobgp-daemon:latest `
  -f /etc/gobgp/gobgpd.toml --api-hosts ${env:GOBGPD_API_HOSTS}:50051
```

On internal host (where UI/API lives), edit `.env`:

- `GOBGP_ENABLED=true`
- `GOBGP_HOST=<wireguard_ip_of_vps>` (example: `10.100.0.1`)
- `GOBGP_PORT=50051`

Then run Route Manager:

```bash
docker build -t gobgp-route-manager:latest -f Dockerfile .
docker run -d --name route-manager \
  --restart unless-stopped \
  -p 8000:8000 \
  --env-file .env \
  -v "$(pwd)/data:/data" \
  gobgp-route-manager:latest
```

```powershell
docker build -t gobgp-route-manager:latest -f Dockerfile .
docker run -d --name route-manager `
  --restart unless-stopped `
  -p 8000:8000 `
  --env-file .env `
  -v ${PWD}\\data:/data `
  gobgp-route-manager:latest
```

## Configuration

Copy `.env.example` to `.env` and set values.

### Core

| Variable | Default | Description |
|---|---|---|
| `APP_HOST` | `0.0.0.0` | API/UI bind host |
| `APP_PORT` | `8000` | API/UI bind port |
| `DB_HOST_DIR` | `./data` | Host dir mapped to `/data` |
| `DATABASE_URL` | `sqlite:////data/route_manager.db` | SQLAlchemy DSN |

### goBGP

| Variable | Default | Description |
|---|---|---|
| `GOBGP_ENABLED` | `false` | Dry-run when `false` |
| `GOBGP_HOST` | `gobgp` | goBGP gRPC host |
| `GOBGP_PORT` | `50051` | goBGP gRPC port |
| `GOBGP_USE_GRPC` | `true` | Prefer gRPC control path |
| `GOBGP_GRPC_FALLBACK_CLI` | `true` | Use CLI if gRPC fails |
| `ROUTE_APPLY_WORKERS` | `8` | Parallel route apply workers |

### Discovery

| Variable | Default | Description |
|---|---|---|
| `IPINFO_TOKEN` | empty | Optional IPinfo token |
| `DISCOVERY_MAX_IPS` | `12` | Limit resolved A-records used after DNS sampling |
| `DISCOVERY_DNS_ATTEMPTS` | `4` | Number of DNS resolution passes per domain |
| `DISCOVERY_DNS_DELAY_MS` | `250` | Delay between DNS resolution passes |
| `DISCOVERY_IP_LOOKUP_TIMEOUT` | `2` | IP -> ASN timeout |
| `DISCOVERY_PREFIX_LOOKUP_TIMEOUT` | `6` | ASN -> prefixes timeout |
| `DISCOVERY_HTTP_RETRIES` | `2` | HTTP retries |
| `DISCOVERY_RIPESTAT_TIMEOUT` | `10` | RIPEstat timeout |
| `DISCOVERY_ENABLE_BGPVIEW` | `false` | Enable BGPView fallback |

## Runtime Behavior

- Route operations run in background tasks.
- Site `enabled=true` means prefixes should be announced.
- Site `enabled=false` means prefixes are withdrawn.
- The app stores target state in SQLite and pushes actions to goBGP.

## API (Current)

### Sites
- `GET /sites`
- `POST /sites`
- `POST /sites/{site_id}/toggle`
- `POST /sites/{site_id}/rediscover`
- `POST /sites/{site_id}/delete`
- `POST /sites/{site_id}/prefixes`
- `POST /sites/{site_id}/tags`
- `POST /sites/bulk-change-next-hop`
- `POST /sites/bulk-add-tags`
- `POST /sites/bulk-set-tags`
- `GET /sites/{site_id}`

### Prefixes
- `POST /prefixes/{prefix_id}/delete`

### Next Hops
- `GET /next-hops`
- `POST /next-hops`
- `POST /next-hops/{next_hop_id}/delete`

### Settings
- `GET /settings`
- `POST /settings/discovery-mode`
- `POST /settings/apply-current`
- `POST /settings/rediscover-all`
- `GET /settings/export`
- `POST /settings/import`

### Other
- `GET /health`
- `GET /api/sites`

## Security Notes

- Keep `.env` private (already ignored).
- Avoid committing logs/database snapshots.
- Discovery logging sanitizes token-like data before writing debug lines.
- If any secret was exposed previously, rotate it.

## Troubleshooting

### UI is up, routes are not applied

- Check `GOBGP_ENABLED=true`
- Check `GOBGP_HOST`, `GOBGP_PORT`
- Open `/gobgp-status`

### Discovery returns empty prefixes

- Try another discovery mode in Settings
- Increase discovery timeouts in `.env`
- Verify outbound internet access from container

### Discovery misses large CDN domains

- Increase `DISCOVERY_DNS_ATTEMPTS` and `DISCOVERY_MAX_IPS`
- Re-run site rediscovery after DNS sampling changes
- For domains like `youtube.com`, DNS-based discovery is still heuristic and may need manual prefix additions

### Database path errors

- Ensure `${DB_HOST_DIR}` exists on host
- Verify Docker bind mount permissions

## Roadmap

- IPv6 discovery and policy controls

## Contributing

1. Fork repository
2. Create feature branch
3. Add focused changes with tests where applicable
4. Open pull request with rationale and rollout notes

## License

- Project code: `GPL-2.0-only` (see `COPYING`)
- Third-party goBGP API files under `app/gobgp_api/*`: `MIT` (see `THIRD_PARTY_NOTICES.md`)
