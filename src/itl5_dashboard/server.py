from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import socket
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from itl5_dashboard.rules import (
    action_for,
    availability_percent,
    classify,
    event_detail,
    next_incident_state,
    priority_for,
    sla_breached,
    synthetic_probe,
    validate_service_url,
)

PACKAGE = Path(__file__).resolve().parent
STATIC = PACKAGE / "static"
SEED = PACKAGE / "default_config" / "services.json"


def resolve_config_path():
    override = os.environ.get("ITL5_CONFIG")
    if override:
        path = Path(override).expanduser()
    else:
        local = Path.cwd() / "config" / "services.json"
        path = local if local.is_file() else Path.home() / ".config" / "itl5-dashboard" / "services.json"
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(SEED.read_text(encoding="utf-8"), encoding="utf-8")
    return path


CONFIG_PATH = resolve_config_path()
log = logging.getLogger("healthboard")

services = []
by_id = {}
history = {}
streaks = {}
open_incidents = {}
resolved = []
events = []
subscribers = []
config_meta = {
    "port": 8787,
    "intervalSeconds": 15,
    "historyHours": 24,
    "incidentThreshold": 2,
}
event_seq = 0
incident_seq = 0
SYNTH_T0 = time.monotonic()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def http_error_302(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


OPENER = urllib.request.build_opener(NoRedirect)


def short_error(exc):
    if isinstance(exc, socket.timeout):
        return "timed out"
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, socket.timeout):
        return "timed out"
    text = str(reason).split("\n", 1)[0].strip()
    lowered = text.lower()
    if "timed out" in lowered:
        return "timed out"
    if "certificate" in lowered or "tls" in lowered or "ssl" in lowered:
        return "tls error"
    if "refused" in lowered:
        return "connection refused"
    if "nodename" in lowered or "name or service not known" in lowered or "servname" in lowered:
        return "dns error"
    return text[:80] or "unreachable"


def http_probe(url, timeout_ms):
    timeout = max(0.1, min(int(timeout_ms or 4000), 15000) / 1000)
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": "itil5-healthboard/1.0", "Accept": "*/*"},
    )
    start = time.perf_counter()
    status = None
    error = None
    try:
        with OPENER.open(req, timeout=timeout) as resp:
            resp.read(256)
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except Exception as exc:
        error = short_error(exc)
    latency = round((time.perf_counter() - start) * 1000, 1)
    if status is None:
        latency = None
    return status, latency, error


def normalize_stored(item):
    criticality = item.get("criticality") or "medium"
    if criticality not in ("high", "medium", "low"):
        criticality = "medium"
    return {
        "id": str(item["id"]),
        "name": str(item["name"]),
        "url": str(item["url"]),
        "owner": str(item.get("owner") or "Unassigned"),
        "criticality": criticality,
        "availabilityTarget": float(item.get("availabilityTarget", 99.9)),
        "latencyTargetMs": int(item.get("latencyTargetMs", 500)),
        "timeoutMs": int(item.get("timeoutMs", 4000)),
        "synthetic": bool(item.get("synthetic")),
    }


def load_config():
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        config_meta["port"] = int(raw.get("port") or 8787)
        config_meta["intervalSeconds"] = max(5, int(raw.get("intervalSeconds") or 15))
        config_meta["historyHours"] = max(1, int(raw.get("historyHours") or 24))
        config_meta["incidentThreshold"] = max(1, int(raw.get("incidentThreshold") or 2))
        services.clear()
        by_id.clear()
        for item in raw.get("services") or []:
            svc = normalize_stored(item)
            services.append(svc)
            by_id[svc["id"]] = svc
            history.setdefault(svc["id"], [])
            streaks.setdefault(svc["id"], 0)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Could not load {CONFIG_PATH}: {exc}") from exc


def urllib_parse_host(url):
    from urllib.parse import urlparse

    return (urlparse(url).hostname or "").lower()


def align_self(port):
    self_svc = by_id.get("self")
    if not self_svc:
        return
    if urllib_parse_host(self_svc["url"]) in ("127.0.0.1", "localhost"):
        self_svc["url"] = f"http://127.0.0.1:{port}/health"


def service_to_config(svc):
    item = {
        "id": svc["id"],
        "name": svc["name"],
        "url": svc["url"],
        "owner": svc["owner"],
        "criticality": svc["criticality"],
        "availabilityTarget": svc["availabilityTarget"],
        "latencyTargetMs": svc["latencyTargetMs"],
        "timeoutMs": svc["timeoutMs"],
    }
    if svc.get("synthetic"):
        item["synthetic"] = True
    return item


def save_config():
    payload = {
        "port": config_meta["port"],
        "intervalSeconds": config_meta["intervalSeconds"],
        "historyHours": config_meta["historyHours"],
        "incidentThreshold": config_meta["incidentThreshold"],
        "services": [service_to_config(svc) for svc in services],
    }
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(CONFIG_PATH)


def new_id(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24] or "service"
    for _ in range(5):
        candidate = f"{slug}-{secrets.token_hex(3)}"
        if candidate not in by_id:
            return candidate
    return f"{slug}-{secrets.token_hex(8)}"


def record_event(svc, event, state, detail, now_ms):
    global event_seq
    event_seq += 1
    events.append(
        {
            "id": event_seq,
            "ts": now_ms,
            "serviceId": svc["id"],
            "serviceName": svc["name"],
            "event": event,
            "state": state,
            "detail": detail,
            "action": action_for(event),
        }
    )
    del events[:-80]


def open_incident(svc, detail, now_ms):
    global incident_seq
    incident_seq += 1
    open_incidents[svc["id"]] = {
        "id": f"INC-{incident_seq:04d}",
        "serviceId": svc["id"],
        "serviceName": svc["name"],
        "priority": priority_for(svc["criticality"]),
        "openedAt": now_ms,
        "resolvedAt": None,
        "summary": detail,
    }


def resolve_incident(service_id, now_ms):
    inc = open_incidents.pop(service_id, None)
    if not inc:
        return
    inc["resolvedAt"] = now_ms
    resolved.append(inc)
    del resolved[:-20]


def apply_result(service_id, status, latency, error, now_ms):
    svc = by_id.get(service_id)
    if not svc:
        return
    event, state = classify(status, latency, svc["latencyTargetMs"], error)
    detail = event_detail(event, status, latency, svc["latencyTargetMs"], error)
    hist = history.setdefault(service_id, [])
    hist.append(
        {
            "ts": now_ms,
            "state": state,
            "event": event,
            "latencyMs": latency,
            "statusCode": status,
            "detail": detail,
        }
    )
    cutoff = now_ms - config_meta["historyHours"] * 3600 * 1000
    history[service_id] = [check for check in hist if check["ts"] >= cutoff]
    streak, action = next_incident_state(
        streaks.get(service_id, 0),
        service_id in open_incidents,
        event,
        config_meta["incidentThreshold"],
    )
    streaks[service_id] = streak
    if action == "open":
        open_incident(svc, detail, now_ms)
    elif action == "resolve":
        resolve_incident(service_id, now_ms)
    record_event(svc, event, state, detail, now_ms)


def incident_view(inc, now_ms):
    end = inc["resolvedAt"] or now_ms
    return {
        "id": inc["id"],
        "serviceId": inc["serviceId"],
        "serviceName": inc["serviceName"],
        "priority": inc["priority"],
        "openedAt": inc["openedAt"],
        "resolvedAt": inc["resolvedAt"],
        "summary": inc["summary"],
        "durationMs": end - inc["openedAt"],
        "open": inc["resolvedAt"] is None,
    }


def build_snapshot():
    now_ms = int(time.time() * 1000)
    views = []
    for svc in services:
        hist = history.get(svc["id"], [])
        latest = hist[-1] if hist else None
        state = latest["state"] if latest else "pending"
        avail = availability_percent([check["state"] for check in hist])
        views.append(
            {
                "id": svc["id"],
                "name": svc["name"],
                "url": svc["url"],
                "owner": svc["owner"],
                "criticality": svc["criticality"],
                "synthetic": bool(svc.get("synthetic")),
                "availabilityTarget": svc["availabilityTarget"],
                "latencyTargetMs": svc["latencyTargetMs"],
                "timeoutMs": svc["timeoutMs"],
                "availability": None if avail is None else round(avail, 1),
                "samples": len(hist),
                "state": state,
                "latencyMs": latest["latencyMs"] if latest else None,
                "slaBreached": sla_breached(avail, svc["availabilityTarget"], state),
                "detail": latest["detail"] if latest else None,
                "lastCheckedAt": latest["ts"] if latest else None,
                "spark": [check["latencyMs"] for check in hist[-24:]],
            }
        )
    rank = {"P1": 0, "P2": 1, "P3": 2}
    open_list = sorted(open_incidents.values(), key=lambda inc: (rank.get(inc["priority"], 9), inc["openedAt"]))
    recent = list(reversed(resolved[-6:]))
    summary = {
        "total": len(views),
        "available": sum(1 for view in views if view["state"] == "available"),
        "degraded": sum(1 for view in views if view["state"] == "degraded"),
        "unavailable": sum(1 for view in views if view["state"] == "unavailable"),
        "pending": sum(1 for view in views if view["state"] == "pending"),
        "openIncidents": len(open_list),
        "slaBreaches": sum(1 for view in views if view["slaBreached"]),
    }
    return {
        "generatedAt": now_ms,
        "intervalSeconds": config_meta["intervalSeconds"],
        "historyHours": config_meta["historyHours"],
        "incidentThreshold": config_meta["incidentThreshold"],
        "summary": summary,
        "services": views,
        "events": list(reversed(events[-40:])),
        "incidents": [incident_view(inc, now_ms) for inc in open_list + recent],
    }


def envelope():
    return {"type": "snapshot", "data": build_snapshot()}


def broadcast():
    payload = envelope()
    for queue in list(subscribers):
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            pass


def snap_response():
    return JSONResponse(build_snapshot(), headers={"Cache-Control": "no-store"})


async def probe(svc):
    try:
        if svc.get("synthetic") or str(svc["url"]).startswith("synthetic:"):
            status, latency, error = synthetic_probe(time.monotonic() - SYNTH_T0, svc["latencyTargetMs"])
            return svc["id"], status, latency, error
        status, latency, error = await asyncio.to_thread(http_probe, svc["url"], svc["timeoutMs"])
        return svc["id"], status, latency, error
    except Exception as exc:
        return svc["id"], None, None, short_error(exc)


async def probe_loop():
    await asyncio.sleep(0.6)
    while True:
        started = time.monotonic()
        try:
            current = list(services)
            results = await asyncio.gather(*(probe(svc) for svc in current))
            now_ms = int(time.time() * 1000)
            for service_id, status, latency, error in results:
                apply_result(service_id, status, latency, error, now_ms)
            broadcast()
        except Exception:
            log.exception("sweep failed")
        elapsed = time.monotonic() - started
        await asyncio.sleep(max(1, config_meta["intervalSeconds"] - elapsed))


def parse_service_fields(body, existing=None):
    if not isinstance(body, dict):
        return None, "Expected an object"
    name = str(body.get("name") or "").strip()
    if not name or len(name) > 80 or any(ord(ch) < 32 for ch in name):
        return None, "Name is required"
    url = str(body.get("url") or "").strip()
    owner = str(body.get("owner") or "").strip() or "Unassigned"
    if len(owner) > 80 or any(ord(ch) < 32 for ch in owner):
        return None, "Owner is too long"
    criticality = body.get("criticality") or "medium"
    if criticality not in ("high", "medium", "low"):
        return None, "Criticality must be high, medium, or low"
    avail = body.get("availabilityTarget", 99.9)
    latency = body.get("latencyTargetMs", 500)
    timeout = body.get("timeoutMs", 4000 if not existing else existing["timeoutMs"])
    if isinstance(avail, bool) or not isinstance(avail, (int, float)) or not 0 <= float(avail) <= 100:
        return None, "Availability target must be between 0 and 100"
    if isinstance(latency, bool) or not isinstance(latency, (int, float)) or not 1 <= float(latency) <= 60000:
        return None, "Latency target must be between 1 and 60000 ms"
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 100 <= int(timeout) <= 15000:
        return None, "Timeout must be between 100 and 15000 ms"
    synthetic = bool(existing and existing.get("synthetic") and url == existing["url"])
    if not synthetic:
        url_error = validate_service_url(url)
        if url_error:
            return None, url_error
    return {
        "name": name,
        "url": url,
        "owner": owner,
        "criticality": criticality,
        "availabilityTarget": float(avail),
        "latencyTargetMs": int(latency),
        "timeoutMs": int(timeout),
        "synthetic": synthetic,
    }, None


def parse_new_service(body):
    fields, error = parse_service_fields(body)
    if error:
        return None, error
    fields["id"] = new_id(fields["name"])
    return fields, None


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(probe_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


load_config()
PORT = int(os.environ.get("PORT") or config_meta["port"])
config_meta["port"] = PORT
align_self(PORT)

app = FastAPI(title="Service health", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/manage")
def manage():
    return FileResponse(STATIC / "manage.html", headers={"Cache-Control": "no-store"})


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/snapshot")
async def snapshot():
    return snap_response()


@app.get("/api/events")
async def event_stream():
    queue = asyncio.Queue(maxsize=8)
    subscribers.append(queue)
    queue.put_nowait(envelope())

    async def stream():
        try:
            while True:
                item = await queue.get()
                yield f"data: {json.dumps(item)}\n\n"
        finally:
            if queue in subscribers:
                subscribers.remove(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/services")
async def add_service(body: dict = Body(...)):
    svc, error = parse_new_service(body)
    if error:
        return JSONResponse({"error": error}, status_code=400)
    services.append(svc)
    by_id[svc["id"]] = svc
    history[svc["id"]] = []
    streaks[svc["id"]] = 0
    try:
        save_config()
    except OSError:
        services.pop()
        by_id.pop(svc["id"], None)
        history.pop(svc["id"], None)
        streaks.pop(svc["id"], None)
        return JSONResponse({"error": "Could not save configuration"}, status_code=500)
    broadcast()
    return snap_response()


@app.put("/api/services/{service_id}")
async def update_service(service_id: str, body: dict = Body(...)):
    svc = by_id.get(service_id)
    if not svc:
        return JSONResponse({"error": "Service not found"}, status_code=404)
    fields, error = parse_service_fields(body, existing=svc)
    if error:
        return JSONResponse({"error": error}, status_code=400)
    previous = dict(svc)
    svc.update(fields)
    if service_id in open_incidents:
        open_incidents[service_id]["serviceName"] = svc["name"]
        open_incidents[service_id]["priority"] = priority_for(svc["criticality"])
    try:
        save_config()
    except OSError:
        svc.clear()
        svc.update(previous)
        if service_id in open_incidents:
            open_incidents[service_id]["serviceName"] = previous["name"]
            open_incidents[service_id]["priority"] = priority_for(previous["criticality"])
        return JSONResponse({"error": "Could not save configuration"}, status_code=500)
    if fields["url"] != previous["url"]:
        history[service_id] = []
        streaks[service_id] = 0
        if service_id in open_incidents:
            resolve_incident(service_id, int(time.time() * 1000))
    broadcast()
    return snap_response()


@app.delete("/api/services/{service_id}")
async def delete_service(service_id: str):
    svc = by_id.get(service_id)
    if not svc:
        return JSONResponse({"error": "Service not found"}, status_code=404)
    index = services.index(svc)
    services.pop(index)
    by_id.pop(service_id, None)
    history.pop(service_id, None)
    streaks.pop(service_id, None)
    if service_id in open_incidents:
        resolve_incident(service_id, int(time.time() * 1000))
    try:
        save_config()
    except OSError:
        services.insert(index, svc)
        by_id[service_id] = svc
        return JSONResponse({"error": "Could not save configuration"}, status_code=500)
    broadcast()
    return snap_response()


def main():
    import uvicorn

    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=PORT)


if __name__ == "__main__":
    main()
