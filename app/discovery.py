# SPDX-License-Identifier: GPL-2.0-only
from collections import Counter
from ipaddress import ip_address, ip_network, summarize_address_range
import os
import re
import socket
import time
from typing import Optional
from urllib.parse import urlparse

import requests
from requests import RequestException

DISCOVERY_MODES: list[tuple[str, str]] = [
    ("smart", "Smart: CT logs + HTTP crawl + RIPE + ASN filter (recommended)"),
    ("network_info", "RIPE Stat network-info (exact BGP prefix)"),
    ("rdap", "RDAP netblock (registry allocation)"),
    ("asn_prefixes", "ASN all prefixes (not recommended)"),
]
DISCOVERY_MODE_DEFAULT = "smart"

# Domains to skip when crawling HTML (analytics, ads, fonts — not routing-relevant)
_CRAWL_SKIP_DOMAINS = {
    "www.google.com", "google.com", "google-analytics.com", "analytics.google.com",
    "googletagmanager.com", "doubleclick.net", "googlesyndication.com",
    "fonts.googleapis.com", "fonts.gstatic.com",
    "facebook.com", "facebook.net", "connect.facebook.net",
    "twitter.com", "t.co",
    "amazon-adsystem.com", "ads.yahoo.com",
    "cdn.cookielaw.org", "optanon.blob.core.windows.net",
}

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


def _int_env(name: str, default: int, min_value: int = 1) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= min_value else default


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _resolve_ips(domain: str, debug: Optional[list[str]] = None) -> list[str]:
    attempts = _int_env("DISCOVERY_DNS_ATTEMPTS", 4)
    delay_ms = _int_env("DISCOVERY_DNS_DELAY_MS", 250, min_value=0)
    seen: dict[str, None] = {}

    for attempt in range(1, attempts + 1):
        try:
            infos = socket.getaddrinfo(domain, None)
            attempt_ips = sorted({entry[4][0] for entry in infos if ":" not in entry[4][0]})
            _dbg(debug, f"dns attempt={attempt}/{attempts} ipv4_count={len(attempt_ips)} sample={attempt_ips[:4]}")
            for ip in attempt_ips:
                seen.setdefault(ip, None)
        except Exception as exc:
            _dbg(debug, f"dns attempt={attempt}/{attempts} error={exc}")
        if attempt < attempts and delay_ms:
            time.sleep(delay_ms / 1000)

    ips = list(seen.keys())
    _dbg(debug, f"dns merged_ipv4_count={len(ips)}")
    return ips


def _resolve_ips_doh(domain: str, debug: Optional[list[str]] = None) -> list[str]:
    """Query public DoH resolvers to find anycast IPs invisible to the local system resolver."""
    resolvers = [
        ("google", "https://dns.google/resolve"),
        ("cloudflare", "https://cloudflare-dns.com/dns-query"),
    ]
    timeout = _int_env("DISCOVERY_DNS_TIMEOUT", 5)
    seen: dict[str, None] = {}

    for name, base_url in resolvers:
        try:
            resp = requests.get(
                base_url,
                params={"name": domain, "type": "A"},
                headers={"Accept": "application/dns-json"},
                timeout=timeout,
            )
            if resp.status_code != 200:
                _dbg(debug, f"doh resolver={name} domain={domain} status={resp.status_code}")
                continue
            data = resp.json()
            for record in data.get("Answer") or []:
                if record.get("type") == 1:  # A record
                    ip = record.get("data", "").strip()
                    if ip and ":" not in ip:
                        seen.setdefault(ip, None)
        except (RequestException, ValueError) as exc:
            _dbg(debug, f"doh resolver={name} domain={domain} error={exc}")

    ips = list(seen.keys())
    if ips:
        _dbg(debug, f"doh domain={domain} ips={ips}")
    return ips


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


def _crtsh_subdomains(domain: str, debug: Optional[list[str]] = None) -> list[str]:
    """Query crt.sh CT logs for all known subdomains of *domain*."""
    url = f"https://crt.sh/?q=%.{domain}&output=json"
    timeout = _int_env("DISCOVERY_SMART_CRAWL_TIMEOUT", 10)
    try:
        resp = requests.get(url, timeout=timeout, headers={"Accept": "application/json"})
        if resp.status_code != 200:
            _dbg(debug, f"crtsh domain={domain} status={resp.status_code}")
            return []
        entries = resp.json()
    except (RequestException, ValueError) as exc:
        _dbg(debug, f"crtsh domain={domain} error={exc}")
        return []

    seen: dict[str, None] = {}
    for entry in entries:
        name_value = entry.get("name_value") or entry.get("common_name") or ""
        for name in name_value.splitlines():
            name = name.strip().lstrip("*.")
            if name and "." in name and not name.startswith("*"):
                seen.setdefault(name, None)

    found = list(seen.keys())
    _dbg(debug, f"crtsh domain={domain} subdomains={len(found)}")
    return found


def _http_crawl_domains(domain: str, debug: Optional[list[str]] = None) -> list[str]:
    """Fetch the main page of *domain* and extract unique hostnames from resource URLs."""
    url = f"https://{domain}"
    timeout = _int_env("DISCOVERY_SMART_CRAWL_TIMEOUT", 10)
    try:
        resp = requests.get(
            url, timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (compatible; route-manager/1.0)"},
            allow_redirects=True,
        )
    except RequestException as exc:
        _dbg(debug, f"crawl domain={domain} error={exc}")
        return []

    found: dict[str, None] = {}
    # Regex covers src="...", href="...", action="...", url('...'), url("...")
    pattern = re.compile(r'(?:src|href|action|url)\s*[=:(]\s*["\']?(https?://[^\s"\'<>)]+)', re.IGNORECASE)
    for match in pattern.finditer(resp.text):
        raw_url = match.group(1)
        try:
            host = urlparse(raw_url).netloc.lower()
        except Exception:
            continue
        # Strip port if present
        host = host.split(":")[0]
        if not host or host == domain or host in _CRAWL_SKIP_DOMAINS:
            continue
        # Skip generic TLDs that are clearly not related infrastructure
        if host.endswith(".google.com") and domain not in ("google.com", "youtube.com", "googlevideo.com"):
            continue
        found.setdefault(host, None)

    result = list(found.keys())
    _dbg(debug, f"crawl domain={domain} related_hosts={len(result)} sample={result[:6]}")
    return result


def _asn_prefixes_filtered(
    asn: str,
    known_ips: set[str],
    direct_prefix_nets: list,
    debug: Optional[list[str]] = None,
) -> list[str]:
    """Return ASN prefixes that either contain a resolved IP or are a subnet of a direct prefix.

    Two-pass filter:
    1. IP membership  — catches prefixes that contain one of our resolved IPs directly.
    2. Subnet-of      — catches more-specific /24 CDN prefixes inside a tight aggregate that
                        RIPE returned for our IP (e.g. /24s within a /22 Fastly PoP block).
                        Only applied when the direct RIPE prefix is >= SUBNET_EXPAND_MIN_PREFIXLEN
                        to avoid exploding into hundreds of prefixes for large Google/AWS aggregates
                        like 142.250.0.0/15 or 35.190.0.0/16.
    """
    # Subnet expansion threshold: only expand direct prefixes that are this specific or more.
    # /20 = 4096 addresses. Anything larger (/19, /16, /15 …) is a multi-tenant aggregate
    # and should not trigger sub-prefix enumeration.
    subnet_expand_min = _int_env("DISCOVERY_SUBNET_EXPAND_MIN_PREFIXLEN", 20)

    all_prefixes, source = _asn_prefixes_ripestat(asn, debug=debug)
    if not all_prefixes:
        all_prefixes, source = _asn_prefixes_ipinfo(asn, debug=debug)

    ip_objs = []
    for ip in known_ips:
        try:
            ip_objs.append(ip_address(ip))
        except ValueError:
            pass

    # Only use direct prefixes that are specific enough for subnet expansion
    narrow_direct = [dp for dp in direct_prefix_nets if dp.prefixlen >= subnet_expand_min]

    matched = []
    subnet_matched = 0
    for prefix_str in all_prefixes:
        try:
            net = ip_network(prefix_str, strict=False)
        except ValueError:
            continue
        # Pass 1: resolved IP is inside this prefix
        if any(ip_obj in net for ip_obj in ip_objs):
            matched.append(prefix_str)
            continue
        # Pass 2: this prefix is a more-specific subnet of a sufficiently narrow direct prefix
        if any(net.subnet_of(dp) for dp in narrow_direct if dp.version == net.version):
            matched.append(prefix_str)
            subnet_matched += 1

    _dbg(debug, f"asn_filter asn={asn} source={source} total={len(all_prefixes)} matched={len(matched)} (subnet_expansion={subnet_matched})")
    return matched


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

    def drop_supernets(nets):
        # Sort most-specific first; drop any prefix that is a supernet of an already-kept one.
        # This prevents a /16 supernet from replacing the specific /22s we actually need.
        nets = sorted(nets, key=lambda n: n.prefixlen, reverse=True)
        kept = []
        for net in nets:
            if not any(k.subnet_of(net) for k in kept):
                kept.append(net)
        return [str(n) for n in kept]

    return drop_supernets(networks_v4) + drop_supernets(networks_v6)


def _ensure_direct_ips_covered(
    resolved_ips: list[str],
    prefixes: list[str],
    debug: Optional[list[str]] = None,
) -> list[str]:
    """Safety net: guarantee every directly-resolved IP has its BGP prefix in the result.

    If the main discovery logic missed a prefix for a resolved IP (anycast/GeoDNS mismatch,
    edge case in ASN filtering, etc.), look it up via RIPE and append it.
    Works for every discovery mode.
    """
    prefix_nets: list[tuple[str, object]] = []
    covered: set[str] = set(prefixes)
    for p in prefixes:
        try:
            prefix_nets.append((p, ip_network(p, strict=False)))
        except ValueError:
            pass

    result = list(prefixes)
    for ip in resolved_ips:
        try:
            ip_obj = ip_address(ip)
        except ValueError:
            continue
        if any(ip_obj in net for _, net in prefix_nets):
            continue
        # IP is not covered by any known prefix — fetch it from RIPE
        prefix, _ = _ip_to_prefix_ripestat(ip, debug=debug)
        if prefix and prefix not in covered:
            covered.add(prefix)
            result.append(prefix)
            try:
                prefix_nets.append((prefix, ip_network(prefix, strict=False)))
            except ValueError:
                pass
            _dbg(debug, f"safety_net ip={ip} added_prefix={prefix}")

    return result


def discover_domain(
    domain: str,
    debug: Optional[list[str]] = None,
    mode: str = DISCOVERY_MODE_DEFAULT,
) -> tuple[Optional[str], list[str], list[str]]:
    domain = _normalize_domain(domain)
    _dbg(debug, f"discover domain={domain} mode={mode}")
    resolved_ips = _resolve_ips(domain, debug=debug)
    if not resolved_ips:
        return None, [], []

    max_ips = _int_env("DISCOVERY_MAX_IPS", 12)
    ips = resolved_ips[:max_ips]
    if len(resolved_ips) > len(ips):
        _dbg(debug, f"dns truncated_ips={len(ips)} of {len(resolved_ips)}")

    if mode == "smart":
        # --- Phase 1: domain expansion ---
        max_subdomains = _int_env("DISCOVERY_SMART_MAX_SUBDOMAINS", 80)

        # 1a: CT logs → subdomains of the input domain
        ct_subdomains = _crtsh_subdomains(domain, debug=debug)

        # 1b: HTTP crawl → cross-domain resources (e.g. googlevideo.com from youtube.com)
        crawled_hosts = _http_crawl_domains(domain, debug=debug)

        all_domains: list[str] = [domain] + ct_subdomains[:max_subdomains] + crawled_hosts
        # Deduplicate while preserving order; input domain is always first
        seen_domains: dict[str, None] = {}
        for d in all_domains:
            seen_domains.setdefault(d, None)
        all_domains = list(seen_domains.keys())
        _dbg(debug, f"smart domain_expansion total={len(all_domains)}")

        # --- Phase 2: bulk DNS resolution ---
        all_ips: dict[str, None] = {}
        for d in all_domains:
            for ip in _resolve_ips(d, debug=debug):
                all_ips.setdefault(ip, None)
        _dbg(debug, f"smart bulk_dns unique_ips={len(all_ips)}")

        # Phase 2b: DoH resolution to catch anycast IPs invisible to local DNS
        # (e.g. Cloudflare returns different PoP IPs depending on the resolver location)
        if _bool_env("DISCOVERY_ENABLE_DOH", True):
            doh_limit = _int_env("DISCOVERY_DOH_MAX_DOMAINS", 10)
            before_doh = len(all_ips)
            for d in all_domains[:doh_limit]:
                for ip in _resolve_ips_doh(d, debug=debug):
                    all_ips.setdefault(ip, None)
            if len(all_ips) > before_doh:
                _dbg(debug, f"smart doh_extra_ips={len(all_ips) - before_doh} total={len(all_ips)}")

        ip_set = set(all_ips.keys())
        _dbg(debug, f"smart total_unique_ips={len(ip_set)}")

        if not ip_set:
            return None, list(ip_set), []

        # --- Phase 3: exact prefix per IP (RIPE Stat network-info) ---
        # Deduplicate: one representative IP per /24 is enough — same prefix will be returned
        rep_ips: dict[str, str] = {}  # /24 network string → one IP
        for ip in ip_set:
            try:
                net24 = str(ip_network(f"{ip}/24", strict=False))
            except ValueError:
                net24 = ip
            rep_ips.setdefault(net24, ip)
        _dbg(debug, f"smart ripe_lookups={len(rep_ips)} (deduped from {len(ip_set)} ips)")

        direct_prefixes: dict[str, None] = {}
        asns: dict[str, None] = {}
        primary_asn: Optional[str] = None
        for ip in rep_ips.values():
            prefix, asn = _ip_to_prefix_ripestat(ip, debug=debug)
            if asn:
                asns.setdefault(asn, None)
                if not primary_asn:
                    primary_asn = asn
            if prefix:
                direct_prefixes.setdefault(prefix, None)

        _dbg(debug, f"smart direct_prefixes={len(direct_prefixes)} asns={list(asns.keys())}")

        # Fallback ASN detection if RIPE didn't return one
        if not primary_asn:
            for ip in ip_set:
                asn, _ = _ip_to_asn(ip, debug=debug)
                if asn:
                    primary_asn = asn
                    asns.setdefault(asn, None)
                    break

        # --- Phase 4: ASN expansion (strategy depends on whether domain expansion worked) ---
        expanded_prefixes = set(direct_prefixes.keys())

        # If crt.sh and crawl both failed, all_domains still equals [domain] (no expansion).
        # For CDN-only domains (googlevideo.com, ytimg.com, etc.) this means we only have 1-2
        # local GeoDNS IPs and the filtered approach would miss CDN PoPs in other IP blocks.
        # In that case fall back to fetching ALL ASN prefixes, same as asn_prefixes mode.
        domain_was_expanded = len(all_domains) > 1
        _dbg(debug, f"smart domain_was_expanded={domain_was_expanded}")

        if not domain_was_expanded:
            max_asn_prefixes = _int_env("DISCOVERY_SMART_MAX_ASN_PREFIXES", 500)
            for asn in asns:
                all_asn_pref, _ = _asn_prefixes(asn, debug=debug)
                if len(all_asn_pref) > max_asn_prefixes:
                    _dbg(debug, f"smart fallback=direct_only asn={asn} asn_total={len(all_asn_pref)} exceeds threshold={max_asn_prefixes} (large multi-tenant CDN, using direct prefixes only)")
                else:
                    _dbg(debug, f"smart fallback=full_asn asn={asn} asn_total={len(all_asn_pref)}")
                    expanded_prefixes.update(all_asn_pref)
        else:
            # Normal path: only include ASN prefixes that overlap with what we actually resolved.
            # Build ip_network objects for the direct RIPE prefixes for the subnet filter.
            direct_prefix_nets = []
            for p in direct_prefixes:
                try:
                    direct_prefix_nets.append(ip_network(p, strict=False))
                except ValueError:
                    pass
            _dbg(debug, f"smart direct_prefix_nets={[str(n) for n in direct_prefix_nets]}")
            for asn in asns:
                filtered = _asn_prefixes_filtered(asn, ip_set, direct_prefix_nets, debug=debug)
                expanded_prefixes.update(filtered)

        _dbg(debug, f"smart expanded_prefixes={len(expanded_prefixes)}")
        optimized = _optimize_prefixes(list(expanded_prefixes))
        optimized = _ensure_direct_ips_covered(resolved_ips, optimized, debug)
        _dbg(debug, f"smart final_prefixes={len(optimized)}")
        return primary_asn, list(ip_set)[:max_ips], optimized

    elif mode == "asn_prefixes":
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
        optimized = _ensure_direct_ips_covered(resolved_ips, optimized, debug)
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
        prefixes_out = _ensure_direct_ips_covered(resolved_ips, prefixes_out, debug)
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
        prefixes_out_ni = _ensure_direct_ips_covered(resolved_ips, prefixes_out_ni, debug)
        _dbg(debug, f"network_info result asn={primary_asn_ni} prefixes={len(prefixes_out_ni)}")
        return primary_asn_ni, ips, prefixes_out_ni

