const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const bytes = (b) => {
  if (!b) return "0 B";
  const u = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]; let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return `${b.toFixed(b < 10 && i ? 1 : 0)} ${u[i]}`;
};
const pct = (a, b) => (b ? Math.round((a / b) * 100) : 0);
const uptime = (s) => { const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600); return d ? `${d}d ${h}h` : `${h}h ${Math.floor((s % 3600) / 60)}m`; };
const meter = (label, used, total, text) => {
  const p = pct(used, total); const cls = p > 90 ? "bad" : p > 75 ? "warn" : "";
  return `<div class="meter"><div class="meter-top"><span>${label}</span><span>${text ?? `${bytes(used)} / ${bytes(total)}`} · ${p}%</span></div><div class="bar ${cls}"><span style="width:${p}%"></span></div></div>`;
};
const CLONE_OK = new Set(["lvmthin", "zfspool", "rbd", "nfs", "dir", "cifs", "glusterfs", "cephfs"]);
const TITLES = { overview: "Overview", storage: "Storage", network: "Network", guests: "VMs & containers", deploy: "Deploy desktops" };

let data = null, dep = null;

function show(view) {
  if (!TITLES[view]) view = "overview";
  document.querySelectorAll(".view").forEach((v) => v.classList.add("hidden"));
  $(`#view-${view}`).classList.remove("hidden");
  // Disable the submit button while a request is in flight so a double click cannot start two jobs.
const submitOnce = (form, fn) => form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = form.querySelector("button"), label = btn.textContent;
  if (btn.disabled) return;
  btn.disabled = true; btn.textContent = "Starting…";
  try { await fn(form); await load(); } catch (x) { fail(x); }
  btn.disabled = false; btn.textContent = label;
});
submitOnce($("#tmpl-form"), async (f) => { await api("POST", "/api/templates", formData(f)); f.closest("details").open = false; f.password.value = ""; });
submitOnce($("#batch-form"), (f) => api("POST", "/api/batches", formData(f)));
document.addEventListener("click", async (e) => {
  const t = e.target.dataset;
  try {
    if (t.delBatch && window.confirm(`Stop and permanently delete every desktop in batch ${t.delBatch}?`)) { await api("DELETE", `/api/batches/${t.delBatch}`); load(); }
    if (t.delTmpl && window.confirm(`Permanently delete template ${t.delTmpl}? Linked clones made from it must be deleted first.`)) { await api("DELETE", `/api/templates/${t.delTmpl}`); load(); }
  } catch (x) { fail(x); }
});
$("#update").addEventListener("click", async () => {
  if (!window.confirm("Update vdi-tool to the latest revision? The GUI restarts for a few seconds.")) return;
  try { await api("POST", "/api/update"); $("#update").textContent = "Updating…"; setTimeout(() => location.reload(), 20000); } catch (x) { fail(x); }
});
document.querySelectorAll("nav a[data-view]").forEach((a) => a.classList.toggle("active", a.dataset.view === view));
  $("#title").textContent = TITLES[view];
}

function render() {
  const { nodes, storage, networks, guests, cluster } = data;
  const vms = guests.filter((g) => !g.template);
  const running = vms.filter((g) => g.status === "running").length;
  const cpus = nodes.reduce((a, n) => a + n.maxcpu, 0);
  const mem = nodes.reduce((a, n) => a + n.maxmem, 0);
  const memUsed = nodes.reduce((a, n) => a + n.mem, 0);
  const vdiStore = storage.filter((s) => s.content.includes("images"));
  const free = vdiStore.reduce((a, s) => a + (s.total - s.used), 0);

  $("#summary").innerHTML = [
    ["Cluster", cluster ? esc(cluster.name) : "Single node", `${nodes.length} node${nodes.length === 1 ? "" : "s"}`],
    ["Logical CPUs", cpus, "across all nodes"],
    ["Memory", bytes(mem), `${bytes(memUsed)} in use`],
    ["VM disk space free", bytes(free), `${vdiStore.length} storage pools`],
    ["Guests", vms.length, `${running} running · ${guests.length - vms.length} templates`],
  ].map(([l, v, s]) => `<div class="card"><div class="label">${l}</div><div class="value">${v}</div><div class="sub">${s}</div></div>`).join("");

  $("#nodes").innerHTML = nodes.map((n) => `
    <div class="card">
      <div class="node-head"><b><span class="dot ${n.status === "online" ? "on" : "off"}"></span>${esc(n.name)}</b>
        <span class="pill">${n.status === "online" ? "up " + uptime(n.uptime) : esc(n.status)}</span></div>
      ${meter("CPU", n.cpu * n.maxcpu, n.maxcpu, `${(n.cpu * 100).toFixed(1)}% of ${n.maxcpu} threads`)}
      ${meter("Memory", n.mem, n.maxmem)}
    </div>`).join("") || `<div class="empty">No nodes found.</div>`;

  $("#storage-table").innerHTML = `<tr><th>Storage</th><th>Node</th><th>Type</th><th>Shared</th><th>Linked clones</th><th>Content</th><th style="width:28%">Usage</th></tr>` +
    storage.map((s) => `<tr>
      <td><span class="dot ${s.status === "available" ? "on" : "off"}"></span><b>${esc(s.id)}</b></td>
      <td>${esc(s.node)}</td><td>${esc(s.type)}</td>
      <td>${s.shared ? '<span class="tag yes">shared</span>' : '<span class="tag">local</span>'}</td>
      <td>${CLONE_OK.has(s.type) ? '<span class="tag yes">yes</span>' : '<span class="tag no">full clones only</span>'}</td>
      <td>${s.content.split(",").filter(Boolean).map((c) => `<span class="tag">${esc(c)}</span>`).join("")}</td>
      <td>${s.total ? meter("", s.used, s.total) : "-"}</td></tr>`).join("");

  $("#network-table").innerHTML = `<tr><th>Bridge</th><th>Node</th><th>Address</th><th>Ports</th><th>State</th><th>Comment</th></tr>` +
    (networks.map((b) => `<tr><td><b>${esc(b.iface)}</b></td><td>${esc(b.node)}</td><td>${esc(b.cidr) || "-"}</td>
      <td>${esc(b.ports) || "-"}</td><td><span class="dot ${b.active ? "on" : "off"}"></span>${b.active ? "active" : "down"}</td><td>${esc(b.comments)}</td></tr>`).join("") ||
      `<tr><td colspan="6" class="empty">No bridges found.</td></tr>`);

  $("#guests-table").innerHTML = `<tr><th>ID</th><th>Name</th><th>Type</th><th>Node</th><th>Status</th><th>CPU</th><th>Memory</th><th>Disk R / W</th><th>Net in / out</th></tr>` +
    (guests.map((g) => `<tr><td class="num">${g.vmid}</td><td><b>${esc(g.name)}</b></td>
      <td>${g.template ? '<span class="tag">template</span>' : g.type === "qemu" ? "VM" : "LXC"}</td><td>${esc(g.node)}</td>
      <td><span class="dot ${g.status === "running" ? "on" : ""}"></span>${esc(g.status)}</td>
      <td class="num">${g.status === "running" ? (g.cpu * 100).toFixed(1) + "%" : "-"}</td>
      <td class="num">${bytes(g.mem)} / ${bytes(g.maxmem)}</td>
      <td class="num">${bytes(g.diskread)} / ${bytes(g.diskwrite)}</td>
      <td class="num">${bytes(g.netin)} / ${bytes(g.netout)}</td></tr>`).join("") ||
      `<tr><td colspan="9" class="empty">No VMs or containers yet.</td></tr>`);
}

const opts = (el, items, keep = true) => {
  const cur = el.value;
  el.innerHTML = items.map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join("");
  if (keep && items.some(([v]) => String(v) === cur)) el.value = cur;
};

function renderDeploy() {
  const imgStores = data.storage.filter((s) => s.content.includes("images") && s.status === "available");
  const storeLabel = (s) => `${s.id} (${s.type}, ${bytes(s.total - s.used)} free)`;
  // Prefer the roomiest storage that supports linked clones.
  const sorted = [...imgStores].sort((a, b) => (CLONE_OK.has(b.type) - CLONE_OK.has(a.type)) || (b.total - b.used) - (a.total - a.used));
  const uniq = [...new Map(sorted.map((s) => [s.id, s])).values()];
  document.querySelectorAll(".f-node").forEach((el) => opts(el, data.nodes.map((n) => [n.name, n.name])));
  document.querySelectorAll(".f-storage").forEach((el) => opts(el, uniq.map((s) => [s.id, storeLabel(s)])));
  document.querySelectorAll(".f-bridge").forEach((el) => opts(el, [...new Set(data.networks.map((b) => b.iface))].map((b) => [b, b])));
  opts($("#f-bstorage"), [["", "Same as template (linked clones)"], ...uniq.map((s) => [s.id, `${storeLabel(s)}: full clones if different`])]);
  opts($("#f-image"), Object.entries(dep.images));
  const ready = dep.templates.filter((t) => t.status === "ready" && t.exists);
  opts($("#f-template"), ready.map((t) => [t.vmid, `${t.vmid} · ${t.name}`]));

  $("#templates-table").innerHTML = `<tr><th>ID</th><th>Name</th><th>Node</th><th>Status</th><th></th></tr>` +
    (dep.templates.map((t) => `<tr><td class="num">${t.vmid}</td><td><b>${esc(t.name)}</b>${t.external ? ' <span class="tag">existing</span>' : ""}</td>
      <td>${esc(t.node)}</td>
      <td><span class="status-${esc(t.status)}">${esc(t.status)}</span>${t.step ? ` · ${esc(t.step)}` : ""}${t.error ? `<div class="errs">${esc(t.error)}</div>` : ""}</td>
      <td>${t.external || t.status === "building" ? "" : `<button class="btn small danger" data-del-tmpl="${t.vmid}">Delete</button>`}</td></tr>`).join("") ||
      `<tr><td colspan="5" class="empty">No templates yet. Build one below.</td></tr>`);

  $("#batches").innerHTML = dep.batches.map((b) => {
    const c = b.counts, done = (c.running || 0) + (c.created || 0) + (c.stopped || 0), failed = c.failed || 0;
    const busy = b.status === "deploying" || b.status === "deleting";
    const parts = Object.entries(c).map(([k, v]) => `${v} ${k}`).join(" · ");
    return `<div class="card batch"><div class="batch-head">
      <div><b>${esc(b.prefix)}-${esc(b.id)}</b> <span class="status-${esc(b.status)}">${esc(b.status)}</span>
        <div class="sub">${b.count} desktops · IDs ${b.first}-${b.last} · from ${esc(b.template_name || b.template)} · ${b.full ? "full" : "linked"} clones on ${esc(b.storage)}${b.finished ? ` · deployed in ${Math.round(b.finished - b.created)}s` : ""}</div></div>
      <button class="btn small danger" data-del-batch="${esc(b.id)}" ${busy ? "disabled" : ""}>Delete batch</button></div>
      ${meter(parts, done, b.count, `${done} of ${b.count} ready${failed ? `, ${failed} failed` : ""}`)}
      ${b.errors.length ? `<div class="errs">${esc(b.errors.join("\n"))}</div>` : ""}</div>`;
  }).join("") || `<div class="empty">No batches yet.</div>`;
}

async function api(method, url, body) {
  const r = await fetch(url, { method, headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail || r.statusText));
  return j;
}
const fail = (e) => { const err = $("#error"); err.textContent = e.message; err.classList.remove("hidden"); };
const formData = (form) => {
  const o = {};
  for (const el of form.elements) {
    if (!el.name) continue;
    if (el.type === "checkbox") o[el.name] = el.checked;
    else if (el.value !== "") o[el.name] = el.type === "number" ? Number(el.value) : el.value;
  }
  return o;
};

async function load() {
  const err = $("#error"); err.classList.add("hidden");
  try {
    const h = await (await fetch("/api/health")).json();
    $("#foot").textContent = `vdi-tool ${h.version}` + (h.pve_version ? ` · PVE ${h.pve_version}` : "");
    const conn = $("#conn");
    if (!h.configured) { conn.textContent = "not configured"; conn.className = "pill bad"; throw new Error("No Proxmox API token configured. Re-run the installer on the Proxmox host."); }
    conn.textContent = h.connected ? `connected · ${h.pve_host}` : "disconnected";
    conn.className = `pill ${h.connected ? "ok" : "bad"}`;
    const r = await fetch("/api/overview");
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    data = await r.json();
    dep = await api("GET", "/api/deploy");
    render();
    renderDeploy();
  } catch (e) { err.textContent = e.message; err.classList.remove("hidden"); }
}

document.querySelectorAll("nav a[data-view]").forEach((a) => a.addEventListener("click", () => show(a.dataset.view)));
window.addEventListener("hashchange", () => show(location.hash.slice(1)));
$("#refresh").addEventListener("click", load);
show(location.hash.slice(1));
load();
setInterval(load, 5000);
