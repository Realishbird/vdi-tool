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
const TITLES = { overview: "Overview", storage: "Storage", network: "Network", guests: "VMs & containers" };

let data = null;

function show(view) {
  if (!TITLES[view]) view = "overview";
  document.querySelectorAll(".view").forEach((v) => v.classList.add("hidden"));
  $(`#view-${view}`).classList.remove("hidden");
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
    render();
  } catch (e) { err.textContent = e.message; err.classList.remove("hidden"); }
}

document.querySelectorAll("nav a[data-view]").forEach((a) => a.addEventListener("click", () => show(a.dataset.view)));
window.addEventListener("hashchange", () => show(location.hash.slice(1)));
$("#refresh").addEventListener("click", load);
show(location.hash.slice(1));
load();
setInterval(load, 15000);
