// AMD Full stack dashboard — plain JS, no build step.

const $ = (sel) => document.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (r) => (r == null ? "–" : `${Math.round(r * 100)}%`);
const show = (el, on = true) => el.classList.toggle("hidden", !on);

const GROUP_NAMES = {
  privacy: "Privacy", refusal: "Refuses non-prescriptions", medicines: "Medicines found / none invented",
  fields: "Dosing fields", illness: "Illness", review: "Asks the doctor at the right time",
  safety: "No unsupported numbers", wording: "Required / forbidden wording",
};
const FIELD_NAMES = {
  amount: "Amount per dose", times_per_day: "Times per day", times_of_day: "Time of day", food: "Food",
  duration: "Duration", prn: "Only when needed", max_per_day: "Maximum per day", every_hours: "Hours between doses",
};

let config = null;
const datasetName = (f) => ({ "cases.jsonl": "Core", "realworld.jsonl": "Real-world messy" }[f]
  || (f.startsWith("random_seed") ? `Random (seed ${f.match(/\d+/)[0]})` : f.replace(".jsonl", "")));
let runs = [];
let current = null; // full run being shown

// ---------------------------------------------------------------- tabs
document.querySelectorAll(".tabs button").forEach((btn) =>
  btn.addEventListener("click", () => selectTab(btn.dataset.tab)));
function selectTab(name) {
  if (history.replaceState) history.replaceState(null, "", `#${name}`);
  document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) => show(t, t.id === `tab-${name}`));
}

// ---------------------------------------------------------------- tooltip
const tip = $("#tooltip");
document.addEventListener("mousemove", (e) => {
  const target = e.target.closest("[data-tip]");
  if (!target) return show(tip, false);
  tip.innerHTML = target.dataset.tip;
  show(tip, true);
  const x = Math.min(e.clientX + 14, window.innerWidth - tip.offsetWidth - 8);
  tip.style.left = `${x}px`;
  tip.style.top = `${e.clientY + 14}px`;
});

// ---------------------------------------------------------------- data
async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return res;
}

async function loadConfig() {
  config = await (await api("/api/config")).json();
  $("#config").textContent = `${config.provider} · ${config.model.split("/").pop()}`;
  $("#run-dataset").innerHTML = Object.entries(config.datasets).map(([f, n]) =>
    `<option value="${esc(f)}">${esc(datasetName(f))} (${n} cases)</option>`).join("");
  for (const sel of ["#run-provider", "#try-provider"]) {
    $(sel).innerHTML = config.providers.map((p) => `<option ${p === config.provider ? "selected" : ""}>${esc(p)}</option>`).join("");
  }
}

async function loadRuns(selectFile) {
  runs = await (await api("/api/runs")).json();
  show($("#empty"), !runs.length);
  show($("#overview"), runs.length > 0);
  $("#run-select").innerHTML = runs.map((r) =>
    `<option value="${esc(r.file)}">${esc(r.meta.started.replace("T", " "))} · ${esc(datasetName(r.meta.cases_file))} · ${esc(r.meta.provider)}${r.meta.cold_cache ? " · cold" : ""}</option>`).join("");
  renderHistory();
  const wanted = selectFile || new URLSearchParams(location.search).get("run");  // ?run=<file> picks a run
  if (runs.length) await showRun(runs.some((r) => r.file === wanted) ? wanted : runs[0].file);
}

$("#run-select").addEventListener("change", (e) => showRun(e.target.value));

async function showRun(file) {
  $("#run-select").value = file;
  current = await (await api(`/api/runs/${encodeURIComponent(file)}`)).json();
  const m = current.meta;
  $("#run-meta").textContent = `${datasetName(m.cases_file)} · ${m.model} · ${current.summary.cases} cases · ${Math.round(m.duration_s)}s`;
  renderOverview();
  renderCases();
}

// ---------------------------------------------------------------- overview
function tile(label, value, note = "", status = null) {
  const st = status == null ? "" :
    `<div class="status ${status ? "good" : "bad"}"><span class="dot ${status ? "good" : "bad"}"></span>${status ? "✓ OK" : "✗ Needs work"}</div>`;
  return `<div class="tile"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div>${note ? `<div class="note">${esc(note)}</div>` : ""}${st}</div>`;
}

function renderOverview() {
  const s = current.summary;
  const fieldChecks = Object.values(s.by_field).reduce((a, v) => [a[0] + v.passed, a[1] + v.total], [0, 0]);
  const t = s.time_to_dosing_s, tt = s.time_total_s;
  $("#tiles").innerHTML = [
    tile("Checks passed", pct(s.checks.rate), `${s.checks.passed} of ${s.checks.total}`),
    tile("Cases fully correct", `${s.cases_fully_correct}/${s.cases}`),
    tile("Dosing fields correct", pct(fieldChecks[1] ? fieldChecks[0] / fieldChecks[1] : null), `${fieldChecks[0]} of ${fieldChecks[1]}`),
    tile("Privacy leaks", s.privacy_leaks, "personal details sent to AI or patient", s.privacy_leaks === 0),
    tile("Unsupported numbers", s.unsupported_numbers, "numbers not on the prescription", s.unsupported_numbers === 0),
    tile("Review flag", `${s.review_flag.missed} missed`, `${s.review_flag.needless} needless`, s.review_flag.missed === 0),
    tile("Time to dosing", t ? `${t.median}s` : "–", t ? `median · p95 ${t.p95}s` : ""),
    tile("Full explanation", tt ? `${tt.median}s` : "–", tt ? `median · p95 ${tt.p95}s` : ""),
    tile("Reading level", s.reading_grade ? `grade ${s.reading_grade.mean}` : "–", "school years needed (lower is easier)"),
  ].join("");

  bars("#chart-fields", Object.entries(s.by_field).map(([k, v]) => ({
    name: FIELD_NAMES[k] || k, value: v.rate, label: `${v.passed}/${v.total} · ${pct(v.rate)}`,
    tip: `<b>${esc(FIELD_NAMES[k] || k)}</b><br>${v.passed} of ${v.total} correct`,
  })), 1);
  bars("#chart-groups", Object.entries(s.by_group).map(([k, v]) => ({
    name: GROUP_NAMES[k] || k, value: v.rate, label: `${v.passed}/${v.total} · ${pct(v.rate)}`,
    tip: `<b>${esc(GROUP_NAMES[k] || k)}</b><br>${v.passed} of ${v.total} checks passed`,
  })), 1);
  const timed = current.results.filter((r) => r.time_total != null).sort((a, b) => b.time_total - a.time_total);
  const maxT = Math.max(...timed.map((r) => r.time_total), 1);
  bars("#chart-time", timed.map((r) => ({
    name: r.id, value: r.time_total, label: `${r.time_total.toFixed(1)}s`,
    tip: `<b>${esc(r.id)}</b><br>Dosing shown after ${r.time_to_dosing ?? "–"}s<br>Complete after ${r.time_total}s`,
  })), maxT);

  $("#table-categories").innerHTML = `<table><thead><tr><th>Kind</th><th class="num">Fully correct</th></tr></thead><tbody>${
    Object.entries(s.by_category).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="num">${statusMark(v.passed === v.total)} ${v.passed}/${v.total}</td></tr>`).join("")
  }</tbody></table>`;
}

function bars(sel, rows, max) {
  $(sel).innerHTML = rows.map((r) => `
    <div class="bar-row" data-tip="${esc(r.tip)}">
      <span class="name" title="${esc(r.name)}">${esc(r.name)}</span>
      <span class="track"><span class="fill" style="width:${Math.max(0, (r.value ?? 0) / max * 100)}%"></span></span>
      <span class="val">${esc(r.label)}</span>
    </div>`).join("");
}

function statusMark(ok) {
  return `<span class="status ${ok ? "good" : "bad"}">${ok ? "✓" : "✗"}</span>`;
}

function renderHistory() {
  $("#table-history").innerHTML = `<div class="table-scroll"><table><thead><tr>
    <th>Run</th><th>Test set</th><th>Provider / model</th><th class="num">Checks</th><th class="num">Cases correct</th>
    <th class="num">Fields</th><th class="num">Privacy leaks</th><th class="num">Unsupported numbers</th>
    <th class="num">Dosing (median)</th><th class="num">Total (median)</th></tr></thead><tbody>${
    runs.map((r) => {
      const s = r.summary;
      const f = Object.values(s.by_field).reduce((a, v) => [a[0] + v.passed, a[1] + v.total], [0, 0]);
      return `<tr><td>${esc(r.meta.started.replace("T", " "))}${r.meta.cold_cache ? " · cold" : ""}</td>
        <td>${esc(datasetName(r.meta.cases_file))}</td>
        <td>${esc(r.meta.provider)} · ${esc(r.meta.model.split("/").pop())}</td>
        <td class="num">${pct(s.checks.rate)}</td><td class="num">${s.cases_fully_correct}/${s.cases}</td>
        <td class="num">${pct(f[1] ? f[0] / f[1] : null)}</td><td class="num">${s.privacy_leaks}</td>
        <td class="num">${s.unsupported_numbers}</td>
        <td class="num">${s.time_to_dosing_s ? s.time_to_dosing_s.median + "s" : "–"}</td>
        <td class="num">${s.time_total_s ? s.time_total_s.median + "s" : "–"}</td></tr>`;
    }).join("")}</tbody></table></div>`;
}

// ---------------------------------------------------------------- cases
$("#only-failed").addEventListener("change", renderCases);

function renderCases() {
  if (!current) return;
  const onlyFailed = $("#only-failed").checked;
  const rows = current.results.filter((r) => !onlyFailed || r.checks.some((c) => !c.ok));
  $("#cases-table").innerHTML = `<div class="card table-scroll"><table><thead><tr>
    <th>Result</th><th>Case</th><th>Kind</th><th class="num">Checks</th><th class="num">Time</th></tr></thead><tbody>${
    rows.map((r) => {
      const passed = r.checks.filter((c) => c.ok).length;
      const ok = passed === r.checks.length;
      return `<tr class="case-row" data-id="${esc(r.id)}"><td>${statusMark(ok)} ${ok ? "Pass" : `${r.checks.length - passed} failed`}</td>
        <td>${esc(r.id)}</td><td>${esc(r.category)}</td><td class="num">${passed}/${r.checks.length}</td>
        <td class="num">${r.time_total != null ? r.time_total.toFixed(1) + "s" : "–"}</td></tr>
        <tr class="detail hidden" data-detail="${esc(r.id)}"><td colspan="5">${caseDetail(r)}</td></tr>`;
    }).join("")}</tbody></table></div>`;
  document.querySelectorAll(".case-row").forEach((row) => row.addEventListener("click", () =>
    document.querySelector(`[data-detail="${CSS.escape(row.dataset.id)}"]`).classList.toggle("hidden")));
}

function caseDetail(r) {
  const fmt = (v) => (typeof v === "object" && v !== null ? JSON.stringify(v) : String(v));
  return `
    <h3>Checks</h3>
    <table><thead><tr><th></th><th>Area</th><th>Check</th><th>Expected</th><th>Got</th></tr></thead><tbody>${
      r.checks.map((c) => `<tr><td>${statusMark(c.ok)}</td><td>${esc(GROUP_NAMES[c.group] || c.group)}</td><td>${esc(c.name)}</td>
        <td>${esc(fmt(c.expected))}</td><td>${esc(fmt(c.got))}</td></tr>`).join("")}</tbody></table>
    <div class="detail-grid">
      <div><h3>Prescription</h3><pre>${esc(r.input)}</pre></div>
      <div><h3>Sent to the AI${r.privacy_removed?.length ? ` (removed: ${esc(r.privacy_removed.join(", "))})` : ""}</h3><pre>${esc(r.sent_to_ai)}</pre></div>
      <div><h3>For the patient</h3><pre>${esc(r.output || r.error || "")}</pre></div>
      <div><h3>For the doctor</h3><pre class="review">${esc((r.review || []).join("\n"))}</pre></div>
    </div>`;
}

// ---------------------------------------------------------------- run benchmark
$("#run-start").addEventListener("click", async () => {
  show($("#run-error"), false);
  const body = {
    provider: $("#run-provider").value, cold: $("#run-cold").checked, dataset: $("#run-dataset").value,
    limit: parseInt($("#run-limit").value, 10) || null,
  };
  try {
    await api("/api/benchmark", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    $("#run-start").disabled = true;
    show($("#run-progress"), true);
    show($("#run-done"), false);
    $("#run-cases").innerHTML = "";
    poll();
  } catch (e) {
    $("#run-error").textContent = e.message;
    show($("#run-error"), true);
  }
});

async function poll() {
  const job = await (await api("/api/benchmark/status")).json();
  if (!job.total) return;
  $("#run-count").textContent = `${job.done} of ${job.total} cases · ${datasetName(job.dataset)} · ${job.provider}`;
  $("#run-bar").style.width = `${(job.done / job.total) * 100}%`;
  const avg = job.done ? (Date.now() / 1000 - job.started) / job.done : null;
  $("#run-eta").textContent = job.running && avg ? `about ${Math.round(avg * (job.total - job.done))}s left` : "";
  $("#run-cases").innerHTML = job.cases.map((c) => {
    const ok = !c.failed.length;
    return `<li>${statusMark(ok)} <b>${esc(c.id)}</b> · ${c.passed}/${c.checks} checks · ${c.time_total.toFixed(1)}s${
      ok ? "" : ` <span class="muted">— ${esc(c.failed.slice(0, 3).join("; "))}</span>`}</li>`;
  }).join("");
  if (job.running) return setTimeout(poll, 1500);

  $("#run-start").disabled = false;
  show($("#run-done"), true);
  if (job.error) {
    $("#run-done").innerHTML = `<span class="error">The run stopped: ${esc(job.error)}</span>`;
  } else {
    $("#run-done").innerHTML = `Done. <a href="#" id="open-run">Open the results</a>`;
    $("#open-run").addEventListener("click", async (e) => {
      e.preventDefault();
      await loadRuns(job.file);
      selectTab("overview");
    });
  }
}

// ---------------------------------------------------------------- try it
const EXAMPLES = {
  angine: "Diagnostic : Angine bactérienne avec fièvre\n1) Amoxicilline 1 g : 1 cp matin et soir pendant 8 jours, au milieu des repas.\n2) Paracétamol 1 g : 1 cp toutes les 6 h si fièvre ou douleur, max 3 cp/j.\n3) Strepsils : 1 pastille à sucer 3 fois par jour si mal de gorge.\nBoire beaucoup d'eau. Repos 3 jours. Revoir dans 10 jours si pas d'amélioration.",
  diabetes: "Diagnostic : Diabète type 2 et HTA\n1) Metformine 850 mg : 1 cp matin et soir au cours des repas. QSP 3 mois.\n2) Amlodipine 5 mg : 1 cp le matin. Traitement au long cours.\nRégime pauvre en sel et en sucre. Marcher 30 min par jour. Contrôle de la glycémie dans 1 mois.",
  personal: "Dr. Karim Benali — Médecine générale, Casablanca\nTél : 0522 33 44 55\nPatient : Mme Fatima Zahra El Idrissi, 54 ans\nCIN : BE123456\nDate : 10/10/2026\n\nDiagnostic : Angine\n1) Amoxicilline 1 g : 1 cp matin et soir pendant 8 jours, au milieu des repas.\n2) Paracétamol 1 g : 1 cp toutes les 6 h si fièvre, max 3 cp/j.\nRevoir dans 10 jours.\n\nSignature et cachet",
  unclear: "Diagnostic : Bronchite\n1) Augmentin 1 g : 1 sachet matin et soir pendant 7 jours.\n2) Toplexil sirop : c. à s. ???\n3) Ventoline : 2 bouffées si gêne respiratoire.\nRevoir si pas d'amélioration.",
};
document.querySelectorAll("[data-example]").forEach((b) =>
  b.addEventListener("click", () => { $("#try-input").value = EXAMPLES[b.dataset.example]; }));

$("#try-go").addEventListener("click", async () => {
  const text = $("#try-input").value.trim();
  if (!text) return;
  $("#try-go").disabled = true;
  $("#try-status").textContent = "Reading the prescription…";
  show($("#try-out"), true);
  ["#try-sent", "#try-patient", "#try-review", "#try-removed"].forEach((s) => ($(s).textContent = ""));
  $("#try-tiles").innerHTML = "";

  let illness = [], meds = [], details = {};
  const paint = () => {
    const lines = [...illness, ...(illness.length ? [""] : [])];
    if (meds.length) lines.push("How to take them", ...meds.flat(), "");
    const det = Object.keys(details).sort((a, b) => a - b).map((k) => details[k]).flat();
    if (det.length) lines.push("What they are for", ...det);
    $("#try-patient").textContent = lines.join("\n");
  };

  try {
    const res = await api("/api/explain", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, provider: $("#try-provider").value }),
    });
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "", dosingAt = null;
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buffer.indexOf("\n")) >= 0) {
        const ev = JSON.parse(buffer.slice(0, nl));
        buffer = buffer.slice(nl + 1);
        if (ev.type === "privacy") {
          $("#try-sent").textContent = ev.sent_to_ai || "(nothing medical)";
          $("#try-removed").textContent = ev.removed.length ? `Removed on this computer first: ${ev.removed.join(", ")}.` : "No personal details found.";
        } else if (ev.type === "dosing") {
          dosingAt = ev.elapsed;
          illness = ev.illness; meds = ev.medicines; paint();
          $("#try-status").textContent = `Dosing shown after ${ev.elapsed}s · looking up what each medicine is for…`;
        } else if (ev.type === "medicine") {
          details[ev.index] = ev.lines; paint();
        } else if (ev.type === "done") {
          $("#try-patient").textContent = ev.text;
          $("#try-review").textContent = ev.review.join("\n");
          $("#try-status").textContent = "";
          $("#try-tiles").innerHTML = [
            tile("Time to dosing", `${dosingAt}s`),
            tile("Complete", `${ev.elapsed}s`),
            tile("Doctor check needed", ev.needs_review ? "Yes" : "No", ev.needs_review ? "something is uncertain" : "everything understood"),
            tile("Unsupported numbers", ev.unsupported_numbers.length, ev.unsupported_numbers.join(", "), ev.unsupported_numbers.length === 0),
            tile("Reading level", ev.reading_grade != null ? `grade ${ev.reading_grade}` : "–", "school years needed"),
          ].join("");
        } else if (ev.type === "error") {
          $("#try-patient").textContent = ev.message;
          $("#try-status").textContent = "";
        }
      }
    }
  } catch (e) {
    $("#try-status").textContent = e.message;
  } finally {
    $("#try-go").disabled = false;
  }
});

// ---------------------------------------------------------------- start
loadConfig().then(() => loadRuns()).then(() => {
  const tab = location.hash.slice(1);  // e.g. #cases opens the Cases tab
  if (["overview", "cases", "run", "try"].includes(tab)) selectTab(tab);
}).catch((e) => { $("#config").textContent = `Backend not reachable: ${e.message}`; });
