import importlib
import json
from pathlib import Path
from threading import Lock
import time

from fastapi.testclient import TestClient
from concurrent.futures import Future


def load_app(tmp_path, monkeypatch):
    db_path = Path(tmp_path) / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("STATUS_REFRESH_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("STATUS_STALE_AFTER_SECONDS", "3600")
    monkeypatch.setenv("GOBGP_ENABLED", "false")

    import app.database
    import app.models
    import app.config
    import app.state
    import app.services.job_service
    import app.services.settings_service
    import app.services.route_service
    import app.services.site_service
    import app.services.rediscover_service
    import app.services.status_service
    import app.routers.health
    import app.routers.sites
    import app.routers.next_hops
    import app.routers.settings
    import app.routers.logs
    import app.main

    importlib.reload(app.database)
    importlib.reload(app.models)
    importlib.reload(app.config)
    importlib.reload(app.state)
    importlib.reload(app.services.job_service)
    importlib.reload(app.services.settings_service)
    importlib.reload(app.services.route_service)
    importlib.reload(app.services.site_service)
    importlib.reload(app.services.rediscover_service)
    importlib.reload(app.services.status_service)
    importlib.reload(app.routers.health)
    importlib.reload(app.routers.sites)
    importlib.reload(app.routers.next_hops)
    importlib.reload(app.routers.settings)
    importlib.reload(app.routers.logs)
    importlib.reload(app.main)
    return app.main, app.models


def test_build_optimized_route_plan_dedups_by_next_hop(tmp_path, monkeypatch):
    main, models = load_app(tmp_path, monkeypatch)

    hop_a = models.NextHop(id=1, ip="10.10.10.1")
    hop_b = models.NextHop(id=2, ip="10.10.10.2")

    site_a = models.Site(domain="a.example", enabled=True, next_hop=hop_a, next_hop_id=1, is_manual=False)
    site_a.prefixes = [
        models.Prefix(cidr="10.0.0.0/24", is_active=True),
        models.Prefix(cidr="10.0.1.0/24", is_active=True),
    ]

    site_b = models.Site(domain="b.example", enabled=True, next_hop=hop_a, next_hop_id=1, is_manual=False)
    site_b.prefixes = [
        models.Prefix(cidr="10.0.0.0/24", is_active=True),
    ]

    site_c = models.Site(domain="c.example", enabled=True, next_hop=hop_b, next_hop_id=2, is_manual=False)
    site_c.prefixes = [
        models.Prefix(cidr="10.0.0.0/24", is_active=True),
    ]

    plan = main.build_optimized_route_plan([site_a, site_b, site_c], ipv6_enabled=True)

    assert plan["raw_prefix_rows"] == 4
    assert plan["optimized_unique_routes"] == 2
    assert ("10.0.0.0/23", "10.10.10.1") in plan["routes"]
    assert ("10.0.0.0/24", "10.10.10.2") in plan["routes"]


def test_site_status_metadata_reports_partial_and_paused(tmp_path, monkeypatch):
    main, models = load_app(tmp_path, monkeypatch)

    hop = models.NextHop(id=1, ip="10.10.10.1")
    site = models.Site(domain="status.example", enabled=True, next_hop=hop, next_hop_id=1, is_manual=False)
    site.prefixes = [
        models.Prefix(cidr="10.0.0.0/24", is_active=True, is_announced=True),
        models.Prefix(cidr="10.0.1.0/24", is_active=True, is_announced=False),
    ]

    metadata = main._site_status_metadata(site, ipv6_enabled=True)
    assert metadata["status"] == "partial"
    assert metadata["desired_prefixes_count"] == 2
    assert metadata["announced_prefixes_count"] == 1

    site.enabled = False
    paused = main._site_status_metadata(site, ipv6_enabled=True)
    assert paused["status"] == "paused"


def test_import_configuration_upserts_and_preserves_existing_prefixes(tmp_path, monkeypatch):
    main, models = load_app(tmp_path, monkeypatch)
    monkeypatch.setattr(main.gobgp, "list_routes", lambda: (True, [], "ok"))

    with TestClient(main.app) as client:
        payload = {
            "version": 1,
            "settings": {
                "discovery_mode": "smart",
                "ipv6_enabled": True,
                "auto_rediscover_all_enabled": True,
            },
            "next_hops": [
                {"ip": "10.10.10.1", "name": "Primary"},
            ],
            "sites": [
                {
                    "domain": "import.example",
                    "asn": "AS64500",
                    "enabled": True,
                    "site_type": "discovery",
                    "auto_rediscover_enabled": True,
                    "next_hop_ip": "10.10.10.1",
                    "prefixes": [
                        {"cidr": "10.0.0.0/24", "source": "manual", "is_active": True},
                    ],
                }
            ],
        }

        response = client.post(
            "/settings/import",
            files={"config_file": ("config.json", json.dumps(payload), "application/json")},
        )
        assert response.status_code == 200

        db = main.SessionLocal()
        try:
            site = db.query(models.Site).filter(models.Site.domain == "import.example").first()
            assert site is not None
            assert site.auto_rediscover_enabled is True
            assert site.is_manual is False
            assert len(site.prefixes) == 1
        finally:
            db.close()

        payload["sites"][0]["asn"] = "AS64501"
        payload["sites"][0]["prefixes"] = [
            {"cidr": "10.0.1.0/24", "source": "discovery", "is_active": True},
        ]
        response = client.post(
            "/settings/import",
            files={"config_file": ("config.json", json.dumps(payload), "application/json")},
        )
        assert response.status_code == 200

        db = main.SessionLocal()
        try:
            from sqlalchemy.orm import joinedload
            site = db.query(models.Site).options(joinedload(models.Site.prefixes)).filter(models.Site.domain == "import.example").first()
            assert site.asn == "AS64501"
            assert sorted(prefix.cidr for prefix in site.prefixes) == ["10.0.0.0/24", "10.0.1.0/24"]
        finally:
            db.close()


def test_export_configuration_returns_expected_shape(tmp_path, monkeypatch):
    main, models = load_app(tmp_path, monkeypatch)
    monkeypatch.setattr(main.gobgp, "list_routes", lambda: (True, [], "ok"))

    with TestClient(main.app) as client:
        db = main.SessionLocal()
        try:
            hop = models.NextHop(ip="10.10.10.1", name="Primary")
            db.add(hop)
            db.commit()
            db.refresh(hop)

            site = models.Site(
                domain="export.example",
                asn="AS64500",
                enabled=True,
                is_manual=False,
                auto_rediscover_enabled=True,
                next_hop_id=hop.id,
            )
            db.add(site)
            db.commit()
            db.refresh(site)
            db.add(models.Prefix(site_id=site.id, cidr="10.0.0.0/24", source="discovery", is_active=True, is_announced=True))
            db.add(models.Job(job_type="rediscover_site", site_id=site.id, status="done"))
            db.commit()
            main._set_setting_value(db, "discovery_mode", "smart")
            main._set_setting_value(db, "ipv6_enabled", "true")
            main._set_setting_value(db, "auto_rediscover_all_enabled", "true")
            db.commit()
        finally:
            db.close()

        response = client.get("/settings/export")
        assert response.status_code == 200
        data = response.json()
        assert data["version"] == 1
        assert data["settings"]["auto_rediscover_all_enabled"] is True
        assert data["sites"][0]["site_type"] == "discovery"
        assert data["sites"][0]["auto_rediscover_enabled"] is True
        assert "jobs" not in data
        assert "job_logs" not in data
        assert "is_announced" not in json.dumps(data)
        assert "last_checked_at" not in json.dumps(data)


def test_auto_rediscover_toggle_syncs_global_setting(tmp_path, monkeypatch):
    main, models = load_app(tmp_path, monkeypatch)
    monkeypatch.setattr(main.gobgp, "list_routes", lambda: (True, [], "ok"))

    with TestClient(main.app) as client:
        db = main.SessionLocal()
        try:
            hop = models.NextHop(ip="10.10.10.1")
            db.add(hop)
            db.commit()
            db.refresh(hop)

            discovery_a = models.Site(domain="a.example", next_hop_id=hop.id, enabled=True, is_manual=False, auto_rediscover_enabled=False)
            discovery_b = models.Site(domain="b.example", next_hop_id=hop.id, enabled=True, is_manual=False, auto_rediscover_enabled=False)
            manual_site = models.Site(domain="manual-routes", next_hop_id=hop.id, enabled=True, is_manual=True, auto_rediscover_enabled=False)
            db.add_all([discovery_a, discovery_b, manual_site])
            db.commit()
            db.refresh(discovery_a)
        finally:
            db.close()

        response = client.post("/settings/auto-rediscover-all", data={"enabled": "on"})
        assert response.status_code == 200

        db = main.SessionLocal()
        try:
            sites = {site.domain: site for site in db.query(models.Site).all()}
            assert sites["a.example"].auto_rediscover_enabled is True
            assert sites["b.example"].auto_rediscover_enabled is True
            assert sites["manual-routes"].auto_rediscover_enabled is False
            assert main._get_auto_rediscover_all_enabled(db) is True
        finally:
            db.close()

        response = client.post(
            f"/sites/{sites['a.example'].id}/auto-rediscover",
            data={},
            headers={"referer": "/sites"},
        )
        assert response.status_code == 200

        db = main.SessionLocal()
        try:
            assert main._get_auto_rediscover_all_enabled(db) is False
        finally:
            db.close()


def test_auto_rediscover_cycle_processes_only_enabled_discovery_sites(tmp_path, monkeypatch):
    main, models = load_app(tmp_path, monkeypatch)

    import app.services.status_service as _svc_status
    import app.services.settings_service as _svc_settings
    import app.services.rediscover_service as _svc_rediscover

    with TestClient(main.app):
        db = main.SessionLocal()
        try:
            hop = models.NextHop(ip="10.10.10.1")
            db.add(hop)
            db.commit()
            db.refresh(hop)

            site_run = models.Site(domain="run.example", next_hop_id=hop.id, enabled=True, is_manual=False, auto_rediscover_enabled=True)
            site_skip_job = models.Site(domain="skip-job.example", next_hop_id=hop.id, enabled=True, is_manual=False, auto_rediscover_enabled=True)
            site_manual = models.Site(domain="manual.example", next_hop_id=hop.id, enabled=True, is_manual=True, auto_rediscover_enabled=False)
            site_disabled = models.Site(domain="off.example", next_hop_id=hop.id, enabled=True, is_manual=False, auto_rediscover_enabled=False)
            db.add_all([site_run, site_skip_job, site_manual, site_disabled])
            db.commit()
            db.refresh(site_run)
            db.refresh(site_skip_job)
            db.add(models.Job(job_type="rediscover_site", site_id=site_skip_job.id, status="running"))
            db.commit()
            run_id = site_run.id
            skip_job_id = site_skip_job.id
        finally:
            db.close()

        processed = []
        monkeypatch.setattr(_svc_status, "refresh_gobgp_state", lambda trigger: None)
        monkeypatch.setattr(_svc_settings, "set_maintenance_status", lambda message: None)

        def fake_rediscover(db, site, apply_changes=True, debug=None, cancel_event=None):
            processed.append(site.id)
            return {"ok": True, "site_id": site.id, "added": 0, "removed": 0, "discovered": 0, "asn": site.asn}

        monkeypatch.setattr(_svc_rediscover, "rediscover_site_state", fake_rediscover)

        _svc_status.run_auto_rediscover_cycle("test")

        assert processed == [run_id]

        db = main.SessionLocal()
        try:
            done_job = db.query(models.Job).filter(models.Job.site_id == run_id, models.Job.job_type == "auto_rediscover_site").first()
            assert done_job is not None
            assert done_job.status == "done"

            original_running = db.query(models.Job).filter(models.Job.site_id == skip_job_id, models.Job.status == "running").count()
            assert original_running == 1
        finally:
            db.close()


def test_rediscover_site_endpoint_queues_job_and_logs_it(tmp_path, monkeypatch):
    main, models = load_app(tmp_path, monkeypatch)
    monkeypatch.setattr(main.gobgp, "list_routes", lambda: (True, [], "ok"))

    import app.services.rediscover_service as _svc_rediscover

    def fake_submit(site_id, job_id):
        future = Future()
        future.set_result(None)
        return future

    monkeypatch.setattr(_svc_rediscover, "submit_rediscover_site_job", fake_submit)

    with TestClient(main.app) as client:
        db = main.SessionLocal()
        try:
            hop = models.NextHop(ip="10.10.10.1")
            db.add(hop)
            db.commit()
            db.refresh(hop)

            site = models.Site(domain="queue.example", next_hop_id=hop.id, enabled=True, is_manual=False)
            db.add(site)
            db.commit()
            db.refresh(site)
            site_id = site.id
        finally:
            db.close()

        response = client.post(f"/sites/{site_id}/rediscover")
        assert response.status_code == 200
        job_id = response.json()["job_id"]

        db = main.SessionLocal()
        try:
            job = db.query(models.Job).filter(models.Job.id == job_id).first()
            assert job is not None
            assert job.status == "pending"
            logs = db.query(models.JobLog).filter(models.JobLog.job_id == job_id).order_by(models.JobLog.id.asc()).all()
            assert any("[queued] rediscover scheduled source=manual" in log.message for log in logs)
        finally:
            db.close()


def test_rediscover_site_state_reapplies_active_prefixes(tmp_path, monkeypatch):
    main, models = load_app(tmp_path, monkeypatch)

    import app.services.rediscover_service as _svc_rediscover

    add_calls = []
    del_calls = []

    monkeypatch.setattr(main.gobgp, "add_route", lambda cidr, next_hop: (add_calls.append((cidr, next_hop)) or True, "ok"))
    monkeypatch.setattr(main.gobgp, "del_route", lambda cidr, next_hop: (del_calls.append((cidr, next_hop)) or True, "ok"))
    monkeypatch.setattr(
        _svc_rediscover,
        "discover_domain",
        lambda domain, debug=None, mode=None: ("AS64500", ["203.0.113.10"], ["10.0.0.0/24", "10.0.1.0/24"]),
    )

    db = main.SessionLocal()
    try:
        hop = models.NextHop(ip="10.10.10.1")
        db.add(hop)
        db.commit()
        db.refresh(hop)

        site = models.Site(domain="reapply.example", next_hop_id=hop.id, enabled=True, is_manual=False)
        db.add(site)
        db.commit()
        db.refresh(site)
        db.add(models.Prefix(site_id=site.id, cidr="10.0.0.0/24", source="discovery", is_active=True))
        db.commit()

        debug = []
        result = _svc_rediscover.rediscover_site_state(db, site, apply_changes=True, debug=debug)
    finally:
        db.close()

    assert result["ok"] is True
    assert result["removed"] == 0
    assert result["added"] == 1
    assert result["sync_attempted"] == 2
    assert result["sync_succeeded"] == 2
    assert result["sync_failed"] == 0
    assert del_calls == []
    assert add_calls.count(("10.0.0.0/24", "10.10.10.1")) == 1
    assert add_calls.count(("10.0.1.0/24", "10.10.10.1")) == 2
    assert any(line.startswith("[bgp reapply] syncing active prefixes") for line in debug)
    assert any("attempted=2 succeeded=2 failed=0" in line for line in debug)


def test_rediscover_site_state_skips_bgp_changes_when_site_disabled(tmp_path, monkeypatch):
    main, models = load_app(tmp_path, monkeypatch)

    import app.services.rediscover_service as _svc_rediscover

    add_calls = []
    del_calls = []

    monkeypatch.setattr(main.gobgp, "add_route", lambda cidr, next_hop: (add_calls.append((cidr, next_hop)) or True, "ok"))
    monkeypatch.setattr(main.gobgp, "del_route", lambda cidr, next_hop: (del_calls.append((cidr, next_hop)) or True, "ok"))
    monkeypatch.setattr(
        _svc_rediscover,
        "discover_domain",
        lambda domain, debug=None, mode=None: ("AS64500", ["203.0.113.10"], ["10.0.0.0/24", "10.0.1.0/24"]),
    )

    db = main.SessionLocal()
    try:
        hop = models.NextHop(ip="10.10.10.1")
        db.add(hop)
        db.commit()
        db.refresh(hop)

        site = models.Site(domain="disabled.example", next_hop_id=hop.id, enabled=False, is_manual=False)
        db.add(site)
        db.commit()
        db.refresh(site)
        db.add(models.Prefix(site_id=site.id, cidr="10.0.0.0/24", source="discovery", is_active=True))
        db.commit()

        debug = []
        result = _svc_rediscover.rediscover_site_state(db, site, apply_changes=True, debug=debug)
    finally:
        db.close()

    assert result["ok"] is True
    assert result["removed"] == 0
    assert result["added"] == 1
    assert result["sync_attempted"] == 0
    assert result["sync_succeeded"] == 0
    assert result["sync_failed"] == 0
    assert add_calls == []
    assert del_calls == []
    assert any(line.startswith("[done] added=1 removed=0 total_prefixes=2") for line in debug)


def test_rediscover_all_queues_sites_with_parallel_limit(tmp_path, monkeypatch):
    main, models = load_app(tmp_path, monkeypatch)
    monkeypatch.setattr(main.gobgp, "list_routes", lambda: (True, [], "ok"))

    import app.services.status_service as _svc_status
    import app.services.settings_service as _svc_settings
    import app.services.route_service as _svc_route
    import app.services.rediscover_service as _svc_rediscover

    monkeypatch.setattr(_svc_status, "refresh_gobgp_state", lambda trigger: None)
    monkeypatch.setattr(_svc_settings, "set_maintenance_status", lambda message: None)

    def fake_apply_current_state(db, debug=None):
        return {
            "ok": True,
            "routes_found": 0,
            "routes_removed": 0,
            "sites": 0,
            "raw_prefix_rows": 0,
            "optimized_unique_routes": 0,
            "prefixes_attempted": 0,
            "prefixes_applied": 0,
            "prefixes_failed": 0,
            "errors": [],
        }

    monkeypatch.setattr(_svc_route, "apply_current_state", fake_apply_current_state)

    active = 0
    max_active = 0
    counter_lock = Lock()

    def fake_rediscover(db, site, apply_changes=True, debug=None, cancel_event=None):
        nonlocal active, max_active
        with counter_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.3)
        with counter_lock:
            active -= 1
        return {"ok": True, "site_id": site.id, "added": 0, "removed": 0, "discovered": 0, "asn": site.asn}

    monkeypatch.setattr(_svc_rediscover, "rediscover_site_state", fake_rediscover)

    with TestClient(main.app):
        db = main.SessionLocal()
        try:
            hop = models.NextHop(ip="10.10.10.1")
            db.add(hop)
            db.commit()
            db.refresh(hop)

            sites = []
            for idx in range(6):
                site = models.Site(
                    domain=f"site-{idx}.example",
                    next_hop_id=hop.id,
                    enabled=True,
                    is_manual=False,
                )
                db.add(site)
                db.commit()
                db.refresh(site)
                sites.append(site.id)
        finally:
            db.close()

        _svc_status.run_rediscover_all_and_apply_job("test")

        db = main.SessionLocal()
        try:
            jobs = (
                db.query(models.Job)
                .filter(models.Job.site_id.in_(sites), models.Job.job_type == "rediscover_site")
                .order_by(models.Job.id.asc())
                .all()
            )
            assert len(jobs) == 6
            assert all(job.status == "done" for job in jobs)
            assert max_active == 4
        finally:
            db.close()
