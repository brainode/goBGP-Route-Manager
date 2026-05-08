# Architecture

## Scope

`goBGP Route Manager` is a server-rendered FastAPI application that stores desired routing state in SQLite and applies that state to a goBGP daemon over gRPC, with CLI fallback.

The project has five main layers:

- Presentation: Jinja templates in `app/templates/*` and static assets in `app/static/`
- HTTP routing: FastAPI endpoints in `app/routers/`
- Application orchestration: business logic in `app/services/`
- Integration: `GoBGPClient` and the discovery pipeline in `app/gobgp_client.py` and `app/discovery.py`
- Persistence: SQLAlchemy engine/session factory and ORM models in `app/database.py` and `app/models.py`

## UML Class Diagram

```plantuml
@startuml
skinparam classAttributeIconSize 0
skinparam packageStyle rectangle

package "app.routers" {
  class SitesRouter {
    +list_sites()
    +create_site()
    +toggle_site()
    +rediscover_site()
    +delete_site()
    +site_detail()
    +add_prefix()
    +delete_prefix()
    +update_site_tags()
    +bulk_change_next_hop()
    +bulk_add_tags()
    +bulk_set_tags()
  }

  class NextHopsRouter {
    +list_next_hops()
    +create_next_hop()
    +delete_next_hop()
  }

  class SettingsRouter {
    +settings_page()
    +set_discovery_mode()
    +export_configuration()
    +import_configuration()
    +purge_inactive()
    +apply_current_state()
    +rediscover_all()
  }
}

package "app.services" {
  class SiteService {
    +sync_site(db, site)
    +sync_site_by_id(site_id)
    +change_site_next_hop(db, site, new_next_hop_id)
    +bulk_change_next_hop(site_ids, next_hop_id)
    +attach_runtime_status(sites, ipv6_enabled)
  }

  class RouteService {
    +apply_prefix(db, site, prefix, announce)
    +build_optimized_route_plan(sites, ipv6_enabled)
    +normalize_cidr(cidr)
  }

  class RediscoverService {
    +rediscover_site_state(db, site, debug)
    +submit_rediscover_site_job(site_id, job_id)
  }

  class SettingsService {
    +serialize_configuration(db)
    +import_configuration(db, payload)
  }
}

package "app.discovery" {
  class DiscoveryPipeline {
    +discover_domain(domain, debug, mode)
    -_resolve_ips(domain)
    -_ip_to_asn(ip, debug)
    -_asn_prefixes(asn, debug)
    -_ip_to_prefix_ripestat(ip, debug)
    -_ip_to_prefix_rdap(ip, debug)
    -_optimize_prefixes(prefixes)
  }
}

package "app.gobgp_client" {
  class GoBGPClient {
    -enabled: bool
    -binary: str
    -host: str
    -port: str
    -use_grpc: bool
    -grpc_timeout: float
    -grpc_fallback_cli: bool
    +add_route(cidr, next_hop)
    +del_route(cidr, next_hop)
    +del_route_any(cidr)
    +list_routes()
    +purge_routes()
    +status()
    -_get_grpc_stub()
    -_grpc_add_del(op, cidr, next_hop)
    -_build_unicast_path(cidr, next_hop)
    -_run(cmd, op)
    -_run_once(cmd)
  }
}

package "app.database" {
  class DatabaseFactory {
    +engine
    +SessionLocal()
    +get_db()
  }
}

package "app.models" {
  class Site {
    +id: int
    +domain: str
    +asn: str
    +enabled: bool
    +next_hop_id: int
    +tags: str | None
    +created_at: datetime
    +updated_at: datetime
  }

  class NextHop {
    +id: int
    +ip: str
    +name: str
    +created_at: datetime
  }

  class Prefix {
    +id: int
    +site_id: int
    +cidr: str
    +source: str
    +is_active: bool
    +created_at: datetime
  }

  class Setting {
    +key: str
    +value: str
  }
}

SitesRouter --> DatabaseFactory : Depends(get_db)
SitesRouter --> SiteService : sync / bulk ops
SitesRouter --> RouteService : apply / build plan
SitesRouter --> DiscoveryPipeline : discover / rediscover
NextHopsRouter --> DatabaseFactory : Depends(get_db)
SettingsRouter --> DatabaseFactory : Depends(get_db)
SettingsRouter --> SettingsService : serialize / import
SiteService --> GoBGPClient : add/del/status/list/purge
RouteService --> GoBGPClient : add/del

Site "1" --> "1" NextHop : next_hop
Site "1" --> "*" Prefix : prefixes

DatabaseFactory ..> Site
DatabaseFactory ..> NextHop
DatabaseFactory ..> Prefix
DatabaseFactory ..> Setting
SitesRouter ..> Site
SitesRouter ..> NextHop
SitesRouter ..> Prefix
SitesRouter ..> Setting
NextHopsRouter ..> NextHop
SettingsRouter ..> Site
SettingsRouter ..> Prefix
SettingsRouter ..> Setting
@enduml
```

## Component Roles

### `app/main.py`

This is the application bootstrap.

- Creates the FastAPI app instance and mounts routers
- Defines the lifespan context manager (schema creation, background thread startup)
- Provides backward-compat module-level aliases for tests and tooling

### `app/routers/`

These are the HTTP route handler modules.

- `sites.py` — site CRUD, toggle, rediscover, prefix add/delete, **bulk next-hop change**, **bulk tag operations**, **tag editing**
- `next_hops.py` — next-hop CRUD
- `settings.py` — settings page, discovery mode, import/export, purge, apply-current, rediscover-all
- `logs.py` — job log viewer
- `health.py` — health checks

### `app/services/`

These are the business logic modules extracted from the original monolithic `main.py`.

- `site_service.py` — site sync, runtime status metadata, **bulk next-hop change** with withdraw/announce
- `route_service.py` — route apply/withdraw, CIDR normalization, optimized route plan builder
- `rediscover_service.py` — background rediscover jobs, prefix reconciliation
- `settings_service.py` — settings get/set, configuration import/export
- `status_service.py` — periodic goBGP RIB sync, background refresh thread
- `job_service.py` — job creation, active-job checks, LoggingList

### `app/gobgp_client.py`

This is the integration boundary to goBGP.

- Prefers gRPC for route operations and status checks
- Falls back to the `gobgp` CLI when configured or when gRPC fails
- Normalizes route add/delete/list/purge/status into one object API
- Hides transport details from the rest of the application

### `app/discovery.py`

This is the domain-to-routes pipeline.

- Resolves a domain to IPv4 addresses
- Maps IPs to ASNs using a provider fallback chain
- Maps ASNs or IPs to prefixes depending on selected discovery mode
- Collapses and normalizes prefixes before returning them to the caller

### `app/database.py`

This is the persistence bootstrap.

- Creates the SQLAlchemy engine
- Exposes `SessionLocal`
- Enables SQLite foreign keys on connect
- Supplies `get_db()` for FastAPI dependency injection

### `app/models.py`

This is the domain model.

- `Site` is the aggregate root for routable domain/group state
- `NextHop` is a reusable lookup entity
- `Prefix` is a child entity of `Site`
- `Setting` stores low-volume runtime configuration such as discovery mode

## Patterns Used

### 1. Gateway / Adapter

Used in `app/gobgp_client.py`.

`GoBGPClient` hides protocol details behind a stable method set:

- `add_route()`
- `del_route()`
- `list_routes()`
- `status()`

The rest of the application does not need to know whether the actual transport is:

- gRPC
- normal `gobgp` CLI
- legacy CLI syntax fallback

This is the most important integration abstraction in the codebase.

### 2. Strategy + Fallback Chain

Used in `app/discovery.py`.

There are two strategy dimensions.

Discovery mode strategy:

- `network_info`
- `rdap`
- `asn_prefixes`

Provider fallback strategy:

- `IPinfo` for `IP -> ASN`
- `IPinfo` for `ASN -> prefixes`
- `RIPEstat` fallback for prefixes
- `BGPView` optional fallback when enabled

The caller only passes `mode`; provider selection and fallback logic stay encapsulated in the pipeline.

### 3. Dependency Injection

Used in `app/routers/`.

FastAPI injects infrastructure dependencies into handlers:

- `db: Session = Depends(get_db)`
- `background_tasks: BackgroundTasks`

This keeps route handlers stateless and avoids global mutable session objects.

### 4. Data Mapper

Used via SQLAlchemy ORM in `app/models.py` and `app/database.py`.

The database schema is represented by mapped classes, while persistence operations happen through the session:

- `db.add(...)`
- `db.commit()`
- `db.refresh(...)`
- `db.query(...)`

The entities themselves do not contain SQL.

### 5. Background Job Dispatch

Used in `app/routers/`.

Longer route synchronization work is not executed inline with the HTTP request. Instead the application schedules:

- `background_tasks.add_task(_sync_site_by_id, site.id)`

This is not a durable queue, but it is still a clear asynchronous boundary that improves UI responsiveness.

### 6. Server-Side Template View

Used in `app/templates/*`.

The application renders HTML on the server with Jinja templates rather than using a SPA frontend. This keeps the UI simple and closely coupled to the backend workflow.

## Data Flow

### 1. Create Site With Auto Discovery

1. Browser submits `POST /sites`
2. `create_site()` validates the selected `next_hop_id`
3. A `Site` row is inserted first so the object exists even if discovery later returns no prefixes
4. If `discover=on`, `discover_domain()` resolves the domain, selects a discovery mode, finds prefixes and returns a normalized prefix list
5. The handler stores `site.asn`
6. Returned prefixes are inserted into `Prefix` rows with `source="discovery"`
7. If the site is enabled, `site_service.sync_site_by_id()` is scheduled in a background task
8. The background job reads the current site state from SQLite and applies each active prefix to goBGP via `GoBGPClient`
9. Optional `tags` are stored as a comma-separated string on the `Site` row

### 2. Create Site Without Discovery

1. Browser submits `POST /sites` with discovery disabled
2. The handler inserts the `Site`
3. No prefixes are created automatically
4. If enabled, sync still runs, but there is nothing to announce until prefixes are added manually

### 3. Manual Prefix Add

1. Browser submits `POST /sites/{site_id}/prefixes`
2. `add_prefix()` validates CIDR syntax with `ip_network(..., strict=False)`
3. A `Prefix` row is inserted with `source="manual"`
4. If the parent site is enabled, `route_service.apply_prefix()` calls `GoBGPClient.add_route()`

### 4. Site Toggle

1. Browser submits `POST /sites/{site_id}/toggle`
2. `toggle_site()` flips `site.enabled`
3. Background sync is scheduled
4. `site_service.sync_site()` iterates active prefixes
5. Each prefix is announced or withdrawn depending on the new `enabled` value

### 5. Rediscover Site

1. Browser submits `POST /sites/{site_id}/rediscover`
2. The handler runs `discover_domain()` again using the configured discovery mode
3. Existing discovery-origin prefixes are compared with the newly discovered prefix set
4. Removed prefixes are withdrawn and deleted
5. New prefixes are inserted and announced if the site is enabled
6. Manual prefixes are preserved because only discovery-owned prefixes are reconciled

### 6. Bulk Change Next Hop

1. Browser selects sites via checkboxes on `/sites` and chooses a new next hop in the bulk action bar
2. `POST /sites/bulk-change-next-hop` receives `site_ids` and `next_hop_id`
3. A background task calls `site_service.bulk_change_next_hop(site_ids, next_hop_id)`
4. For each site, `change_site_next_hop()`:
   - withdraws active prefixes using the **old** next-hop IP
   - updates `site.next_hop_id` in SQLite
   - re-announces prefixes using the **new** next-hop IP
5. Tags, manual prefixes, and other site attributes are preserved

### 7. Tag Management

1. Tags are stored as a comma-separated string on `Site.tags` (e.g. `"video,streaming"`)
2. `POST /sites` accepts `tags` at creation time
3. `POST /sites/{site_id}/tags` updates tags for a single site
4. `POST /sites/bulk-add-tags` merges new tags into existing ones (deduplication)
5. `POST /sites/bulk-set-tags` replaces tags entirely
6. Tags are exported and imported with the full configuration JSON

### 8. Status Page

1. Browser requests `GET /gobgp-status`
2. `GoBGPClient.status()` checks:
   - local `gobgp` binary availability
   - goBGP daemon reachability
   - effective ability to apply routes
3. The result is rendered as a server-side HTML status page

## Current Architectural Tradeoffs

- Background tasks are in-process only. If the process dies, queued sync work is lost.
- Schema creation happens on startup via `Base.metadata.create_all(...)`; there is no migration layer yet.
- Business logic has been extracted from route handlers into `app/services/`; route handlers now delegate to services.
- Discovery and route apply are synchronous inside worker functions; there is no rate limiting or durable retry queue.
- There is no authentication/authorization layer yet, so deployment should assume trusted networks only.

## Extension Points

- Replace FastAPI `BackgroundTasks` with a real job queue for durable sync
- Add Alembic migrations for schema evolution beyond runtime `ALTER TABLE` additions
- Add audit trail / route operation history table
- Add auth before any public or semi-public deployment
- Consider a normalized `Tag` many-to-many table if tag-based querying becomes complex
