const STATE = {
  available: "Available",
  degraded: "Degraded",
  unavailable: "Unavailable",
  pending: "Pending",
};

let services = [];
let editingId = null;

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

function form() {
  return document.getElementById("editor");
}

function payload() {
  const node = form();
  return {
    name: node.name.value.trim(),
    url: node.url.value.trim(),
    owner: node.owner.value.trim(),
    criticality: node.criticality.value,
    availabilityTarget: Number(node.availabilityTarget.value),
    latencyTargetMs: Number(node.latencyTargetMs.value),
    timeoutMs: Number(node.timeoutMs.value),
  };
}

function clearForm() {
  editingId = null;
  const node = form();
  node.reset();
  node.criticality.value = "medium";
  node.availabilityTarget.value = "99.9";
  node.latencyTargetMs.value = "500";
  node.timeoutMs.value = "4000";
  document.getElementById("editor-title").textContent = "Add a service";
  document.getElementById("editor-sub").textContent = "HTTP or HTTPS";
  document.getElementById("save").textContent = "Add service";
  document.getElementById("cancel").hidden = true;
  document.getElementById("form-error").textContent = "";
  renderList();
}

function editService(svc) {
  editingId = svc.id;
  const node = form();
  node.name.value = svc.name;
  node.url.value = svc.url;
  node.owner.value = svc.owner === "Unassigned" ? "" : svc.owner;
  node.criticality.value = svc.criticality;
  node.availabilityTarget.value = svc.availabilityTarget;
  node.latencyTargetMs.value = svc.latencyTargetMs;
  node.timeoutMs.value = svc.timeoutMs || 4000;
  document.getElementById("editor-title").textContent = "Edit " + svc.name;
  document.getElementById("editor-sub").textContent = svc.synthetic ? "Simulated probe" : "CI " + svc.id;
  document.getElementById("save").textContent = "Save changes";
  document.getElementById("cancel").hidden = false;
  document.getElementById("form-error").textContent = "";
  renderList();
  node.name.focus();
}

function renderList() {
  const root = document.getElementById("list");
  const count = document.getElementById("count");
  count.textContent = services.length === 1 ? "1 service" : services.length + " services";
  if (!services.length) {
    root.replaceChildren(el("p", { class: "empty" }, ["No services in the catalogue."]));
    return;
  }
  root.replaceChildren(
    ...services.map((svc) => {
      const edit = el("button", { class: "ghost", type: "button" }, [editingId === svc.id ? "Editing" : "Edit"]);
      const remove = el("button", { class: "ghost danger", type: "button" }, ["Remove"]);
      edit.addEventListener("click", () => editService(svc));
      remove.addEventListener("click", () => removeService(svc, remove));
      const url = svc.synthetic ? "Simulated probe" : svc.url;
      return el("article", { class: "manage-row state-" + svc.state + (editingId === svc.id ? " selected" : "") }, [
        el("div", null, [
          el("div", { class: "manage-name" }, [
            el("strong", null, [svc.name]),
            el("span", { class: "badge" }, [
              el("span", { class: "mark", "aria-hidden": "true" }),
              STATE[svc.state] || svc.state,
            ]),
          ]),
          el("div", { class: "url", title: url }, [url]),
          el("div", { class: "sub" }, [
            svc.owner +
              " · " +
              svc.criticality +
              " · " +
              Number(svc.availabilityTarget).toFixed(1) +
              "% · " +
              svc.latencyTargetMs +
              " ms · " +
              (svc.timeoutMs || 4000) +
              " ms timeout",
          ]),
        ]),
        el("div", { class: "manage-actions" }, [edit, remove]),
      ]);
    }),
  );
}

function applySnapshot(data) {
  services = data.services || [];
  if (editingId && !services.some((svc) => svc.id === editingId)) clearForm();
  else renderList();
}

async function removeService(svc, button) {
  if (!window.confirm("Stop monitoring " + svc.name + "?")) return;
  button.disabled = true;
  try {
    const res = await fetch("/api/services/" + encodeURIComponent(svc.id), { method: "DELETE" });
    const data = await res.json();
    if (!res.ok) {
      document.getElementById("form-error").textContent = data.error || "Could not remove service";
      return;
    }
    if (editingId === svc.id) clearForm();
    applySnapshot(data);
  } finally {
    button.disabled = false;
  }
}

form().addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = document.getElementById("form-error");
  const button = document.getElementById("save");
  error.textContent = "";
  button.disabled = true;
  const id = editingId;
  try {
    const res = await fetch(id ? "/api/services/" + encodeURIComponent(id) : "/api/services", {
      method: id ? "PUT" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload()),
    });
    const data = await res.json();
    if (!res.ok) {
      error.textContent = data.error || "Could not save service";
      return;
    }
    applySnapshot(data);
    clearForm();
  } catch (err) {
    error.textContent = "Could not save service";
  } finally {
    button.disabled = false;
  }
});

document.getElementById("cancel").addEventListener("click", clearForm);

async function boot() {
  try {
    const res = await fetch("/api/snapshot", { cache: "no-store" });
    if (res.ok) applySnapshot(await res.json());
    else document.getElementById("count").textContent = "Offline";
  } catch (err) {
    document.getElementById("count").textContent = "Offline";
  }
  const source = new EventSource("/api/events");
  source.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      applySnapshot(msg.data || msg);
    } catch (err) {
      return;
    }
  };
}

boot();
