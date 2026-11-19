// Printable benchmark report: /report?runs=<id> for one run, /report?runs=<a>,<b>,... to compare (first = baseline).
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (v) => (typeof v === "number" ? v.toLocaleString(undefined, { maximumFractionDigits: 2 }) : esc(v ?? "-"));
const when = (t) => new Date(t * 1000).toLocaleString();
const S1 = "var(--s1)", S2 = "var(--s2)";

// label, and whether a higher value is better (null = neither)
const METRICS = {
  completed: ["Desktops completed", true], failed: ["Desktops failed", false],
  total_iops: ["Total IOPS", true], iops_per_desktop: ["IOPS per desktop", true], total_mbps: ["Throughput (MB/s)", true],
  total_gbit: ["Throughput (Gbit/s)", true], read_lat_ms: ["Avg read latency (ms)", false], write_lat_ms: ["Avg write latency (ms)", false],
  p50_ms: ["Latency p50 (ms)", false], p95_ms: ["Latency p95 (ms)", false], worst_p99_ms: ["Worst desktop p99 (ms)", false],
  max_lat_ms: ["Max latency (ms)", false], mean_ms: ["Avg action time (ms)", false], worst_desktop_ms: ["Slowest desktop avg (ms)", false],
  actions: ["Actions done", true], errors: ["Action errors", false], total_mb_per_s: ["Total hashed (MB/s)", true],
  mb_per_s_per_desktop: ["Hashed per desktop (MB/s)", true], mean_boot_s: ["Avg boot (s)", false], p95_boot_s: ["p95 boot (s)", false],
  all_up_s: ["All desktops up (s)", false], written_gb: ["Written this run (GB)", null], filled_gb_per_desktop: ["Fill per desktop (GB)", null],
  total_write_mb_per_s: ["Total write speed (MB/s)", true], stopped_early: ["Stopped early", false], peak_host_cpu: ["Peak host CPU (%)", null],
};
const HEADLINE = { fio: ["total_iops", "total_mbps", "total_gbit", "p95_ms", "worst_p99_ms"], office: ["mean_ms", "p95_ms"], cpu: ["total_mb_per_s"],
  fill: ["written_gb", "total_write_mb_per_s"], bootstorm: ["mean_boot_s", "all_up_s"] };

const fmtAxis = (v) => (Number.isInteger(v) ? v.toLocaleString() : v >= 100 ? Math.round(v).toLocaleString() : v >= 10 ? v.toFixed(1) : v.toFixed(2));
const niceMax = (m) => { if (m <= 0) return 1; const p = 10 ** Math.floor(Math.log10(m)); return [1, 2, 5, 10].map((k) => k * p).find((k) => k >= m); };

// One measure per chart. The shaded band marks the timed part of the run.
function chart(title, unit, series, band) {
  series = series.filter((s) => s.points.length > 1);
  if (!series.length) return "";
  const W = 480, H = 170, L = 46, R = 8, T = 8, B = 22;
  const t0 = series[0].points[0][0], t1 = series[0].points.at(-1)[0] || 1;
  const max = niceMax(Math.max(...series.flatMap((s) => s.points.map((p) => p[1]))));
  const x = (t) => L + ((t - t0) / (t1 - t0 || 1)) * (W - L - R), y = (v) => T + (1 - v / max) * (H - T - B);
  const shade = band && band[1] > band[0] ? `<rect class="timed" x="${x(Math.max(t0, band[0]))}" y="${T}" width="${Math.max(0, x(Math.min(t1, band[1])) - x(Math.max(t0, band[0])))}" height="${H - T - B}"/>` : "";
  const grid = [0, 0.5, 1].map((f) => `<line class="grid" x1="${L}" x2="${W - R}" y1="${y(max * f)}" y2="${y(max * f)}"/><text class="axis" x="${L - 6}" y="${y(max * f) + 4}" text-anchor="end">${fmtAxis(max * f)}</text>`).join("");
  const xl = [t0, (t0 + t1) / 2, t1].map((t, i) => `<text class="axis" x="${x(t)}" y="${H - 5}" text-anchor="${["start", "middle", "end"][i]}">${Math.round(t)}s</text>`).join("");
  const lines = series.map((s) => `<polyline fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round" points="${s.points.map((p) => `${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join(" ")}"/>`).join("");
  const legend = series.length > 1 ? series.map((s) => `<span><i style="background:${s.color}"></i>${esc(s.name)}</span>`).join("") : "";
  return `<div class="chart"><h3>${esc(title)} <span class="muted">(${esc(unit)})</span></h3><div class="legend">${legend}</div>
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(title)}">${shade}${grid}${xl}${lines}</svg></div>`;
}

const kv = (rows) => `<table class="kv">${rows.filter((r) => r[1] !== undefined && r[1] !== null && r[1] !== "").map(([k, v]) => `<tr><th>${esc(k)}</th><td>${v}</td></tr>`).join("")}</table>`;

function workloadText(r) {
  const f = r.spec?.fio;
  if (!f) return esc(r.profile_name);
  const pat = { randrw: "random read/write", rw: "sequential read/write", randread: "random read", randwrite: "random write", read: "sequential read", write: "sequential write" }[f.rw] || f.rw;
  const parts = [pat, `block size ${f.bssplit || f.bs || "4k"}`, `queue depth ${f.iodepth ?? 16}`, `${f.numjobs ?? 1} job(s) per desktop`];
  if (/rw$/.test(f.rw)) parts.splice(1, 0, `${f.rwmixread ?? 70}% read`);
  if (f.rate_iops) parts.push(`capped at ${f.rate_iops} IOPS per desktop`);
  return `${esc(r.profile_name)}<div class="muted">${esc(parts.join(", "))}</div>`;
}

function setupRows(r) {
  const s = r.setup || {}, e = r.env || {};
  return [
    ["Workload", workloadText(r)],
    ["Started", esc(when(r.created))],
    ["Duration", r.kind === "bootstorm" ? "until every desktop is up" : `${r.duration} s${r.ramp ? `, ${r.ramp} s ramp-up` : ""}`],
    ["Test data per desktop", ["fio", "fill"].includes(r.kind) ? `${r.size_gb} GB` : null],
    ["Desktops", `${Object.keys(r.vms).length} (${esc(r.batch_name)})`],
    ["Clone type", s.clone ? `${esc(s.clone)} clones` : null],
    ["Template", s.template ? `${esc(s.template)} (${esc(s.os)})` : null],
    ["Per desktop", s.cores || s.memory || s.disk_gb ? [s.cores && `${s.cores} vCPU`, s.memory && `${s.memory} MB RAM`, s.disk_gb && `${s.disk_gb} GB disk`].filter(Boolean).join(", ") : null],
    ["Storage", `<b>${esc(r.storage)}</b>${e.storage_type ? ` (${esc(e.storage_type)}${e.storage_target ? `, ${esc(e.storage_target)}` : ""})` : ""}`],
    ["Storage size", e.storage_total_gb ? `${num(e.storage_total_gb)} GB, ${num(e.storage_used_gb)} GB used at start` : null],
  ];
}
const envRows = (r) => { const e = r.env || {}; return [
  ["Host", e.node ? `${esc(e.node)} (${esc(e.pve_host)})` : null], ["Proxmox VE", esc(e.pve_version)], ["Kernel", esc(e.kernel)],
  ["CPU", e.cpu_model ? `${esc(e.cpu_model)}${e.sockets ? `, ${e.sockets} socket(s)` : ""}, ${e.cpus} threads` : null],
  ["Memory", e.memory_gb ? `${num(e.memory_gb)} GB` : null], ["Tool version", esc(e.tool_version)],
]; };

function banner(r) {
  const v = r.validity, cls = v.level;
  const title = { valid: "Result is valid", warning: "Result is usable, with a caveat", invalid: "Do not rely on this result", running: "Run in progress" }[cls];
  const items = [...v.problems, ...v.notes];
  return `<div class="banner ${cls}"><b>${title}</b>${cls === "valid" ? "All desktops completed and the checks passed." : `<ul>${items.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>`}</div>`;
}

const head = (r) => `<p class="sub">Run ${esc(r.id)} · ${esc(when(r.created))} · ${esc(r.storage)}${r.setup?.clone ? ` · ${esc(r.setup.clone)} clones` : ""}</p>
  ${(r.tags || []).length ? `<div class="tags">${r.tags.map((t) => `<span>${esc(t)}</span>`).join("")}</div>` : ""}
  ${r.note ? `<div class="note">${esc(r.note)}</div>` : ""}`;

function resultCells(kind, v) {
  if (!v) return ["-"];
  if (kind === "fio") return [num(v.iops), num(v.mbps), num(v.read.lat_ms), num(v.write.lat_ms), num(Math.max(v.read.p99_ms, v.write.p99_ms))];
  if (kind === "office") return [num(v.mean_ms), num(v.p95_ms), num(v.actions)];
  if (kind === "cpu") return [num(v.mb_per_s), num(v.cores)];
  if (kind === "fill") return [num(v.written_gb), num(v.mb_per_s), num(v.total_gb)];
  return [num(v.boot_s)];
}
const RESULT_HEAD = { fio: ["IOPS", "MB/s", "Read ms", "Write ms", "p99 ms"], office: ["Avg ms", "p95 ms", "Actions"], cpu: ["MB/s", "Cores"],
  fill: ["Written GB", "MB/s", "Held GB"], bootstorm: ["Boot s"] };

function single(r) {
  const s = r.summary || {}, sp = r.spread || {};
  const mf = r.measure_from || 0, band = r.kind === "fio" && r.measure_from !== undefined ? [mf, mf + r.duration] : null;
  const pts = (k) => r.samples.filter((x) => x[k] !== undefined && x[k] !== null).map((x) => [x.t, x[k]]);
  const lat = Object.entries(r.latency || {}).map(([t, [sum, n]]) => [Number(t), n ? sum / n : 0]).sort((a, b) => a[0] - b[0]);
  const headKeys = (HEADLINE[r.kind] || []).filter((k) => s[k] !== undefined);
  const tiles = [...headKeys, ...Object.keys(s).filter((k) => METRICS[k] && !headKeys.includes(k))]
    .map((k) => `<div class="tile ${headKeys.includes(k) ? "head" : ""}"><div class="label">${METRICS[k][0]}</div><div class="value">${num(s[k])}</div></div>`).join("");
  const charts = [
    r.kind === "office" ? chart("Action response time", "ms", [{ name: "Average", color: S1, points: lat }]) : "",
    chart("Desktop disk throughput, as Proxmox saw it", "MB/s", [{ name: "Read", color: S1, points: pts("diskread") }, { name: "Write", color: S2, points: pts("diskwrite") }], band),
    chart("Host CPU", "%", [{ name: "Host CPU", color: S1, points: pts("host_cpu") }], band),
    chart("Desktop vCPUs busy", "cores", [{ name: "vCPUs", color: S1, points: pts("vm_cpu") }], band),
    chart("Desktop network", "MB/s", [{ name: "In", color: S1, points: pts("netin") }, { name: "Out", color: S2, points: pts("netout") }], band),
  ].join("");
  // Outliers: the desktops furthest on the bad side of the median.
  const vms = Object.values(r.vms), ok = vms.filter((v) => v.result && sp.key && v.result[sp.key] !== undefined);
  const bad = (v) => (sp.higher_better ? sp.p50 - v.result[sp.key] : v.result[sp.key] - sp.p50);
  const outliers = new Set(ok.filter((v) => sp.p50 && bad(v) / sp.p50 > 0.25).sort((a, b) => bad(b) - bad(a)).slice(0, 10).map((v) => v.vmid));
  const failed = vms.filter((v) => v.status === "failed");
  const rh = RESULT_HEAD[r.kind] || [];
  const rows = vms.map((v) => `<tr class="${outliers.has(v.vmid) ? "outlier" : ""}"><td class="num">${v.vmid}</td><td>${esc(v.name)}</td><td>${esc(v.status)}${outliers.has(v.vmid) ? " · outlier" : ""}</td>
    ${v.result ? resultCells(r.kind, v.result).map((c) => `<td class="num">${c}</td>`).join("") : `<td colspan="${rh.length}" class="err">${esc(v.error || "")}</td>`}</tr>`).join("");
  document.title = `Report ${r.id}: ${r.profile_name}`;
  $("#exports").innerHTML = `<a class="btn" href="/api/runs/${esc(r.id)}/desktops.csv">Desktops CSV</a><a class="btn" href="/api/runs/${esc(r.id)}" download="vdi-tool-run-${esc(r.id)}.json">Raw JSON</a>`;
  return `<h1>${esc(r.profile_name)}</h1>${head(r)}${banner(r)}
    ${r.verdict ? `<p class="verdict"><b>What limited this run:</b> ${esc(r.verdict)}</p>` : ""}
    <h2>Key results</h2><div class="tiles">${tiles || '<p class="muted">No results.</p>'}</div>
    <div class="two"><div><h2>Test setup</h2>${kv(setupRows(r))}</div><div><h2>Environment</h2>${kv(envRows(r))}
      ${sp.key ? `<h2>Spread across desktops: ${esc(sp.label)}</h2>${kv([["Lowest", num(sp.min)], ["Median", num(sp.p50)], ["95th percentile", num(sp.p95)], ["Highest", num(sp.max)]])}` : ""}</div></div>
    ${charts ? `<h2>Over time</h2>${band ? '<p class="muted">The shaded band is the timed part of the run. Before it, desktops were writing their test files.</p>' : ""}<div class="charts">${charts}</div>` : ""}
    ${failed.length ? `<h2>Failures (${failed.length})</h2><table><tr><th>ID</th><th>Desktop</th><th>What happened</th></tr>${failed.slice(0, 25).map((v) => `<tr><td class="num">${v.vmid}</td><td>${esc(v.name)}</td><td class="err">${esc(v.error || "")}</td></tr>`).join("")}</table>${failed.length > 25 ? `<p class="muted">Showing the first 25. The CSV has all of them.</p>` : ""}` : ""}
    <h2>Per desktop</h2>${outliers.size ? `<p class="muted">Highlighted rows are more than 25% worse than the median desktop.</p>` : ""}
    <table><tr><th class="num">ID</th><th>Desktop</th><th>Status</th>${rh.map((h) => `<th class="num">${h}</th>`).join("")}</tr>${rows}</table>`;
}

function compare(rs) {
  const base = rs[0], q = new URLSearchParams(location.search);
  const [slaKey, slaVal] = (q.get("target") || "").split(":"), sla = METRICS[slaKey] && slaVal !== "" && !isNaN(Number(slaVal)) ? Number(slaVal) : null;
  const label = (r, i) => `${String.fromCharCode(65 + i)}: ${r.storage}${r.setup?.clone ? `, ${r.setup.clone}` : ""}`;
  const keys = Object.keys(METRICS).filter((k) => rs.some((r) => r.summary?.[k] !== undefined));
  const delta = (k, r) => {
    const a = base.summary?.[k], b = r.summary?.[k], hb = METRICS[k][1];
    if (r === base || typeof a !== "number" || typeof b !== "number" || !a) return "";
    const pct = ((b - a) / a) * 100, cls = hb === null || Math.abs(pct) < 1 ? "muted" : (pct > 0) === hb ? "better" : "worse";
    return ` <span class="${cls}">(${pct > 0 ? "+" : ""}${pct.toFixed(Math.abs(pct) < 10 ? 1 : 0)}%)</span>`;
  };
  const passes = (r) => { const v = r.summary?.[slaKey]; return typeof v !== "number" ? null : METRICS[slaKey][1] ? v >= sla : v <= sla; };
  const mixed = new Set(rs.map((r) => r.kind)).size > 1 || new Set(rs.map((r) => JSON.stringify(r.spec))).size > 1;
  const hk = (HEADLINE[base.kind] || []).find((k) => rs.every((r) => typeof r.summary?.[k] === "number"));
  const hmax = hk ? Math.max(...rs.map((r) => r.summary[hk])) || 1 : 1;
  const bars = hk ? `<h2>${METRICS[hk][0]}</h2><div class="bars">${rs.map((r, i) => `<div class="row"><div>${esc(label(r, i))}</div><div class="track"><div class="fill" style="width:${(r.summary[hk] / hmax) * 100}%"></div></div><div class="v">${num(r.summary[hk])}</div></div>`).join("")}</div>` : "";
  const setupKeys = [["Workload", (r) => esc(r.profile_name)], ["Started", (r) => esc(when(r.created))], ["Storage", (r) => `<b>${esc(r.storage)}</b>${r.env?.storage_type ? ` (${esc(r.env.storage_type)})` : ""}`],
    ["Clone type", (r) => esc(r.setup?.clone || "-")], ["Desktops", (r) => Object.keys(r.vms).length], ["Duration", (r) => `${r.duration} s`],
    ["Test data per desktop", (r) => (["fio", "fill"].includes(r.kind) ? `${r.size_gb} GB` : "-")], ["Tags", (r) => esc((r.tags || []).join(", ") || "-")],
    ["Validity", (r) => `<span class="${r.validity.level === "invalid" ? "fail" : r.validity.level === "valid" ? "pass" : ""}">${esc(r.validity.level)}</span>`],
    ["Full report", (r) => `<a class="noprint" href="/report?runs=${esc(r.id)}">open</a><span class="muted"> run ${esc(r.id)}</span>`]];
  document.title = `Comparison of ${rs.length} runs`;
  $("#exports").innerHTML = `<a class="btn" href="/api/runs.csv">All runs CSV</a>`;
  const opts = keys.map((k) => `<option value="${k}" ${k === slaKey ? "selected" : ""}>${METRICS[k][0]}</option>`).join("");
  return `<h1>Benchmark comparison</h1><p class="sub">${rs.length} runs · baseline is A · generated ${esc(new Date().toLocaleString())}</p>
    ${mixed ? `<div class="banner warning"><b>These runs did not use the same workload</b>Differences may come from the workload, not the storage.</div>` : ""}
    ${rs.some((r) => r.validity.level === "invalid") ? `<div class="banner invalid"><b>At least one run is marked invalid</b>See the Validity row and open that run's report for the reason.</div>` : ""}
    ${bars}
    <h2>Results</h2>
    <form class="sla noprint" id="sla">Target: <select name="k">${opts}</select> <input name="v" type="number" step="any" placeholder="value" value="${sla ?? ""}"> <button>Apply</button><span class="muted">Adds a pass or fail row. Lower is better for latency and boot times.</span></form>
    <table><tr><th>Measure</th>${rs.map((r, i) => `<th class="num">${esc(label(r, i))}</th>`).join("")}</tr>
      ${keys.map((k) => `<tr><td>${METRICS[k][0]}</td>${rs.map((r) => `<td class="num">${num(r.summary?.[k])}${delta(k, r)}</td>`).join("")}</tr>`).join("")}
      ${sla !== null ? `<tr><td><b>Target: ${METRICS[slaKey][0]} ${METRICS[slaKey][1] ? "at least" : "at most"} ${num(sla)}</b></td>${rs.map((r) => { const p = passes(r); return `<td class="num ${p === null ? "" : p ? "pass" : "fail"}">${p === null ? "-" : p ? "PASS" : "FAIL"}</td>`; }).join("")}</tr>` : ""}</table>
    <p class="muted">Percentages compare each run with A. Green is better, red is worse.</p>
    <h2>What was tested</h2><table><tr><th></th>${rs.map((r, i) => `<th>${String.fromCharCode(65 + i)}</th>`).join("")}</tr>
      ${setupKeys.map(([k, f]) => `<tr><th>${k}</th>${rs.map((r) => `<td>${f(r)}</td>`).join("")}</tr>`).join("")}</table>
    <h2>What limited each run</h2><table>${rs.map((r, i) => `<tr><th>${String.fromCharCode(65 + i)}</th><td>${esc(r.verdict || "-")}${[...r.validity.problems, ...r.validity.notes].map((p) => `<div class="err">${esc(p)}</div>`).join("")}</td></tr>`).join("")}</table>
    <h2>Environment</h2>${kv(envRows(rs.find((r) => r.env?.pve_version) || base))}`;
}

// Value against number of desktops. An optional dashed line marks the target.
function xy(title, unit, points, target) {
  points = points.filter((p) => typeof p[1] === "number");
  if (points.length < 2) return "";
  const W = 480, H = 190, L = 54, R = 12, T = 10, B = 34;
  const x0 = 0, x1 = points.at(-1)[0], max = niceMax(Math.max(...points.map((p) => p[1]), target ?? 0));
  const x = (v) => L + ((v - x0) / (x1 - x0 || 1)) * (W - L - R), y = (v) => T + (1 - v / max) * (H - T - B);
  const grid = [0, 0.5, 1].map((f) => `<line class="grid" x1="${L}" x2="${W - R}" y1="${y(max * f)}" y2="${y(max * f)}"/><text class="axis" x="${L - 6}" y="${y(max * f) + 4}" text-anchor="end">${fmtAxis(max * f)}</text>`).join("");
  const ticks = points.map((p) => `<text class="axis" x="${x(p[0])}" y="${H - 18}" text-anchor="middle">${p[0]}</text>`).join("");
  const line = `<polyline fill="none" stroke="${S1}" stroke-width="2" points="${points.map((p) => `${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join(" ")}"/>` +
    points.map((p) => `<circle cx="${x(p[0])}" cy="${y(p[1])}" r="3.5" fill="${S1}"/>`).join("");
  const tl = typeof target === "number" ? `<line x1="${L}" x2="${W - R}" y1="${y(target)}" y2="${y(target)}" stroke="${S2}" stroke-width="1.5" stroke-dasharray="5 4"/><text x="${L + 4}" y="${y(target) - 5}" font-size="11" fill="${S2}">target ${num(target)}</text>` : "";
  return `<div class="chart"><h3>${esc(title)}${unit ? ` <span class="muted">(${esc(unit)})</span>` : ""}</h3>
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(title)}">${grid}${ticks}${tl}${line}<text class="axis" x="${(L + W - R) / 2}" y="${H - 3}" text-anchor="middle">desktops running the workload</text></svg></div>`;
}

const DENSITY = { fio: ["total_iops", "p95_ms", "iops_per_desktop"], office: ["actions", "mean_ms", null], cpu: ["total_mb_per_s", null, "mb_per_s_per_desktop"], bootstorm: [null, "all_up_s", "mean_boot_s"] };

function density(rs) {
  const q = new URLSearchParams(location.search), base = rs[0], n = (r) => Object.keys(r.vms).length;
  const [thru, lat, per] = DENSITY[base.kind] || [];
  const defKey = lat || per || thru;
  const [qk, qv] = (q.get("target") || "").split(":"), slaKey = METRICS[qk] ? qk : defKey;
  const sla = METRICS[qk] && qv !== "" && !isNaN(Number(qv)) ? Number(qv) : null;
  const ok = (r) => { const v = r.summary?.[slaKey]; return typeof v !== "number" || r.validity.level === "invalid" ? null : METRICS[slaKey][1] ? v >= sla : v <= sla; };
  // The answer is the largest step that passes with every smaller step passing too.
  let best = null;
  if (sla !== null) for (const r of rs) { if (ok(r)) best = r; else break; }
  const keys = [thru, per, "p50_ms", lat, "worst_p99_ms", "mean_boot_s", "peak_host_cpu"].filter((k, i, a) => k && a.indexOf(k) === i && rs.some((r) => r.summary?.[k] !== undefined));
  const firstFail = sla !== null ? rs.find((r) => ok(r) === false) : null;
  const answer = sla === null
    ? `<div class="banner warning"><b>Set a target to get the answer</b>Pick a measure and a limit below, for example ${esc(METRICS[defKey][0])}, and the report states how many desktops this storage supports.</div>`
    : best
      ? `<div class="banner valid"><b>Supports ${n(best)} desktops${best === rs.at(-1) ? " or more" : ""}</b>${esc(METRICS[slaKey][0])} stayed ${METRICS[slaKey][1] ? "at or above" : "at or below"} ${num(sla)} up to ${n(best)} desktops (measured ${num(best.summary[slaKey])}).${firstFail ? ` At ${n(firstFail)} desktops it was ${num(firstFail.summary?.[slaKey])}.` : " The largest step still passed, so the limit is higher than this test went."}</div>`
      : `<div class="banner invalid"><b>The target was missed at the smallest step</b>${esc(METRICS[slaKey][0])} was ${num(rs[0].summary?.[slaKey])} with ${n(rs[0])} desktops, against a target of ${num(sla)}.</div>`;
  const eff = per && typeof base.summary?.[per] === "number" && base.summary[per] ? (r) => (typeof r.summary?.[per] === "number" ? `${Math.round((r.summary[per] / base.summary[per]) * 100)}%` : "-") : null;
  const opts = Object.keys(METRICS).filter((k) => rs.some((r) => r.summary?.[k] !== undefined)).map((k) => `<option value="${k}" ${k === slaKey ? "selected" : ""}>${METRICS[k][0]}</option>`).join("");
  const charts = [thru && xy(METRICS[thru][0], "", rs.map((r) => [n(r), r.summary?.[thru]]), slaKey === thru ? sla : undefined),
    lat && xy(METRICS[lat][0], "", rs.map((r) => [n(r), r.summary?.[lat]]), slaKey === lat ? sla : undefined),
    per && xy(METRICS[per][0], "", rs.map((r) => [n(r), r.summary?.[per]]), slaKey === per ? sla : undefined),
    xy("Peak host CPU (%)", "", rs.map((r) => [n(r), r.summary?.peak_host_cpu]))].filter(Boolean).join("");
  document.title = `Density test: ${base.profile_name}`;
  $("#exports").innerHTML = `<a class="btn" href="/api/runs.csv">All runs CSV</a>`;
  return `<h1>Density test: ${esc(base.profile_name)}</h1>
    <p class="sub">${rs.length} steps from ${n(base)} to ${n(rs.at(-1))} desktops · ${esc(base.storage)}${base.setup?.clone ? ` · ${esc(base.setup.clone)} clones` : ""} · ${esc(when(base.created))}</p>
    ${answer}
    <form class="sla noprint" id="sla">Target: <select name="k">${opts}</select> <input name="v" type="number" step="any" placeholder="limit" value="${sla ?? ""}"> <button>Apply</button><span class="muted">Lower is better for latency and boot times, higher for IOPS and throughput.</span></form>
    <h2>How results change as desktops are added</h2><div class="charts">${charts}</div>
    <h2>Step by step</h2>
    <table class="tight"><tr><th class="num">Desktops</th><th class="num">Completed</th>${keys.map((k) => `<th class="num">${METRICS[k][0]}</th>`).join("")}${eff ? '<th class="num">Per desktop vs first step</th>' : ""}<th>Validity</th>${sla !== null ? "<th>Target</th>" : ""}<th class="noprint"></th></tr>
      ${rs.map((r) => { const p = sla !== null ? ok(r) : null; return `<tr><td class="num"><b>${n(r)}</b></td><td class="num">${num(r.summary?.completed)}</td>${keys.map((k) => `<td class="num">${num(r.summary?.[k])}</td>`).join("")}${eff ? `<td class="num">${eff(r)}</td>` : ""}
        <td class="${r.validity.level === "invalid" ? "fail" : ""}">${esc(r.validity.level)}</td>${sla !== null ? `<td class="${p === null ? "" : p ? "pass" : "fail"}">${p === null ? "-" : p ? "PASS" : "FAIL"}</td>` : ""}<td class="noprint"><a href="/report?runs=${esc(r.id)}">report</a></td></tr>`; }).join("")}</table>
    ${eff ? `<p class="muted">"Per desktop vs first step" shows how much each desktop still gets compared with the lightest step. A falling figure means desktops are starting to share a limit.</p>` : ""}
    <h2>What limited each step</h2><table>${rs.map((r) => `<tr><th class="num">${n(r)}</th><td>${esc(r.verdict || "-")}${[...r.validity.problems, ...r.validity.notes].map((p) => `<div class="err">${esc(p)}</div>`).join("")}</td></tr>`).join("")}</table>
    <div class="two"><div><h2>Test setup</h2>${kv(setupRows(base).filter((r) => r[0] !== "Desktops" && r[0] !== "Started"))}</div><div><h2>Environment</h2>${kv(envRows(base))}</div></div>`;
}

(async () => {
  const qs = new URLSearchParams(location.search), ids = qs.get("runs") || "", series = qs.get("series") || "";
  try {
    const res = await fetch(series ? `/api/report?series=${encodeURIComponent(series)}` : `/api/report?runs=${encodeURIComponent(ids)}`);
    const j = await res.json();
    if (!res.ok) throw new Error(j.detail || res.statusText);
    if (!j.runs.length) throw new Error("No run selected.");
    $("#report").innerHTML = (series ? density(j.runs) : j.runs.length === 1 ? single(j.runs[0]) : compare(j.runs)) +
      `<footer>Generated by vdi-tool ${esc(j.tool_version)} on ${esc(new Date(j.generated * 1000).toLocaleString())}. Throughput charts use Proxmox's own disk counters; per-desktop results come from the workload inside each desktop.</footer>`;
    $("#sla")?.addEventListener("submit", (e) => {
      e.preventDefault();
      const f = e.target, q = new URLSearchParams(location.search);
      if (f.v.value === "") q.delete("target"); else q.set("target", `${f.k.value}:${f.v.value}`);
      location.search = q.toString();
    });
  } catch (e) {
    $("#report").innerHTML = `<div class="banner invalid"><b>Could not build the report</b>${esc(e.message)}</div>`;
  }
})();
$("#print").addEventListener("click", () => window.print());
