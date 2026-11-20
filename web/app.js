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
const meter = (label, used, total, text, progress = false) => {
  const p = pct(used, total); const cls = progress ? (p >= 100 ? "ok" : "") : p > 90 ? "bad" : p > 75 ? "warn" : "";
  return `<div class="meter"><div class="meter-top"><span>${label}</span><span>${text ?? `${bytes(used)} / ${bytes(total)}`} · ${p}%</span></div><div class="bar ${cls}"><span style="width:${p}%"></span></div></div>`;
};
const CLONE_OK = new Set(["lvmthin", "zfspool", "rbd", "nfs", "dir", "cifs", "glusterfs", "cephfs"]);
const TITLES = { overview: "Overview", storage: "Storage", network: "Network", guests: "VMs & containers", deploy: "Deploy desktops", bench: "Benchmarks" };

let data = null, dep = null, bench = null, selRun = null, runDetail = null;
const cmp = new Set();  // runs ticked for a comparison report

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
  opts($("#f-bstorage"), [["", "Same as template"], ...uniq.map((s) => [s.id, `${storeLabel(s)}: full clones if different`])]);
  $("#store-fill").textContent = uniq.map((s) => `${s.id}: ${pct(s.used, s.total)}% full, ${bytes(s.total - s.used)} free`).join(" · ") + ". Fills are refused past 90%.";
  opts($("#f-image"), Object.entries(dep.images));
  const isoName = (i) => i.volid.split("/").pop(), isVirtio = (i) => /virtio/i.test(i.volid);
  opts($("#f-winiso"), dep.isos.filter((i) => !isVirtio(i)).map((i) => [i.volid, `${isoName(i)} (${bytes(i.size)})`]));
  opts($("#f-virtioiso"), dep.isos.filter(isVirtio).map((i) => [i.volid, `${isoName(i)} (${bytes(i.size)})`]));
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
        <div class="sub">${b.count} desktops · IDs ${b.first}-${b.last} · from ${esc(b.template_name || b.template)} · ${b.full ? "full" : "linked"} clones on ${esc(b.storage)} · ${b.custom_password ? "own password" : "template's password"}${b.fill_gb ? ` · fills ${b.fill_gb} GB each` : ""}${b.finished ? ` · deployed in ${Math.round(b.finished - b.created)}s` : ""}</div></div>
      <div>${b.os === "windows" ? `<button class="btn small" data-name-batch="${esc(b.id)}" ${busy ? "disabled" : ""} title="Give every Windows desktop its own computer name">Fix names</button> ` : ""}<button class="btn small" data-pw-batch="${esc(b.id)}" ${busy ? "disabled" : ""}>Set password</button>
      <button class="btn small danger" data-del-batch="${esc(b.id)}" ${busy ? "disabled" : ""}>Delete batch</button></div></div>
      ${meter(parts, done, b.count, `${done} of ${b.count} ready${failed ? `, ${failed} failed` : ""}`, true)}
      ${b.errors.length ? `<div class="errs">${esc(b.errors.join("\n"))}</div>` : ""}</div>`;
  }).join("") || `<div class="empty">No batches yet.</div>`;
}


// ------------------------------------------------------------ benchmarks
const C1 = "#3987e5", C2 = "#d95926";  // validated series colours for the dark surface
const fmt = (v) => (v >= 100 ? Math.round(v) : v >= 10 ? v.toFixed(1) : v.toFixed(2)).toString();
const niceMax = (m) => { if (m <= 0) return 1; const p = 10 ** Math.floor(Math.log10(m)); return [1, 2, 5, 10].map((k) => k * p).find((k) => k >= m); };

// One measure per chart, shared x axis in seconds. series: [{name, color, points: [[t, v], ...]}]
function chart(title, unit, series) {
  series = series.filter((s) => s.points.length);
  const head = `<h3>${esc(title)} <span class="hint">(${esc(unit)})</span></h3>`;
  if (!series.length || series[0].points.length < 2) return `<div class="card chart">${head}<div class="nodata">Not enough data yet.</div></div>`;
  const W = 520, H = 190, L = 44, R = 10, T = 8, B = 24;
  const ts = series[0].points.map((p) => p[0]), t0 = ts[0], t1 = ts[ts.length - 1] || 1;
  const max = niceMax(Math.max(...series.flatMap((s) => s.points.map((p) => p[1]))));
  const x = (t) => L + ((t - t0) / (t1 - t0 || 1)) * (W - L - R), y = (v) => T + (1 - v / max) * (H - T - B);
  const grid = [0, 0.5, 1].map((f) => `<line class="grid" x1="${L}" x2="${W - R}" y1="${y(max * f)}" y2="${y(max * f)}"/><text class="axis" x="${L - 6}" y="${y(max * f) + 4}" text-anchor="end">${fmt(max * f)}</text>`).join("");
  const xl = [t0, (t0 + t1) / 2, t1].map((t, i) => `<text class="axis" x="${x(t)}" y="${H - 6}" text-anchor="${["start", "middle", "end"][i]}">${Math.round(t)}s</text>`).join("");
  const lines = series.map((s) => `<polyline fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round" points="${s.points.map((p) => `${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join(" ")}"/>`).join("");
  const legend = series.length > 1 ? series.map((s) => `<span><i style="background:${s.color}"></i>${esc(s.name)}</span>`).join("") : "";
  const payload = esc(JSON.stringify({ series, L, R, W, t0, t1, unit }));
  return `<div class="card chart" data-chart="${payload}">${head}<div class="legend">${legend}</div>
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(title)}">${grid}${xl}${lines}<line class="cross" y1="${T}" y2="${H - B}" style="display:none"/></svg><div class="tip" style="display:none"></div></div>`;
}

document.addEventListener("mousemove", (e) => {
  const el = e.target.closest?.(".chart[data-chart]");
  document.querySelectorAll(".chart .tip").forEach((t) => { if (!el || t.parentNode !== el) { t.style.display = "none"; t.parentNode.querySelector(".cross").style.display = "none"; } });
  if (!el) return;
  const c = JSON.parse(el.dataset.chart), svg = el.querySelector("svg"), box = svg.getBoundingClientRect();
  const px = ((e.clientX - box.left) / box.width) * c.W;
  const t = c.t0 + ((px - c.L) / (c.W - c.L - c.R)) * (c.t1 - c.t0);
  const pts = c.series[0].points; let i = 0;
  pts.forEach((p, k) => { if (Math.abs(p[0] - t) < Math.abs(pts[i][0] - t)) i = k; });
  const cx = c.L + ((pts[i][0] - c.t0) / (c.t1 - c.t0 || 1)) * (c.W - c.L - c.R);
  const cross = el.querySelector(".cross"); cross.setAttribute("x1", cx); cross.setAttribute("x2", cx); cross.style.display = "";
  const tip = el.querySelector(".tip");
  tip.innerHTML = `<b>${Math.round(pts[i][0])}s</b><br>` + c.series.map((s) => `<i style="background:${s.color}"></i>${esc(s.name)}: <b>${s.points[i] ? fmt(s.points[i][1]) : "-"}</b> ${esc(c.unit)}`).join("<br>");
  tip.style.display = "";
  const er = el.getBoundingClientRect(), left = (cx / c.W) * box.width + (box.left - er.left);
  tip.style.left = `${Math.min(left + 12, er.width - tip.offsetWidth - 8)}px`; tip.style.top = `${box.top - er.top + 6}px`;
});

const SUMMARY_LABELS = {
  desktops: "Desktops", completed: "Completed", failed: "Failed", mean_ms: "Avg action time (ms)", p95_ms: "Worst p95 (ms)",
  worst_desktop_ms: "Slowest desktop avg (ms)", actions: "Actions done", errors: "Action errors", total_iops: "Total IOPS",
  iops_per_desktop: "IOPS per desktop", total_mbps: "Throughput (MB/s)", total_gbit: "Throughput (Gbit/s)", read_lat_ms: "Read latency (ms)", write_lat_ms: "Write latency (ms)", worst_p99_ms: "Worst p99 latency (ms)",
  total_mb_per_s: "Total hashed (MB/s)", mb_per_s_per_desktop: "Per desktop (MB/s)", mean_boot_s: "Avg boot (s)", p95_boot_s: "p95 boot (s)",
  all_up_s: "All desktops up (s)", written_gb: "Written this run (GB)", filled_gb_per_desktop: "Fill per desktop (GB)",
  total_write_mb_per_s: "Total write speed (MB/s)", stopped_early: "Stopped early", peak_host_cpu: "Peak host CPU (%)",
};
const resultText = (profile, r) => !r ? "-" :
  profile === "fio" ? `${r.iops.toLocaleString()} IOPS · ${r.mbps ?? "-"} MB/s · read ${r.read.lat_ms} ms · write ${r.write.lat_ms} ms` :
  profile === "office" ? `${r.mean_ms} ms avg · ${r.p95_ms} ms p95 · ${r.actions} actions` :
  profile === "cpu" ? `${r.mb_per_s} MB/s on ${r.cores} cores` :
  profile === "fill" ? `${r.written_gb} GB written at ${r.mb_per_s} MB/s · ${r.total_gb} GB held${r.stopped ? ` · ${r.stopped}` : ""}` : `${r.boot_s} s to boot`;

function renderBench() {
  opts($("#f-profile"), Object.entries(bench.profiles));
  $("#custom-profiles").innerHTML = bench.custom.length ? `<table class="table" style="margin-top:14px"><tr><th>Your workloads</th><th></th></tr>` +
    bench.custom.map((id) => `<tr><td>${esc(bench.profiles[id])}</td><td><button class="btn small danger" data-del-prof="${esc(id)}">Delete</button></td></tr>`).join("") + "</table>" : "";
  const readyB = dep.batches.filter((b) => b.status === "ready" || b.status === "partial");
  opts($("#f-rbatch"), [...readyB.map((b) => [b.id, `${b.prefix}-${b.id} · ${b.count} desktops on ${b.storage}`]),
    ...(readyB.length > 1 ? [["__all__", `All ${readyB.length} batches at once · ${readyB.reduce((n, b) => n + b.count, 0)} desktops on ${[...new Set(readyB.map((b) => b.storage))].join(", ")}`]] : [])]);
  $("#runs").innerHTML = bench.runs.map((r) => {
    const s = r.summary || {}, c = r.counts, fin = (c.done || 0) + (c.failed || 0), total = Object.values(c).reduce((a, b) => a + b, 0);
    return `<div class="card run ${r.id === selRun ? "selected" : ""}" data-run="${esc(r.id)}"><div class="batch-head">
      <div><b>${esc(r.profile_name || bench.profiles[r.profile] || r.profile)}</b> <span class="status-${esc(r.status)}">${esc(r.status)}</span>${r.validity === "invalid" && r.status === "done" ? ` <span class="pill bad" title="Open the report to see why">not valid</span>` : ""}${(r.tags || []).map((t) => ` <span class="pill">${esc(t)}</span>`).join("")}
        <div class="sub">${esc(r.batch_name)} · ${total} desktops${r.clone ? ` (${esc(r.clone)} clones)` : ""} on ${esc(r.storage)}${r.series ? " · density step" : ""} · ${new Date(r.created * 1000).toLocaleString()}${r.kind === "bootstorm" ? "" : ` · ${r.duration}s${r.ramp ? `, ${r.ramp}s ramp` : ""}${["fio", "fill"].includes(r.kind) ? `, ${r.size_gb} GB each` : ""}`}</div></div>
      <div class="run-actions"><label class="hint"><input type="checkbox" data-cmp="${esc(r.id)}" ${cmp.has(r.id) ? "checked" : ""}> Compare</label>
        ${r.series ? `<a class="btn small" href="/report?series=${esc(r.series)}" target="_blank">Density report</a>` : ""}
        <a class="btn small" href="/report?runs=${esc(r.id)}" target="_blank">Report</a>
        <button class="btn small danger" data-del-run="${esc(r.id)}" ${r.status === "running" ? "disabled" : ""}>Delete</button></div></div>
      ${r.status === "running" ? meter(Object.entries(c).map(([k, v]) => `${v} ${k}`).join(" · "), fin, total, `${fin} of ${total} finished`, true) : ""}
      ${r.error ? `<div class="errs">${esc(r.error)}</div>` : ""}
      ${s.completed !== undefined && r.id !== selRun ? `<div class="sub" style="margin-top:8px">${Object.entries(s).filter(([k]) => SUMMARY_LABELS[k] && !["desktops"].includes(k)).slice(0, 5).map(([k, v]) => `${SUMMARY_LABELS[k]}: <b>${v}</b>`).join(" · ")}</div>` : ""}</div>`;
  }).join("") || `<div class="empty">No runs yet. Deploy a batch, then start a run.</div>`;
  [...cmp].forEach((id) => { if (!bench.runs.some((r) => r.id === id)) cmp.delete(id); });
  const order = bench.runs.filter((r) => cmp.has(r.id)).reverse().map((r) => r.id);  // oldest first = baseline
  $("#run-tools").innerHTML = bench.runs.length ? `<a class="btn small ${order.length < 2 ? "off" : "primary"}" ${order.length < 2 ? "" : `href="/report?runs=${order.join(",")}" target="_blank"`}>Compare ${order.length || ""} selected runs</a>
    <a class="btn small" href="/api/runs.csv">All runs as CSV</a><span class="hint">Tick Compare on two or more runs for a side by side report. The oldest ticked run is the baseline.</span>` : "";

  // Side-by-side comparison: one table per workload, one row per finished run.
  const cols = { office: ["mean_ms", "p95_ms", "worst_desktop_ms"], fio: ["total_iops", "total_mbps", "total_gbit", "read_lat_ms", "write_lat_ms", "worst_p99_ms"],
    cpu: ["total_mb_per_s", "mb_per_s_per_desktop"], fill: ["written_gb", "filled_gb_per_desktop", "total_write_mb_per_s"], bootstorm: ["mean_boot_s", "p95_boot_s", "all_up_s"] };
  $("#compare").innerHTML = [...new Set(bench.runs.map((r) => r.profile))].map((p) => {
    const done = bench.runs.filter((r) => r.profile === p && r.status === "done");
    const keys = done.length ? cols[done[0].kind] : null;
    if (done.length < 2 || !keys) return "";
    const all = [...keys, "peak_host_cpu"];
    return `<h2>Compare: ${esc(done[0].profile_name || p)}</h2><table class="table"><tr><th>When</th><th>Batch</th><th>Storage</th><th>Desktops</th>${all.map((k) => `<th>${SUMMARY_LABELS[k]}</th>`).join("")}</tr>` +
      done.map((r) => `<tr><td>${new Date(r.created * 1000).toLocaleString()}</td><td>${esc(r.batch_name)}</td><td><b>${esc(r.storage)}</b></td><td class="num">${r.summary.completed}</td>${all.map((k) => `<td class="num">${(r.summary[k] ?? "-").toLocaleString()}</td>`).join("")}</tr>`).join("") + "</table>";
  }).join("");

  const d = runDetail;
  if (!d || d.id !== selRun) { $("#run-detail").innerHTML = ""; return; }
  const smp = d.samples, pts = (k) => smp.filter((s) => s[k] !== undefined).map((s) => [s.t, s[k]]);
  const lat = Object.entries(d.latency).map(([t, [sum, n]]) => [Number(t), n ? sum / n : 0]).sort((a, b) => a[0] - b[0]);
  const charts = [
    d.kind === "office" ? chart("Action response time", "ms", [{ name: "Avg across desktops", color: C1, points: lat }]) : "",
    chart("Host CPU", "%", [{ name: "Host CPU", color: C1, points: pts("host_cpu") }]),
    chart("Desktop disk throughput", "MB/s", [{ name: "Read", color: C1, points: pts("diskread") }, { name: "Write", color: C2, points: pts("diskwrite") }]),
    chart("Desktop network", "MB/s", [{ name: "In", color: C1, points: pts("netin") }, { name: "Out", color: C2, points: pts("netout") }]),
    chart("Desktop vCPUs busy", "cores", [{ name: "vCPUs busy", color: C1, points: pts("vm_cpu") }]),
  ].join("");
  const tiles = Object.entries(d.summary || {}).filter(([k]) => SUMMARY_LABELS[k]).map(([k, v]) => `<div class="tile"><div class="label">${SUMMARY_LABELS[k]}</div><div class="value">${v.toLocaleString()}</div></div>`).join("");
  const rows = Object.values(d.vms).map((vm) => `<tr><td class="num">${vm.vmid}</td><td><b>${esc(vm.name)}</b></td><td>${esc(vm.node)}</td>
    <td><span class="status-${vm.status === "done" ? "done" : vm.status === "failed" ? "failed" : "running"}">${esc(vm.status)}</span></td>
    <td>${vm.error ? `<span class="errs">${esc(vm.error)}</span>` : esc(resultText(d.kind, vm.result))}</td>
    <td class="num">${vm.io ? `${bytes(vm.io.diskread)} / ${bytes(vm.io.diskwrite)}` : "-"}</td>
    <td class="num">${vm.io ? `${bytes(vm.io.netin)} / ${bytes(vm.io.netout)}` : "-"}</td></tr>`).join("");
  if (document.activeElement?.closest?.("#note-form")) return;  // do not wipe a note while it is being typed
  $("#run-detail").innerHTML = `<h2>Run ${esc(d.id)}: ${esc(d.profile_name || d.profile)}</h2>
    <form id="note-form" class="form note-form" data-rid="${esc(d.id)}"><label>Tags (comma separated, e.g. array model, protocol)<input name="tags" value="${esc((d.tags || []).join(", "))}"></label>
      <label>Notes for the report<input name="note" value="${esc(d.note || "")}"></label>
      <div class="form-actions"><button class="btn">Save</button><a class="btn primary" href="/report?runs=${esc(d.id)}" target="_blank">Open report</a>
      <a class="btn" href="/api/runs/${esc(d.id)}/desktops.csv">Desktops CSV</a></div></form>
    ${tiles ? `<div class="tiles">${tiles}</div>` : ""}<div class="charts">${charts}</div>
    <h2>Per desktop</h2><table class="table"><tr><th>ID</th><th>Name</th><th>Node</th><th>Status</th><th>Result</th><th>Disk read / written</th><th>Net in / out</th></tr>${rows}</table>`;
}

async function api(method, url, body) {
  const r = await fetch(url, { method, headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined });
  if (r.status === 401 && url !== "/api/login") { location.href = "/login"; return new Promise(() => {}); }
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
    const h = await api("GET", "/api/health");
    $("#foot").textContent = `vdi-tool ${h.version}` + (h.pve_version ? ` · PVE ${h.pve_version}` : "");
    const conn = $("#conn");
    if (!h.configured) { conn.textContent = "not configured"; conn.className = "pill bad"; throw new Error("No Proxmox API token configured. Re-run the installer on the Proxmox host."); }
    conn.textContent = h.connected ? `connected · ${h.pve_host}` : "disconnected";
    conn.className = `pill ${h.connected ? "ok" : "bad"}`;
    const r = await fetch("/api/overview");
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    data = await r.json();
    dep = await api("GET", "/api/deploy");
    bench = await api("GET", "/api/runs");
    if (!selRun || !bench.runs.some((r) => r.id === selRun)) selRun = (bench.runs.find((r) => r.status === "running") || bench.runs[0] || {}).id || null;
    if (selRun && (!runDetail || runDetail.id !== selRun || runDetail.status === "running")) runDetail = await api("GET", `/api/runs/${selRun}`);
    render();
    renderDeploy();
    if (!document.querySelector(".chart .tip:not([style*=none])")) renderBench();
  } catch (e) { err.textContent = e.message; err.classList.remove("hidden"); }
}

// Disable the submit button while a request is in flight so a double click cannot start two jobs.
const submitOnce = (form, fn) => form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = form.querySelector("button"), label = btn.textContent;
  if (btn.disabled) { return; }
  btn.disabled = true; btn.textContent = "Starting…";
  try { await fn(form); await load(); } catch (x) { fail(x); }
  btn.disabled = false; btn.textContent = label;
});
submitOnce($("#tmpl-form"), async (f) => { await api("POST", "/api/templates", formData(f)); f.closest("details").open = false; f.password.value = ""; });
submitOnce($("#win-form"), async (f) => { await api("POST", "/api/templates", formData(f)); f.closest("details").open = false; f.password.value = ""; });
submitOnce($("#batch-form"), async (f) => { await api("POST", "/api/batches", formData(f)); f.password.value = ""; });
submitOnce($("#prof-form"), async (f) => { await api("POST", "/api/profiles", formData(f)); f.name.value = ""; });
submitOnce($("#run-form"), async (f) => {
  const body = formData(f);
  if (body.batch === "__all__") {  // one run across every ready batch, started at the same moment
    const ids = dep.batches.filter((b) => b.status === "ready" || b.status === "partial").map((b) => b.id);
    body.batch = ids[0]; body.batches = ids.slice(1);
  }
  const r = await api("POST", "/api/runs", body); if (r.id) selRun = r.id; runDetail = null;
});
document.addEventListener("click", async (e) => {
  const t = e.target.dataset;
  const card = e.target.closest(".run");
  if (t.cmp) { e.target.checked ? cmp.add(t.cmp) : cmp.delete(t.cmp); renderBench(); return; }
  if (e.target.closest(".run-actions a, .run-actions label")) return;
  if (card && !t.delRun) { selRun = card.dataset.run; runDetail = null; load(); return; }
  try {
    if (t.delProf) { await api("DELETE", `/api/profiles/${t.delProf}`); load(); }
    if (t.delRun && window.confirm("Delete this run and its results?")) { await api("DELETE", `/api/runs/${t.delRun}`); load(); }
    if (t.nameBatch && window.confirm("Rename every Windows desktop in this batch to match its VM name? Each renamed desktop restarts once.")) {
      const r = await api("POST", `/api/batches/${t.nameBatch}/rename`);
      if (r.failed) throw new Error(`Renamed ${r.ok} desktops, ${r.failed} failed. ${r.errors.join(" | ")}`);
      window.alert(`${r.ok} desktops are being renamed and will restart.`);
    }
    if (t.pwBatch) {
      const pw = window.prompt('New password for user "vdi" on every running desktop in this batch (5 characters or more):');
      if (pw) { const r = await api("POST", `/api/batches/${t.pwBatch}/password`, { password: pw }); if (r.failed) throw new Error(`Password set on ${r.ok} desktops, ${r.failed} failed. ${r.errors.join(" | ")}`); window.alert(`Password set on ${r.ok} desktops.`); load(); }
    }
    if (t.delBatch && window.confirm(`Stop and permanently delete every desktop in batch ${t.delBatch}?`)) { await api("DELETE", `/api/batches/${t.delBatch}`); load(); }
    if (t.delTmpl && window.confirm(`Permanently delete template ${t.delTmpl}? Linked clones made from it must be deleted first.`)) { await api("DELETE", `/api/templates/${t.delTmpl}`); load(); }
  } catch (x) { fail(x); }
});
document.addEventListener("submit", async (e) => {
  if (e.target.id !== "note-form") return;
  e.preventDefault();
  const f = e.target;
  try {
    await api("PATCH", `/api/runs/${f.dataset.rid}`, { note: f.note.value, tags: f.tags.value.split(",").map((x) => x.trim()).filter(Boolean) });
    document.activeElement?.blur(); runDetail = null; load();
  } catch (x) { fail(x); }
});
$("#update").addEventListener("click", async () => {
  if (!window.confirm("Update vdi-tool to the latest revision? The GUI restarts for a few seconds.")) return;
  try { await api("POST", "/api/update"); $("#update").textContent = "Updating…"; setTimeout(() => location.reload(), 20000); } catch (x) { fail(x); }
});
$("#f-fill").addEventListener("change", (e) => $("#f-fillgb").classList.toggle("hidden", !e.target.checked));
$("#grafana").href = `http://${location.hostname}:3000/d/vdi-overview`;
document.querySelectorAll("nav a[data-view]").forEach((a) => a.addEventListener("click", () => show(a.dataset.view)));
window.addEventListener("hashchange", () => show(location.hash.slice(1)));
$("#refresh").addEventListener("click", load);
show(location.hash.slice(1));
load();
setInterval(load, 5000);

// Login for the GUI: off until a password is set here.
async function authPanel(open) {
  const a = await api("GET", "/api/auth");
  const btn = $("#auth-btn"), p = $("#auth-panel");
  btn.textContent = a.enabled ? "Login: on" : "Login: off";
  if (open === undefined) return;
  p.classList.toggle("hidden", !open);
  if (!open) return;
  p.innerHTML = `<h2>${a.enabled ? "Login is on" : "Login is off"}</h2>
    <div class="hint">${a.enabled ? "Everyone needs the admin password to open this tool. Desktops still report without it. Scripts can send the key in /var/lib/vdi-tool/api-key as a Bearer token."
      : "Anyone who can reach this address can use the tool. Set an admin password to require a login."}</div>
    <form id="auth-form" class="form"><label>${a.enabled ? "New password" : "Admin password"} (8 characters or more)
      <input name="password" type="password" minlength="8" autocomplete="new-password" required></label>
    <div class="form-actions"><button class="btn primary">${a.enabled ? "Change password" : "Turn login on"}</button>
    ${a.enabled ? '<button type="button" class="btn" id="auth-out">Log out</button><button type="button" class="btn danger" id="auth-off">Turn login off</button>' : ""}
    <button type="button" class="btn" id="auth-close">Close</button></div></form>`;
  $("#auth-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try { await api("POST", "/api/password", { password: e.target.password.value }); await authPanel(false); } catch (x) { fail(x); }
  });
  $("#auth-close").addEventListener("click", () => authPanel(false));
  if (a.enabled) {
    $("#auth-out").addEventListener("click", async () => { await api("POST", "/api/logout"); location.href = "/login"; });
    $("#auth-off").addEventListener("click", async () => { try { await api("DELETE", "/api/password"); await authPanel(false); } catch (x) { fail(x); } });
  }
}
$("#auth-btn").addEventListener("click", () => authPanel($("#auth-panel").classList.contains("hidden")));
authPanel();

// EUC Score package: uploaded by the person, never downloaded by the tool.
async function eucState() {
  const e = await api("GET", "/api/euc");
  $("#euc-state").textContent = e.installed ? `Uploaded: ${(e.size / 1e6).toFixed(0)} MB on ${new Date(e.uploaded * 1000).toLocaleString()}.` : "Not uploaded yet.";
  $("#euc-rm").classList.toggle("hidden", !e.installed);
  $("#f-euc").disabled = !e.installed;
  if (!e.installed) $("#f-euc").checked = false;
}
$("#euc-up").addEventListener("click", async () => {
  const f = $("#euc-file").files[0];
  if (!f) return fail(new Error("Pick the EUC Score ZIP first."));
  $("#euc-state").textContent = "Uploading…";
  const r = await fetch("/api/euc/package", { method: "PUT", body: f });
  if (!r.ok) fail(new Error(((await r.json().catch(() => ({}))).detail) || r.statusText));
  eucState();
});
$("#euc-rm").addEventListener("click", async () => { try { await api("DELETE", "/api/euc/package"); eucState(); } catch (x) { fail(x); } });
eucState().catch(() => {});
