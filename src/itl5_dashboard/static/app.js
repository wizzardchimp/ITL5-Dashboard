const STATE = {
  available: "Available",
  degraded: "Degraded",
  unavailable: "Unavailable",
  pending: "Pending",
};

const EVENT = {
  informational: "Informational",
  warning: "Warning",
  exception: "Exception",
};

const ACTION = {
  none: "No action",
  review: "Review",
  required: "Action required",
};

let latest = null;

function el(tag, attrs, children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value == null) continue;
      if (key === "class") node.className = value;
      else node.setAttribute(key, value);
    }
  }
  for (const child of children || []) {
    if (child != null) node.append(child);
  }
  return node;
}

function ms(value) {
  if (value == null || Number.isNaN(value)) return "—";
  return Math.round(value) + " ms";
}

function pct(value) {
  if (value == null) return "—";
  return Number(value).toFixed(1) + "%";
}

function clock(ts) {
  return new Date(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function duration(value) {
  const s = Math.max(0, Math.round(value / 1000));
  if (s < 60) return s + "s";
  const m = Math.floor(s / 60);
  const rem = s % 60;
  if (m < 60) return rem ? m + "m " + rem + "s" : m + "m";
  const h = Math.floor(m / 60);
  return h + "h " + (m % 60) + "m";
}

function ago(ts) {
  const s = Math.max(0, (Date.now() - ts) / 1000);
  if (s < 5) return "just now";
  if (s < 60) return Math.floor(s) + "s ago";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  return Math.floor(s / 3600) + "h ago";
}

function spark(points, target) {
  if (!points || points.filter((value) => value != null).length < 2) return null;
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 96 28");
  svg.setAttribute("class", "spark");
  svg.setAttribute("aria-hidden", "true");
  const nums = points.filter((value) => value != null);
  const max = Math.max(target || 0, ...nums, 1) * 1.2;
  const w = 96;
  const h = 28;
  if (target) {
    const y = h - (target / max) * (h - 2) - 1;
    const line = document.createElementNS(ns, "line");
    line.setAttribute("x1", "0");
    line.setAttribute("x2", String(w));
    line.setAttribute("y1", y.toFixed(1));
    line.setAttribute("y2", y.toFixed(1));
    line.setAttribute("class", "spark-target");
    svg.append(line);
  }
  let d = "";
  let pen = false;
  points.forEach((value, index) => {
    if (value == null) {
      pen = false;
      return;
    }
    const x = points.length === 1 ? 0 : (index / (points.length - 1)) * w;
    const y = h - (value / max) * (h - 2) - 1;
    d += (pen ? "L" : "M") + x.toFixed(1) + " " + y.toFixed(1);
    pen = true;
  });
  const path = document.createElementNS(ns, "path");
  path.setAttribute("d", d);
  path.setAttribute("class", "spark-line");
  svg.append(path);
  return svg;
}

function tile(label, value, tone) {
  return el("div", { class: "tile" + (tone ? " " + tone : "") }, [
    el("span", null, [label]),
    el("strong", null, [String(value)]),
  ]);
}

function renderServices(list) {
  const root = document.getElementById("services");
  if (!list.length) {
    root.replaceChildren(el("p", { class: "empty" }, ["No services in the catalogue."]));
    return;
  }
  const table = el("table", null, [
    el("thead", null, [
      el("tr", null, [
        el("th", null, ["Service"]),
        el("th", null, ["State"]),
        el("th", null, ["Latency"]),
        el("th", null, ["Availability"]),
        el("th", null, ["SLA"]),
        el("th", null, [""]),
      ]),
    ]),
  ]);
  const body = el("tbody");
  for (const svc of list) {
    const latency = el("td", { class: "metric" }, [
      el("div", { class: svc.state === "degraded" ? "hot" : "" }, [ms(svc.latencyMs)]),
      el("div", { class: "sub" }, ["target " + svc.latencyTargetMs + " ms"]),
      spark(svc.spark, svc.latencyTargetMs),
    ]);
    const availability = el("td", { class: "metric" });
    if (svc.availability == null) {
      availability.append("—");
    } else {
      availability.append(
        el("div", null, [pct(svc.availability)]),
        el("div", { class: "sub" }, [svc.samples + " samples · " + Number(svc.availabilityTarget).toFixed(1) + "%"]),
      );
    }
    const remove = el("button", { class: "ghost", type: "button", "aria-label": "Remove " + svc.name }, ["Remove"]);
    remove.addEventListener("click", () => removeService(svc.id, remove));
    const checked = svc.lastCheckedAt
      ? el("div", { class: "sub", "data-ts": String(svc.lastCheckedAt) }, [ago(svc.lastCheckedAt)])
      : el("div", { class: "sub" }, ["Not checked"]);
    body.append(
      el("tr", { class: "state-" + svc.state }, [
        el("td", null, [
          el("strong", { class: "svc-name" }, [svc.name]),
          el("div", { class: "sub" }, ["CI " + svc.id + " · " + svc.owner + " · " + svc.criticality]),
          el("div", { class: "url", title: svc.synthetic ? "Simulated probe" : svc.url }, [
            svc.synthetic ? "Simulated probe" : svc.url,
          ]),
        ]),
        el("td", null, [
          el("span", { class: "badge", title: svc.detail || "" }, [
            el("span", { class: "mark", "aria-hidden": "true" }),
            STATE[svc.state] || svc.state,
          ]),
          checked,
        ]),
        latency,
        availability,
        el("td", { class: svc.slaBreached ? "breach" : svc.state === "pending" ? "" : "met" }, [
          svc.state === "pending" ? "—" : svc.slaBreached ? "Breach" : "Met",
        ]),
        el("td", { class: "actions" }, [remove]),
      ]),
    );
  }
  table.append(body);
  root.replaceChildren(table);
}

function renderIncidents(list, threshold) {
  const root = document.getElementById("incidents");
  const open = list.filter((inc) => inc.open);
  const closed = list.filter((inc) => !inc.open);
  const nodes = [];
  if (!open.length) {
    nodes.push(el("p", { class: "empty" }, ["No open incidents."]));
  }
  for (const inc of open) nodes.push(incidentNode(inc));
  if (closed.length) {
    nodes.push(el("div", { class: "group-label" }, ["Recently resolved"]));
    for (const inc of closed) nodes.push(incidentNode(inc));
  }
  root.replaceChildren(...nodes);
  const sub = document.getElementById("incident-sub");
  const n = threshold === 1 ? "1 consecutive exception" : threshold + " consecutive exceptions";
  sub.textContent = "Opens after " + n;
}

function incidentNode(inc) {
  const when = inc.open
    ? el("div", { class: "when", "data-open": String(inc.openedAt) }, ["Open · " + duration(Date.now() - inc.openedAt)])
    : el("div", { class: "when" }, ["Resolved · " + duration(inc.durationMs)]);
  return el("article", { class: "incident" }, [
    el("div", { class: "incident-top" }, [
      el("span", { class: "pri " + String(inc.priority).toLowerCase() }, [inc.priority]),
      el("strong", null, [inc.serviceName]),
    ]),
    el("div", { class: "sub" }, [inc.summary]),
    when,
  ]);
}

function renderEvents(list) {
  const root = document.getElementById("events");
  const top = root.scrollTop;
  if (!list.length) {
    root.replaceChildren(el("p", { class: "empty" }, ["No events yet."]));
    return;
  }
  root.replaceChildren(
    ...list.map((ev) =>
      el("article", { class: "event" }, [
        el("time", { datetime: new Date(ev.ts).toISOString() }, [clock(ev.ts)]),
        el("div", null, [
          el("div", { class: "kind " + ev.event }, [EVENT[ev.event] || ev.event]),
          el("strong", null, [ev.serviceName]),
          el("div", { class: "sub" }, [ev.detail]),
          el("div", { class: "action " + ev.action }, [ACTION[ev.action] || ev.action]),
        ]),
      ]),
    ),
  );
  if (top > 0) root.scrollTop = top;
}

function render(data) {
  if (!data || !data.summary) return;
  latest = data;
  const summary = data.summary;
  document.title = summary.openIncidents ? "(" + summary.openIncidents + ") Service health" : "Service health";
  document.getElementById("summary").replaceChildren(
    tile("Available", summary.available, summary.available ? "available" : ""),
    tile("Degraded", summary.degraded, summary.degraded ? "degraded" : ""),
    tile("Unavailable", summary.unavailable, summary.unavailable ? "unavailable" : ""),
    tile("Open incidents", summary.openIncidents, summary.openIncidents ? "alert" : ""),
    tile("SLA breaches", summary.slaBreaches, summary.slaBreaches ? "alert" : ""),
  );
  const hours = data.historyHours === 1 ? "1h window" : data.historyHours + "h window";
  document.getElementById("catalogue-sub").textContent = summary.total + " services · " + hours;
  renderServices(data.services || []);
  renderIncidents(data.incidents || [], data.incidentThreshold);
  renderEvents(data.events || []);
  paintSweep();
}

function paintSweep() {
  const node = document.getElementById("swept");
  if (!latest) return;
  const age = Math.max(0, (Date.now() - latest.generatedAt) / 1000);
  const remain = Math.max(0, latest.intervalSeconds - age);
  node.textContent = "Sweep " + clock(latest.generatedAt) + " · next " + Math.ceil(remain) + "s";
}

function tick() {
  paintSweep();
  document.querySelectorAll("[data-ts]").forEach((node) => {
    node.textContent = ago(Number(node.getAttribute("data-ts")));
  });
  document.querySelectorAll("[data-open]").forEach((node) => {
    node.textContent = "Open · " + duration(Date.now() - Number(node.getAttribute("data-open")));
  });
}

function setLive(label, mode) {
  const live = document.getElementById("live");
  live.classList.toggle("on", mode === "on");
  live.classList.toggle("warn", mode === "warn");
  document.getElementById("live-label").textContent = label;
}

async function removeService(id, button) {
  button.disabled = true;
  try {
    const res = await fetch("/api/services/" + encodeURIComponent(id), { method: "DELETE" });
    const data = await res.json();
    if (!res.ok) return;
    render(data);
  } finally {
    button.disabled = false;
  }
}

async function boot() {
  try {
    const res = await fetch("/api/snapshot", { cache: "no-store" });
    if (res.ok) render(await res.json());
  } catch (err) {
    setLive("Offline", "warn");
  }
  const source = new EventSource("/api/events");
  source.onopen = () => setLive("Live", "on");
  source.onerror = () => setLive("Reconnecting", "warn");
  source.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      render(msg.data || msg);
    } catch (err) {
      return;
    }
  };
}

setInterval(tick, 1000);
boot();
