# SPDX-License-Identifier: GPL-2.0-only
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from ipaddress import ip_network
import logging
from threading import Lock

try:
    import grpc
    from .gobgp_api.generated.api import attribute_pb2, common_pb2, gobgp_pb2, gobgp_pb2_grpc, nlri_pb2
except Exception as grpc_import_exc:  # pragma: no cover
    grpc = None
    attribute_pb2 = None
    common_pb2 = None
    gobgp_pb2 = None
    gobgp_pb2_grpc = None
    nlri_pb2 = None
    _GRPC_IMPORT_ERROR = grpc_import_exc
else:
    _GRPC_IMPORT_ERROR = None

logger = logging.getLogger("uvicorn.error")


class GoBGPClient:
    def __init__(self) -> None:
        self.enabled = os.getenv("GOBGP_ENABLED", "false").lower() == "true"
        self.binary = os.getenv("GOBGP_BIN", "gobgp")
        self.timeout = int(os.getenv("GOBGP_TIMEOUT", "10"))
        self.host = os.getenv("GOBGP_HOST", "").strip()
        self.port = os.getenv("GOBGP_PORT", "").strip()
        self.use_grpc = self._bool_env("GOBGP_USE_GRPC", True)
        self.grpc_timeout = float(os.getenv("GOBGP_GRPC_TIMEOUT", str(self.timeout)) or str(self.timeout))
        self.grpc_fallback_cli = self._bool_env("GOBGP_GRPC_FALLBACK_CLI", True)

        self._grpc_lock = Lock()
        self._grpc_channel = None
        self._grpc_stub = None
        self._grpc_target = None

        # auto | global | legacy
        self._cmd_mode = "auto"
        self._mode_lock = Lock()

    @staticmethod
    def _bool_env(name: str, default: bool) -> bool:
        raw = os.getenv(name, "").strip().lower()
        if not raw:
            return default
        return raw in {"1", "true", "yes", "on"}

    def _base_cmd(self) -> list[str]:
        cmd = [self.binary]
        if self.host:
            cmd.extend(["-u", self.host])
        if self.port:
            cmd.extend(["-p", self.port])
        return cmd

    def _grpc_endpoint(self) -> str:
        host = self.host or "127.0.0.1"
        port = self.port or "50051"
        return f"{host}:{port}"

    def _get_grpc_stub(self):
        if not self.use_grpc:
            return None, "grpc disabled by config"
        if grpc is None or gobgp_pb2 is None or gobgp_pb2_grpc is None:
            return None, f"grpc dependencies unavailable: {_GRPC_IMPORT_ERROR}"

        target = self._grpc_endpoint()
        with self._grpc_lock:
            if self._grpc_stub is not None and self._grpc_target == target:
                return self._grpc_stub, ""

            channel = grpc.insecure_channel(target)
            try:
                grpc.channel_ready_future(channel).result(timeout=max(self.grpc_timeout, 1.0))
            except Exception as exc:
                return None, f"grpc connect failed target={target} error={exc}"

            self._grpc_channel = channel
            self._grpc_stub = gobgp_pb2_grpc.GoBgpServiceStub(channel)
            self._grpc_target = target
            return self._grpc_stub, ""

    def _build_unicast_path(self, cidr: str, next_hop: str):
        net = ip_network(cidr, strict=False)
        origin_attr = attribute_pb2.Attribute(
            origin=attribute_pb2.OriginAttribute(origin=0),  # IGP
        )
        if net.version == 4:
            family = common_pb2.Family(afi=common_pb2.Family.AFI_IP, safi=common_pb2.Family.SAFI_UNICAST)
            nlri = nlri_pb2.NLRI(
                prefix=nlri_pb2.IPAddressPrefix(
                    prefix_len=net.prefixlen,
                    prefix=str(net.network_address),
                )
            )
            attrs = [
                origin_attr,
                attribute_pb2.Attribute(
                    next_hop=attribute_pb2.NextHopAttribute(next_hop=next_hop),
                )
            ]
        else:
            family = common_pb2.Family(afi=common_pb2.Family.AFI_IP6, safi=common_pb2.Family.SAFI_UNICAST)
            nlri = nlri_pb2.NLRI(
                prefix=nlri_pb2.IPAddressPrefix(
                    prefix_len=net.prefixlen,
                    prefix=str(net.network_address),
                )
            )
            attrs = [
                origin_attr,
                attribute_pb2.Attribute(
                    mp_reach=attribute_pb2.MpReachNLRIAttribute(
                        family=family,
                        next_hops=[next_hop],
                        nlris=[nlri],
                    )
                )
            ]
        path = gobgp_pb2.Path(nlri=nlri, pattrs=attrs, family=family)
        return path, family

    def _grpc_add_del(self, op: str, cidr: str, next_hop: str) -> tuple[bool, str]:
        stub, err = self._get_grpc_stub()
        if not stub:
            return False, err

        try:
            path, family = self._build_unicast_path(cidr, next_hop)
            table_global = gobgp_pb2.TableType.Value("TABLE_TYPE_GLOBAL")
            if op == "add":
                stub.AddPath(
                    gobgp_pb2.AddPathRequest(
                        table_type=table_global,
                        path=path,
                    ),
                    timeout=self.grpc_timeout,
                )
            else:
                stub.DeletePath(
                    gobgp_pb2.DeletePathRequest(
                        table_type=table_global,
                        family=family,
                        path=path,
                    ),
                    timeout=self.grpc_timeout,
                )
            return True, "ok"
        except Exception as exc:
            return False, str(exc)

    @staticmethod
    def _ip_family_arg(cidr: str) -> str:
        net = ip_network(cidr, strict=False)
        return "ipv4" if net.version == 4 else "ipv6"

    def add_route(self, cidr: str, next_hop: str) -> tuple[bool, str]:
        grpc_error = ""
        if self.use_grpc:
            ok, message = self._grpc_add_del("add", cidr, next_hop)
            if ok or not self.grpc_fallback_cli:
                return ok, message
            grpc_error = message
        family = self._ip_family_arg(cidr)
        cmd = self._base_cmd() + ["global", "rib", "-a", family, "add", cidr, "nexthop", next_hop]
        ok, message = self._run(cmd, "add")
        if self.use_grpc and grpc_error:
            if ok:
                logger.warning("gobgp add fallback grpc_failed=%s cidr=%s family=%s", grpc_error, cidr, family)
                return True, f"{message} (transport=cli fallback grpc_error={grpc_error})"
            return False, f"{message} (transport=cli fallback grpc_error={grpc_error})"
        return ok, message

    def del_route(self, cidr: str, next_hop: str) -> tuple[bool, str]:
        grpc_error = ""
        if self.use_grpc:
            ok, message = self._grpc_add_del("del", cidr, next_hop)
            if ok or not self.grpc_fallback_cli:
                return ok, message
            grpc_error = message
        family = self._ip_family_arg(cidr)
        cmd = self._base_cmd() + ["global", "rib", "-a", family, "del", cidr, "nexthop", next_hop]
        ok, message = self._run(cmd, "del")
        if self.use_grpc and grpc_error:
            if ok:
                logger.warning("gobgp del fallback grpc_failed=%s cidr=%s family=%s", grpc_error, cidr, family)
                return True, f"{message} (transport=cli fallback grpc_error={grpc_error})"
            return False, f"{message} (transport=cli fallback grpc_error={grpc_error})"
        return ok, message

    def del_route_any(self, cidr: str) -> tuple[bool, str]:
        grpc_error = ""
        if self.use_grpc:
            try:
                net = ip_network(cidr, strict=False)
                next_hop = "0.0.0.0" if net.version == 4 else "::"
            except Exception:
                next_hop = "0.0.0.0"
            ok, message = self._grpc_add_del("del", cidr, next_hop)
            if ok or not self.grpc_fallback_cli:
                return ok, message
            grpc_error = message
        family = self._ip_family_arg(cidr)
        cmd = self._base_cmd() + ["global", "rib", "-a", family, "del", cidr]
        ok, message = self._run(cmd, "del")
        if self.use_grpc and grpc_error:
            if ok:
                logger.warning("gobgp del-any fallback grpc_failed=%s cidr=%s family=%s", grpc_error, cidr, family)
                return True, f"{message} (transport=cli fallback grpc_error={grpc_error})"
            return False, f"{message} (transport=cli fallback grpc_error={grpc_error})"
        return ok, message

    def _run(self, cmd: list[str], op: str) -> tuple[bool, str]:
        if not self.enabled:
            return True, f"dry-run {op}: {' '.join(cmd)}"

        has_global_rib = "global" in cmd and "rib" in cmd
        run_cmd = cmd
        if has_global_rib:
            with self._mode_lock:
                mode = self._cmd_mode
            if mode == "legacy":
                run_cmd = [part for part in cmd if part != "global"]

        ok, message = self._run_once(run_cmd)
        if ok:
            if has_global_rib:
                with self._mode_lock:
                    if self._cmd_mode == "auto" and run_cmd == cmd:
                        self._cmd_mode = "global"
            return True, message

        # Compatibility fallback for CLI variants.
        if has_global_rib:
            with self._mode_lock:
                mode = self._cmd_mode
            if mode == "legacy":
                return False, message
            legacy = cmd.copy()
            legacy = [part for part in legacy if part != "global"]
            legacy_ok, legacy_message = self._run_once(legacy)
            if legacy_ok:
                with self._mode_lock:
                    self._cmd_mode = "legacy"
                return True, f"{legacy_message} (legacy command mode)"
        return False, message

    def _run_once(self, cmd: list[str]) -> tuple[bool, str]:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout, check=False)
            output = (proc.stdout or "") + (proc.stderr or "")
            if proc.returncode == 0:
                return True, output.strip() or "ok"
            return False, output.strip() or f"command failed with code {proc.returncode}"
        except Exception as exc:
            return False, str(exc)

    def _run_check(self, cmd: list[str], timeout: int) -> tuple[bool, str]:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
            output = ((proc.stdout or "") + (proc.stderr or "")).strip()
            if proc.returncode == 0:
                return True, output or "ok"
            return False, output or f"command failed with code {proc.returncode}"
        except Exception as exc:
            return False, str(exc)

    def _extract_prefixes_from_json(self, payload: object) -> set[str]:
        prefixes: set[str] = set()

        def walk(node: object) -> None:
            if isinstance(node, list):
                for item in node:
                    walk(item)
                return
            if isinstance(node, dict):
                prefix = node.get("prefix")
                if isinstance(prefix, str) and "/" in prefix:
                    prefixes.add(prefix)
                nlri = node.get("nlri")
                if isinstance(nlri, dict):
                    nlri_prefix = nlri.get("prefix")
                    if isinstance(nlri_prefix, str) and "/" in nlri_prefix:
                        prefixes.add(nlri_prefix)
                for value in node.values():
                    walk(value)

        walk(payload)
        return prefixes

    def _extract_prefixes_from_text(self, text: str) -> set[str]:
        pattern = r"(?:\d{1,3}\.){3}\d{1,3}/\d{1,2}|[0-9a-fA-F:]+/\d{1,3}"
        return {item for item in re.findall(pattern, text)}

    def _grpc_list_routes(self) -> tuple[bool, list[str], str]:
        stub, err = self._get_grpc_stub()
        if not stub:
            return False, [], err
        table_global = gobgp_pb2.TableType.Value("TABLE_TYPE_GLOBAL")
        families = [
            common_pb2.Family(afi=common_pb2.Family.AFI_IP, safi=common_pb2.Family.SAFI_UNICAST),
            common_pb2.Family(afi=common_pb2.Family.AFI_IP6, safi=common_pb2.Family.SAFI_UNICAST),
        ]
        routes: set[str] = set()
        try:
            for family in families:
                request = gobgp_pb2.ListPathRequest(table_type=table_global, family=family)
                for response in stub.ListPath(request, timeout=self.grpc_timeout):
                    destination = getattr(response, "destination", None)
                    prefix = getattr(destination, "prefix", "") if destination else ""
                    if prefix:
                        routes.add(prefix)
            return True, sorted(routes), f"rib_grpc routes={len(routes)}"
        except Exception as exc:
            return False, [], str(exc)

    def list_routes(self) -> tuple[bool, list[str], str]:
        if self.use_grpc:
            ok, routes, message = self._grpc_list_routes()
            if ok or not self.grpc_fallback_cli:
                return ok, routes, message

        cmd_json = self._base_cmd() + ["global", "rib", "-j"]
        ok, message = self._run(cmd_json, "list")
        if ok:
            try:
                payload = json.loads(message) if message else []
            except json.JSONDecodeError:
                payload = None
            if payload is not None:
                routes = sorted(self._extract_prefixes_from_json(payload))
                return True, routes, f"rib_json routes={len(routes)}"

        cmd_text = self._base_cmd() + ["global", "rib"]
        ok, message = self._run(cmd_text, "list")
        if ok:
            routes = sorted(self._extract_prefixes_from_text(message))
            return True, routes, f"rib_text routes={len(routes)}"
        return False, [], message

    def purge_routes(self) -> dict:
        if not self.enabled:
            return {
                "ok": True,
                "routes_found": 0,
                "routes_removed": 0,
                "errors": [],
                "message": "dry-run mode, gobgp purge skipped",
            }

        listed_ok, routes, list_message = self.list_routes()
        if not listed_ok:
            return {
                "ok": False,
                "routes_found": 0,
                "routes_removed": 0,
                "errors": [list_message],
                "message": "failed to fetch current RIB",
            }
        errors: list[str] = []
        removed = 0
        for cidr in routes:
            ok, message = self.del_route_any(cidr)
            if ok:
                removed += 1
            else:
                errors.append(f"{cidr}: {message}")

        return {
            "ok": len(errors) == 0,
            "routes_found": len(routes),
            "routes_removed": removed,
            "errors": errors,
            "message": list_message,
        }

    def status(self) -> dict:
        check_timeout = max(self.timeout, 1)
        checked_at = datetime.now(timezone.utc).isoformat()
        endpoint = f"{self.host}:{self.port}" if self.host and self.port else (self.host or "default")

        binary_ok, binary_message = self._run_check([self.binary, "-h"], timeout=check_timeout)

        daemon_ok = False
        daemon_message = "binary check failed"
        if self.use_grpc:
            stub, err = self._get_grpc_stub()
            if not stub:
                daemon_ok = False
                daemon_message = err
            else:
                try:
                    stub.GetBgp(gobgp_pb2.GetBgpRequest(), timeout=self.grpc_timeout)
                    daemon_ok = True
                    daemon_message = "ok"
                except Exception as exc:
                    daemon_ok = False
                    daemon_message = str(exc)
        elif binary_ok:
            daemon_ok, daemon_message = self._run_check(self._base_cmd() + ["neighbor"], timeout=check_timeout)

        mode = "enabled" if self.enabled else "dry-run"
        if self.use_grpc:
            can_apply_routes = self.enabled and daemon_ok
        else:
            can_apply_routes = self.enabled and binary_ok and daemon_ok

        return {
            "checked_at": checked_at,
            "mode": mode,
            "enabled": self.enabled,
            "binary": self.binary,
            "endpoint": endpoint,
            "timeout": self.timeout,
            "binary_ok": binary_ok,
            "binary_message": binary_message,
            "daemon_ok": daemon_ok,
            "daemon_message": daemon_message,
            "can_apply_routes": can_apply_routes,
        }

