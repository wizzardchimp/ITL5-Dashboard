from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

METADATA_HOSTS = {
    "metadata.google.internal",
    "metadata.google.com",
}
METADATA_IPS = {
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("169.254.170.2"),
    ipaddress.ip_address("100.100.100.200"),
    ipaddress.ip_address("fd00:ec2::254"),
}
BLOCKED_ADDRESS = "Link-local and metadata addresses are blocked"


def classify(status_code, latency_ms, latency_target_ms, error):
    if error or status_code is None or not 200 <= status_code <= 299:
        return "exception", "unavailable"
    if latency_ms is None or latency_ms > latency_target_ms:
        return "warning", "degraded"
    return "informational", "available"


def event_detail(event, status_code, latency_ms, latency_target_ms, error):
    if event == "informational" and latency_ms is not None:
        return f"{round(latency_ms)} ms within {latency_target_ms} ms target"
    if event == "warning" and latency_ms is not None:
        return f"Latency {round(latency_ms)} ms above {latency_target_ms} ms target"
    if error:
        return error
    if status_code:
        return f"HTTP {status_code}"
    return "Unavailable"


def action_for(event):
    if event == "exception":
        return "required"
    if event == "warning":
        return "review"
    return "none"


def priority_for(criticality):
    return {"high": "P1", "medium": "P2", "low": "P3"}.get(criticality, "P2")


def next_incident_state(fail_streak, has_open_incident, event, threshold):
    if event == "exception":
        streak = fail_streak + 1
        if streak >= threshold and not has_open_incident:
            return streak, "open"
        return streak, None
    if event == "informational" and has_open_incident:
        return 0, "resolve"
    return 0, None


def availability_percent(states):
    if not states:
        return None
    up = sum(1 for state in states if state != "unavailable")
    return up / len(states) * 100


def sla_breached(availability, target, state):
    if state in ("degraded", "unavailable"):
        return True
    if state != "available" or availability is None:
        return False
    return availability + 1e-6 < target


def synthetic_probe(elapsed, latency_target_ms):
    phase = elapsed % 80
    if phase < 20:
        return 200, 48.0, None
    if phase < 32:
        return 200, float(latency_target_ms) + 350.0, None
    return None, None, "connection refused"


def _blocked_ip(ip):
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.is_link_local or ip.is_unspecified or ip in METADATA_IPS:
        return True
    return False


def _default_resolver(host):
    infos = socket.getaddrinfo(host, None)
    return [info[4][0] for info in infos]


def validate_service_url(url, resolver=None):
    if not url or not isinstance(url, str):
        return "URL is required"
    raw = url.strip()
    if raw != url or any(ch.isspace() for ch in raw) or len(raw) > 2000:
        return "URL is invalid"
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        return "Only http and https URLs are allowed"
    if parsed.username or parsed.password:
        return "URLs with credentials are not allowed"
    host = parsed.hostname
    if not host:
        return "URL is missing a host"
    host = host.lower().rstrip(".")
    if host in METADATA_HOSTS or host.endswith(".metadata.google.internal"):
        return BLOCKED_ADDRESS
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _blocked_ip(literal):
            return BLOCKED_ADDRESS
        return None
    resolve = resolver or _default_resolver
    try:
        addresses = resolve(host)
    except OSError:
        return "Host could not be resolved"
    if not addresses:
        return "Host could not be resolved"
    for addr in addresses:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return "Host could not be resolved"
        if _blocked_ip(ip):
            return BLOCKED_ADDRESS
    return None
