# SPDX-License-Identifier: GPL-2.0-only
from collections import Counter
from ipaddress import ip_address, ip_network, collapse_addresses, summarize_address_range
import os
import re
import socket
from typing import Optional

import requests
from requests import RequestException

DISCOVERY_MODES: list[tuple[str, str]] = [
    ("network_info", "RIPE Stat network-info (exact BGP prefix)"),
    ("rdap", "RDAP netblock (registry allocation)"),
    ("asn_prefixes", "ASN all prefixes (not recommended)"),
]
DISCOVERY_MODE_DEFAULT = "network_info"

_RDAP_ENDPOINTS = [
    "https://rdap.arin.net/registry/ip/{ip}",
    "https://rdap.db.ripe.net/ip/{ip}",
    "https://rdap.apnic.net/ip/{ip}",
    "https://rdap.lacnic.net/rdap/ip/{ip}",
    "https://rdap.afrinic.net/rdap/ip/{ip}",
]

def _normalize_domain(domain: str) -> str:
    domain = domain.strip().lower()
    if domain.startswith("http://"):
        domain = domain[7:]
    if domain.startswith("https://"):
        domain = domain[8:]
    return domain.strip("/")


def _dbg(debug: Optional[list[str]], message: str) -> None:
    if debug is not None:
        debug.append(message)


def _redact_sensitive(value: str) -> str:
    value = re.sub(r"([?&]token=)[^&\\s]+", r"\1***", value, flags=re.IGNORECASE)
    value = re.sub(r"(Authorization\\s*:\\s*Bearer\\s+)[^\\s]+", r"\1***", value, flags=re.IGNORECASE)
    return value


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _resolve_ips(domain: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(domain, None)
        ips = sorted({entry[4][0] for entry in infos})
        return [ip for ip in ips if ":" not in ip]
    except Exception:
        return []


def _extract_asn(value: object) -> Optional[str]:
    if isinstance(value, str):
        match = re.search(r"AS(\d+)", value.upper())
        if match:
            return f"AS{match.group(1)}"
    elif isinstance(value, dict):
        for key in ("asn", "as_number", "number"):
            nested = value.get(key)
            extracted = _extract_asn(nested)
            if extracted:
                return extracted
    return None


def _get_json_with_optional_token(url: str, timeout: int, debug: Optional[list[str]] = None) -> tuple[Optional[dict], str]:
    token = os.getenv("IPINFO_TOKEN", "").strip() or None
    candidates: list[tuple[str, dict[str, str], str]]
    if token:
        candidates = [
            (url, {"Authorization": f"Bearer {token}"}, "bearer"),
            (url, {}, "no_auth"),
        ]
    else:
        candidates = [(url, {}, "no_auth")]

    for candidate_url, headers, mode in candidates:
        safe_url = _redact_sensitive(candidate_url)
        try:
            resp = requests.get(candidate_url, headers=headers, timeout=timeout)
        except RequestException as exc:
            _dbg(debug, f"http {mode} {safe_url} request_error={_redact_sensitive(str(exc))}")
            continue

        if resp.status_code != 200:
            _dbg(debug, f"http {mode} {safe_url} status={resp.status_code}")
            continue

        try:
            data = resp.json()
        except ValueError:
            _dbg(debug, f"http {mode} {safe_url} invalid_json")
            continue

        if isinstance(data, dict):
            _dbg(debug, f"http {mode} {safe_url} status=200")
            return data, mode
    return None, "failed"


def _ip_to_asn_ipinfo(ip: str, debug: Optional[list[str]] = None) -> tuple[Optional[str], str]:
    urls = [f"https://api.ipinfo.io/lite/{ip}", f"https://ipinfo.io/{ip}/json"]
    timeout = _int_env("DISCOVERY_IP_LOOKUP_TIMEOUT", 2)
    for url in urls:
        data, mode = _get_json_with_optional_token(url, timeout=timeout, debug=debug)
        if not data:
            continue

        for key in ("asn", "as_number", "org"):
            asn = _extract_asn(data.get(key))
            if asn:
                _dbg(debug, f"ipinfo ip={ip} asn={asn} field={key} auth={mode}")
                return asn, "ipinfo"
    return None, "ipinfo"


def _asn_prefixes_ipinfo(asn_number: str, debug: Optional[list[str]] = None) -> tuple[list[str], str]:
    timeout = _int_env("DISCOVERY_PREFIX_LOOKUP_TIMEOUT", 6)
    data, mode = _get_json_with_optional_token(f"https://ipinfo.io/{asn_number}/json", timeout=timeout, debug=debug)
    if not data:
        return [], "ipinfo"

    result = []
    prefixes = data.get("prefixes")
    if isinstance(prefixes, list):
        for item in prefixes:
            if isinstance(item, str):
                result.append(item)
                continue
            if isinstance(item, dict):
                prefix = item.get("prefix") or item.get("netblock") or item.get("network")
                if isinstance(prefix, str):
                    result.append(prefix)
    _dbg(debug, f"ipinfo asn={asn_number} prefixes={len(result)} auth={mode}")
    return result, "ipinfo"


def _asn_prefixes_ripestat(asn_number: str, debug: Optional[list[str]] = None) -> tuple[list[str], str]:
    url = f"https://stat.ripe.net/data/announced-prefixes/data.json?resource={asn_number}"
    timeout = _int_env("DISCOVERY_PREFIX_LOOKUP_TIMEOUT", 6)
    try:
        resp = requests.get(url, timeout=timeout)
        if resp.status_code != 200:
            _dbg(debug, f"ripestat asn={asn_number} status={resp.status_code}")
            return [], "ripestat"
        data = resp.json().get("data", {})
    except (RequestException, ValueError):
        _dbg(debug, f"ripestat asn={asn_number} request_failed")
        return [], "ripestat"

    result = []
    for item in data.get("prefixes", []):
        if not isinstance(item, dict):
            continue
        prefix = item.get("prefix")
        if isinstance(prefix, str):
            result.append(prefix)
    _dbg(debug, f"ripestat asn={asn_number} prefixes={len(result)}")
    return result, "ripestat"


def _ip_to_asn_bgpview(ip: str, debug: Optional[list[str]] = None) -> tuple[Optional[str], str]:
    url = f"https://api.bgpview.io/ip/{ip}"
    timeout = _int_env("DISCOVERY_IP_LOOKUP_TIMEOUT", 2)
    try:
        resp = requests.get(url, timeout=timeout)
        if resp.status_code != 200:
            _dbg(debug, f"bgpview ip={ip} status={resp.status_code}")
            return None, "bgpview"
        data = resp.json().get("data", {})
    except (RequestException, ValueError):
        _dbg(debug, f"bgpview ip={ip} request_failed")
        return None, "bgpview"

    prefixes = data.get("prefixes", [])
    if not prefixes:
        return None, "bgpview"

    first = prefixes[0]
    asn_info = first.get("asn") or {}
    asn_value = asn_info.get("asn")
    if asn_value is None:
        return None, "bgpview"
    asn = f"AS{asn_value}"
    _dbg(debug, f"bgpview ip={ip} asn={asn}")
    return asn, "bgpview"


def _asn_prefixes_bgpview(asn_number: str, debug: Optional[list[str]] = None) -> tuple[list[str], str]:
    numeric = asn_number.replace("AS", "")
    url = f"https://api.bgpview.io/asn/{numeric}/prefixes"
    timeout = _int_env("DISCOVERY_PREFIX_LOOKUP_TIMEOUT", 6)
    try:
        resp = requests.get(url, timeout=timeout)
        if resp.status_code != 200:
            _dbg(debug, f"bgpview asn={asn_number} status={resp.status_code}")
            return [], "bgpview"
        data = resp.json().get("data", {})
    except (RequestException, ValueError):
        _dbg(debug, f"bgpview asn={asn_number} request_failed")
        return [], "bgpview"

    result = []
    for item in data.get("ipv4_prefixes", []):
        prefix = item.get("prefix")
        if prefix:
            result.append(prefix)
    _dbg(debug, f"bgpview asn={asn_number} prefixes={len(result)}")
    return result, "bgpview"


def _ip_to_asn(ip: str, debug: Optional[list[str]] = None) -> tuple[Optional[str], str]:
    asn, source = _ip_to_asn_ipinfo(ip, debug=debug)
    if asn:
        return asn, source
    if not _bool_env("DISCOVERY_ENABLE_BGPVIEW", False):
        _dbg(debug, "bgpview fallback disabled for ip_to_asn")
        return None, "none"
    return _ip_to_asn_bgpview(ip, debug=debug)


def _asn_prefixes(asn_number: str, debug: Optional[list[str]] = None) -> tuple[list[str], str]:
    prefixes, source = _asn_prefixes_ipinfo(asn_number, debug=debug)
    if prefixes:
        return prefixes, source
    prefixes, source = _asn_prefixes_ripestat(asn_number, debug=debug)
    if prefixes:
        return prefixes, source
    if not _bool_env("DISCOVERY_ENABLE_BGPVIEW", False):
        _dbg(debug, "bgpview fallback disabled for asn_to_prefixes")
        return [], "none"
    return _asn_prefixes_bgpview(asn_number, debug=debug)


def _http_get_json(url: str, timeout: int, debug: Optional[list[str]] = None,
                   label: str = "http", retries: int = 1) -> Optional[dict]:
    for attempt in range(retries):
        try:
            resp = requests.get(url, timeout=timeout)
            if resp.status_code != 200:
                _dbg(debug, f"{label} url={url} status={resp.status_code} attempt={attempt}")
                continue
            data = resp.json()
            if isinstance(data, dict):
                return data
        except (RequestException, ValueError) as exc:
            _dbg(debug, f"{label} url={url} error={exc} attempt={attempt}")
    return None


def _ip_to_prefix_ripestat(ip: str, debug: Optional[list[str]] = None) -> tuple[Optional[str], Optional[str]]:
    url = f"https://stat.ripe.net/data/network-info/data.json?resource={ip}"
    timeout = _int_env("DISCOVERY_NETWORK_INFO_TIMEOUT", _int_env("DISCOVERY_IP_LOOKUP_TIMEOUT", 5))
    data = _http_get_json(url, timeout=timeout, debug=debug, label="ripestat network-info", retries=2)
    if not data:
        return None, None
    payload = data.get("data") or data
    prefix = payload.get("prefix") if isinstance(payload, dict) else None
    asns_raw = payload.get("asns") if isinstance(payload, dict) else None
    asn: Optional[str] = None
    if isinstance(asns_raw, list) and asns_raw:
        asn = f"AS{asns_raw[0]}"
    elif isinstance(asns_raw, (str, int)):
        asn = f"AS{asns_raw}"
    if prefix:
        try:
            ip_network(prefix, strict=False)
            _dbg(debug, f"ripestat network-info ip={ip} prefix={prefix} asn={asn}")
            return prefix, asn
        except ValueError:
            pass
    _dbg(debug, f"ripestat network-info ip={ip} no_prefix asn={asn}")
    return None, asn


def _ip_to_prefix_rdap(ip: str, debug: Optional[list[str]] = None) -> tuple[Optional[str], Optional[str]]:
    timeout = _int_env("DISCOVERY_RDAP_TIMEOUT", _int_env("DISCOVERY_IP_LOOKUP_TIMEOUT", 5))
    for template in _RDAP_ENDPOINTS:
        url = template.format(ip=ip)
        data = _http_get_json(url, timeout=timeout, debug=debug, label="rdap", retries=1)
        if not data:
            continue
        prefix: Optional[str] = None
        # Try cidr0_cidrs first
        cidrs = data.get("cidr0_cidrs")
        if isinstance(cidrs, list) and cidrs:
            best: Optional[ip_network] = None  # type: ignore[type-arg]
            for entry in cidrs:
                v4 = entry.get("v4prefix") or entry.get("v6prefix")
                length = entry.get("length")
                if v4 and length is not None:
                    try:
                        net = ip_network(f"{v4}/{length}", strict=False)
                        if best is None or net.prefixlen > best.prefixlen:
                            best = net
                    except ValueError:
                        pass
            if best:
                prefix = str(best)
        # Fallback: startAddress/endAddress
        if not prefix:
            start = data.get("startAddress")
            end = data.get("endAddress")
            if start and end:
                try:
                    nets = list(summarize_address_range(ip_address(start), ip_address(end)))
                    if nets:
                        prefix = str(min(nets, key=lambda n: n.prefixlen))
                except (TypeError, ValueError):
                    pass
        if prefix:
            asn: Optional[str] = None
            entities = data.get("entities") or []
            for ent in entities:
                roles = ent.get("roles") or []
                if "registration" in roles or "registrant" in roles:
                    handle = ent.get("handle", "")
                    extracted = _extract_asn(handle)
                    if extracted:
                        asn = extracted
                        break
            _dbg(debug, f"rdap ip={ip} registry={url} prefix={prefix} asn={asn}")
            return prefix, asn
        _dbg(debug, f"rdap ip={ip} registry={url} no_prefix")
    return None, None


def _optimize_prefixes(prefixes: list[str]) -> list[str]:
    networks_v4 = []
    networks_v6 = []
    for cidr in prefixes:
        try:
            net = ip_network(cidr, strict=False)
        except Exception:
            continue
        if net.version == 4:
            networks_v4.append(net)
        else:
            networks_v6.append(net)

    collapsed_v4 = [str(net) for net in collapse_addresses(networks_v4)] if networks_v4 else []
    collapsed_v6 = [str(net) for net in collapse_addresses(networks_v6)] if networks_v6 else []
    return collapsed_v4 + collapsed_v6


def discover_domain(
    domain: str,
    debug: Optional[list[str]] = None,
    mode: str = DISCOVERY_MODE_DEFAULT,
) -> tuple[Optional[str], list[str], list[str]]:
    domain = _normalize_domain(domain)
    _dbg(debug, f"discover domain={domain} mode={mode}")
    resolved_ips = _resolve_ips(domain)
    _dbg(debug, f"dns ipv4_count={len(resolved_ips)} sample={resolved_ips[:4]}")
    if not resolved_ips:
        return None, [], []

    max_ips = _int_env("DISCOVERY_MAX_IPS", 10)
    ips = resolved_ips[:max_ips]
    if len(resolved_ips) > len(ips):
        _dbg(debug, f"dns truncated_ips={len(ips)} of {len(resolved_ips)}")

    if mode == "asn_prefixes":
        asns = []
        for ip in ips:
            asn, provider = _ip_to_asn(ip, debug=debug)
            if asn:
                asns.append(asn)
                _dbg(debug, f"ip_to_asn ip={ip} provider={provider} asn={asn}")
            else:
                _dbg(debug, f"ip_to_asn ip={ip} provider={provider} asn=none")

        if not asns:
            _dbg(debug, "ip_to_asn produced empty result")
            return None, ips, []

        primary_asn = Counter(asns).most_common(1)[0][0]
        _dbg(debug, f"primary_asn={primary_asn}")
        prefixes, provider = _asn_prefixes(primary_asn, debug=debug)
        _dbg(debug, f"asn_to_prefixes provider={provider} raw_count={len(prefixes)}")
        optimized = _optimize_prefixes(prefixes)
        _dbg(debug, f"prefix_optimize result_count={len(optimized)}")
        return primary_asn, ips, optimized

    elif mode == "rdap":
        prefixes_seen: set[str] = set()
        prefixes_out: list[str] = []
        primary_asn: Optional[str] = None
        for ip in ips:
            prefix, asn = _ip_to_prefix_rdap(ip, debug=debug)
            if asn and not primary_asn:
                primary_asn = asn
            if prefix and prefix not in prefixes_seen:
                prefixes_seen.add(prefix)
                prefixes_out.append(prefix)
        if not primary_asn:
            # Fallback ASN lookup
            for ip in ips:
                asn, _ = _ip_to_asn(ip, debug=debug)
                if asn:
                    primary_asn = asn
                    break
        _dbg(debug, f"rdap result asn={primary_asn} prefixes={len(prefixes_out)}")
        return primary_asn, ips, prefixes_out

    else:  # network_info (default)
        prefixes_seen_ni: set[str] = set()
        prefixes_out_ni: list[str] = []
        primary_asn_ni: Optional[str] = None
        for ip in ips:
            prefix, asn = _ip_to_prefix_ripestat(ip, debug=debug)
            if asn and not primary_asn_ni:
                primary_asn_ni = asn
            if prefix and prefix not in prefixes_seen_ni:
                prefixes_seen_ni.add(prefix)
                prefixes_out_ni.append(prefix)
        if not primary_asn_ni:
            for ip in ips:
                asn, _ = _ip_to_asn(ip, debug=debug)
                if asn:
                    primary_asn_ni = asn
                    break
        _dbg(debug, f"network_info result asn={primary_asn_ni} prefixes={len(prefixes_out_ni)}")
        return primary_asn_ni, ips, prefixes_out_ni

