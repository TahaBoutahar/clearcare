// Prescription Explainer — demo (iPhone Safari first, also laptop/iPad). Plain JS, no build step.

const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const show = (el, on = true) => el.classList.toggle("hidden", !on);
const store = {
  get: (k, d = "") => { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch {} },
};

const LANGS = { en: "English", fr: "Français", ar: "العربية" };
const REASONS = ["Wrong dose", "Missing medicine", "Wrong medicine", "Wrong timing", "Bad translation", "Hard to understand", "Unreadable", "Other"];
const EXAMPLE = "Patient : Mme Fatima Z., 54 ans\nDiagnostic : Angine bactérienne avec fièvre\n1) Amoxicilline 1 g : 1 cp matin et soir pendant 8 jours, au milieu des repas.\n2) Paracétamol 1 g : 1 cp toutes les 6 h si fièvre ou douleur, max 3 cp/j.\n3) Strepsils : 1 pastille à sucer 3 fois par jour si mal de gorge.\nBoire beaucoup d'eau. Repos 3 jours. Revoir dans 10 jours si pas d'amélioration.\nSignature et cachet";
const STAMP = { en: (d) => `Checked by your doctor on ${d}.`, fr: (d) => `Vérifié par votre médecin le ${d}.`, ar: (d) => `راجعه طبيبك بتاريخ ${d}.` };

let rx = null;              // the prescription on screen
let inputMode = "typed";
let lang = store.get("lang", "en");

// ------------------------------------------------------------------ server
async function api(path, options = {}) {
  const res = await fetch(path, { ...options, headers: { ...(options.headers || {}), "X-Pin": store.get("pin") } });
  if (res.status === 401) { $("#dlg-pin").showModal(); throw new Error("PIN needed"); }
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return res;
}
const postJSON = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

// ------------------------------------------------------------------ history (on this device only)
const history = {
  all() { try { return JSON.parse(store.get("rx-history", "[]")); } catch { return []; } },
  save(entry) {
    const list = this.all().filter((e) => e.key !== entry.key);
    list.unshift(entry);
    store.set("rx-history", JSON.stringify(list.slice(0, 30)));
    renderHistory();
  },
};
function badgeOf(e) {
  if (e.status === "approved") return `<span class="badge approved">Approved · ${e.edited ? "edited · " : ""}${LANGS[e.lang]}</span>`;
  if (e.status === "rejected") return `<span class="badge rejected">Rejected · ${esc(e.reason || "")}</span>`;
  if (e.needsReview) return `<span class="badge review">Needs review · ${LANGS[e.lang]}</span>`;
  return `<span class="badge draft">Not decided · ${LANGS[e.lang]}</span>`;
}
function renderHistory() {
  const q = $("#search").value.trim().toLowerCase();
  const today = new Date().toDateString();
  const list = history.all().filter((e) => new Date(e.created).toDateString() === today)
    .filter((e) => !q || (e.title + " " + (e.meds || []).join(" ")).toLowerCase().includes(q));
  $("#history").innerHTML = list.length ? list.map((e) => `
    <li><button data-key="${esc(e.key)}" class="${rx && rx.key === e.key ? "active" : ""}">
      <div class="h-top"><span>${esc(e.title)}</span><span class="h-time">${esc(e.time)}</span></div>
      ${badgeOf(e)}
    </button></li>`).join("") : `<li class="empty-history">${q ? "Nothing found." : "No prescriptions yet today."}</li>`;
  document.querySelectorAll("#history button").forEach((b) => (b.onclick = () => openEntry(b.dataset.key)));
}
$("#search").oninput = renderHistory;

function openEntry(key) {
  const e = history.all().find((x) => x.key === key);
  if (!e) return;
  rx = { ...e };
  lang = e.lang;
  closeDrawer();
  paintResult();
}

// ------------------------------------------------------------------ drawer (tablet / phone)
function closeDrawer() { $("#side").classList.remove("open"); show($("#scrim"), false); }
$("#menu").onclick = () => { $("#side").classList.add("open"); show($("#scrim"), true); };
$("#scrim").onclick = closeDrawer;

// ------------------------------------------------------------------ new prescription
function newPrescription() {
  rx = null;
  stopAudio();
  show($("#rx-head"), false);
  show($("#input-area"), true);
  show($("#masked"), false);
  show($("#checks"), false);
  show($("#player"), false); show($("#player-note"), false); show($("#give"), false);
  show($("#bottom-actions"), false);
  $("#chips").innerHTML = "";
  $("#status").textContent = "";
  $("#sheet").innerHTML = `<p class="placeholder">The patient’s explanation will appear here.</p>`;
  $("#sheet").removeAttribute("dir");
  $("#rx-input").value = "";
  inputMode = "typed";
  setMode("type");
  closeDrawer();
  renderHistory();
}
$("#new").onclick = newPrescription;
$("#example").onclick = () => { setMode("type"); $("#rx-input").value = EXAMPLE; inputMode = "typed"; };

function setMode(mode) {
  document.querySelectorAll("#mode button").forEach((b) => b.setAttribute("aria-selected", b.dataset.mode === mode));
  show($("#dictate-area"), mode === "dictate");
}
document.querySelectorAll("#mode button").forEach((b) => (b.onclick = () => { if (!rx) setMode(b.dataset.mode); }));

// ------------------------------------------------------------------ dictation (iPhone records audio/mp4)
let recorder = null, chunks = [];
$("#mic").onclick = async () => {
  if (recorder && recorder.state === "recording") return recorder.stop();
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) return micStatus("Dictation needs the https address of the demo. You can still type.");
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const type = ["audio/mp4", "audio/webm;codecs=opus", "audio/webm"].find((t) => MediaRecorder.isTypeSupported(t)) || "";
    recorder = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
    chunks = [];
    recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data);
    recorder.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop());
      $("#mic").setAttribute("aria-pressed", "false");
      micStatus("Turning your voice into text…");
      const kind = recorder.mimeType || type;
      const form = new FormData();
      form.append("audio", new Blob(chunks, { type: kind }), kind.includes("mp4") ? "voice.m4a" : "voice.webm");
      try {
        const { text } = await (await api("/api/transcribe", { method: "POST", body: form })).json();
        const box = $("#rx-input");
        box.value = (box.value ? box.value.trim() + "\n" : "") + text;
        inputMode = "voice";
        setMode("type");
        micStatus("Tap to dictate the prescription. Tap again to stop.");
      } catch (e) { micStatus(`Sorry, I could not turn the recording into text. Why: ${e.message}`); }
    };
    recorder.start();
    $("#mic").setAttribute("aria-pressed", "true");
    micStatus("Recording… speak the prescription, then tap again to stop.");
  } catch { micStatus("The microphone is not allowed. Allow it in Safari, or type the prescription."); }
};
function micStatus(t) { $("#mic-status").textContent = t; }

// ------------------------------------------------------------------ explain
$("#explain").onclick = async () => {
  const text = $("#rx-input").value.trim();
  if (!text) return;
  const num = String(Number(store.get("rx-counter", "0")) + 1).padStart(4, "0");
  store.set("rx-counter", num);
  rx = { key: crypto.randomUUID?.() || String(Date.now()), num, started: performance.now(), lang, inputMode,
         created: Date.now(), time: new Date().toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" }) };
  $("#explain").disabled = true;
  $("#status").textContent = "Reading the prescription…";
  try {
    const res = await postJSON("/api/explain", { text, lang });
    const reader = res.body.getReader(), decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buffer.indexOf("\n")) >= 0) { onEvent(JSON.parse(buffer.slice(0, nl))); buffer = buffer.slice(nl + 1); }
    }
  } catch (e) {
    $("#status").textContent = `Sorry, something went wrong. Why: ${e.message}`;
  } finally { $("#explain").disabled = false; }
};

function onEvent(ev) {
  if (ev.type === "privacy") {
    rx.masked = ev.masked; rx.chips = ev.chips;
    show($("#input-area"), false);
    paintDoctor();
  } else if (ev.type === "dosing") {
    $("#status").textContent = `Read in ${ev.elapsed} s · writing the patient’s explanation…`;
  } else if (ev.type === "done") {
    Object.assign(rx, { id: ev.id, title: ev.title, readIn: ev.read_in, checks: ev.checks, needsReview: ev.needs_review,
      sheet: ev.sheet, medicines: ev.medicines, flagsUnsure: ev.flags_unsure, flagsRisk: ev.flags_risk,
      meds: ev.sheet.cards.map((c) => c.name), status: "open" });
    $("#status").textContent = "";
    history.save(snapshot());
    paintResult();
  } else if (ev.type === "error") {
    $("#status").textContent = ev.message;
    show($("#input-area"), true);
    show($("#masked"), false);
  }
}
const snapshot = () => ({ key: rx.key, num: rx.num, title: rx.title, time: rx.time, created: rx.created, lang: rx.lang,
  status: rx.status, reason: rx.reason, edited: rx.edited, needsReview: rx.needsReview, masked: rx.masked, chips: rx.chips,
  checks: rx.checks, sheet: rx.sheet, final: rx.final, readIn: rx.readIn, meds: rx.meds, id: rx.id });

// ------------------------------------------------------------------ painting
function paintDoctor() {
  show($("#input-area"), false);
  $("#masked").innerHTML = esc(rx.masked).replace(/\[(NAME|AGE|PHONE|ID|DATE|EMAIL|ADDRESS)\]/g, '<span class="tok">[$1]</span>');
  show($("#masked"), true);
  $("#chips").innerHTML = (rx.chips || []).map((c) => `<span class="chip">${esc(c)}</span>`).join("");
}

function paintResult() {
  stopAudio();
  paintDoctor();
  show($("#rx-head"), true);
  $("#rx-meta").textContent = `Prescription #${rx.num}${rx.readIn != null ? ` · read in ${rx.readIn} s` : ""}`;
  $("#rx-title").textContent = rx.title;
  const decided = rx.status === "approved" || rx.status === "rejected";
  show($("#rx-actions"), !decided);
  show($("#bottom-actions"), !decided);
  $("#rx-done").className = `rx-done ${rx.status === "rejected" ? "rejected" : ""}`;
  show($("#rx-done"), decided);
  $("#rx-done").textContent = rx.status === "approved" ? `✓ Approved and given to the patient${rx.edited ? " (edited)" : ""}`
    : rx.status === "rejected" ? `Rejected · ${rx.reason}` : "";

  // Checks box
  const items = rx.checks || [];
  const cls = (k) => ({ "HIGH RISK": "k-risk", DOUBT: "k-doubt", CHECKED: "k-checked" }[k] || "k-note");
  $("#checks").className = `checks ${rx.needsReview ? "" : "ok"}`;
  $("#checks").innerHTML = `<h3>${rx.needsReview ? "⚠ Please check before approving" : "✓ Ready to approve"}</h3>` + items.map((it) =>
    `<div class="check"><span class="kind ${cls(it.kind)}">${esc(it.kind)}</span><div>${it.source ? `<b>${esc(it.source)}</b> — ` : ""}${esc(it.text)}</div></div>`).join("");
  show($("#checks"), true);

  paintLangs();
  paintSheet();
  show($("#player"), true); show($("#player-note"), true);
  show($("#give"), true);
  setGiveEnabled(rx.status === "approved");
  renderHistory();
}

function paintLangs() {
  $("#langs").innerHTML = Object.entries(LANGS).map(([k, v]) =>
    `<button role="radio" data-lang="${k}" aria-checked="${k === lang}" ${k === "ar" ? 'lang="ar"' : ""}>${v}</button>`).join("");
  document.querySelectorAll("#langs button").forEach((b) => (b.onclick = () => switchLang(b.dataset.lang)));
  $("#player-lang").textContent = LANGS[lang];
  $("#pdf-lang").textContent = LANGS[lang];
}

function paintSheet() {
  const s = rx.sheet;
  if (!s) return;
  const el = $("#sheet");
  el.setAttribute("dir", s.rtl ? "rtl" : "ltr");
  el.setAttribute("lang", s.lang);
  if (rx.status === "approved" && rx.edited) {
    el.innerHTML = `<p class="edited">${esc(rx.final)}</p>`;
    return;
  }
  const kept = s.kept_in_english || [];
  let html = kept.length ? `<p class="kept">${kept.length} sentence(s) stayed in English: the translation did not pass the safety check.</p>` : "";
  const illness = s.illness.map((x) => x.replace(/\.$/, "")).join(", ");
  html += `<p class="intro">${illness ? esc(illness) + ". " : ""}${esc(s.intro)}</p>`;
  html += s.cards.map((c) => `
    <div class="med ${c.doubt ? "doubt" : ""} ${c.risk ? "risk" : ""}">
      <span class="num">${c.n}</span>
      <div>
        <div><span class="med-name">${esc(c.name)}</span>${c.tag ? ` <span class="med-tag">· ${esc(c.tag)}</span>` : ""}</div>
        ${c.lines.map((l) => `<p>${esc(l)}</p>`).join("")}
        ${c.sorry ? `<p class="sorry">${esc(c.sorry)}</p>` : ""}
      </div>
    </div>`).join("");
  if (s.schedule.length) html += `<div class="sec">${esc(s.labels["When to take them each day"])}</div>` + s.schedule.map((l) => `<p class="line">${esc(l)}</p>`).join("");
  if (s.also.length) html += `<div class="sec">${esc(s.labels["Also"])}</div>` + s.also.map((l) => `<p class="line">${esc(l)}</p>`).join("");
  if (s.unclear.length) html += `<div class="sec">${esc(s.labels["Sorry, I could not understand everything"])}</div>` +
    s.unclear.map((l) => `<p class="line">${esc(l)}</p>`).join("") + `<p class="line">${esc(s.ask_about_this)}</p>`;
  html += `<p class="muted small">${esc(s.disclaimer)}</p>`;
  el.innerHTML = html;
}

async function switchLang(next) {
  if (!rx?.sheet) { lang = next; paintLangs(); return; }   // chosen before Explain
  if (next === lang) return;
  if (rx.status === "approved") return alert("This sheet is already approved. Start a new prescription to change the language.");
  $("#status").textContent = next === "en" ? "" : "Translating…";
  try {
    rx.sheet = await (await postJSON("/api/translate", { id: rx.id, lang: next })).json();
    lang = rx.lang = next;
    $("#status").textContent = "";
    history.save(snapshot());
    stopAudio();
    paintLangs(); paintSheet();
  } catch (e) { $("#status").textContent = `Sorry, the translation failed. Why: ${e.message}`; }
}

// ------------------------------------------------------------------ decisions (logged without patient text)
function logDecision(decision, extra = {}) {
  postJSON("/api/decision", {
    id: rx.id, doctor: store.get("doctor"), decision, lang: rx.lang, input_mode: rx.inputMode || inputMode,
    seconds: rx.started ? Math.round((performance.now() - rx.started) / 100) / 10 : null, time_to_dosing: rx.readIn,
    lines_total: rx.sheet.text.split("\n").filter((l) => l.trim()).length, needs_review: rx.needsReview,
    medicines: rx.medicines, flags_unsure: rx.flagsUnsure, flags_risk: rx.flagsRisk, ...extra,
  }).catch(() => {});
}
const today = () => new Date().toLocaleDateString("fr-FR");

function approve() {
  if (rx.needsReview && !confirm("Some items are marked HIGH RISK or DOUBT. Have you checked them?")) return;
  logDecision("approved");
  rx.status = "approved";
  rx.final = `${rx.sheet.text}\n\n${STAMP[rx.lang](today())}`;
  history.save(snapshot());
  paintResult();
}
$("#approve").onclick = approve; $("#approve-m").onclick = approve;

function openEdit() {
  $("#edit-text").value = rx.sheet.text;
  $("#edit-text").dir = rx.sheet.rtl ? "rtl" : "ltr";
  $("#dlg-edit").showModal();
}
$("#edit").onclick = openEdit; $("#edit-m").onclick = openEdit;
$("#edit-save").onclick = () => {
  const before = rx.sheet.text.split("\n").map((l) => l.trim()).filter(Boolean);
  const after = new Set($("#edit-text").value.split("\n").map((l) => l.trim()).filter(Boolean));
  logDecision("edited", { lines_changed: before.filter((l) => !after.has(l)).length });
  rx.status = "approved"; rx.edited = true;
  rx.final = `${$("#edit-text").value.trim()}\n\n${STAMP[rx.lang](today())}`;
  $("#dlg-edit").close();
  history.save(snapshot());
  paintResult();
};

function openReject() {
  $("#reasons").innerHTML = REASONS.map((r) => `<button data-r="${esc(r)}">${esc(r)}</button>`).join("");
  document.querySelectorAll("#reasons button").forEach((b) => (b.onclick = () => {
    logDecision("rejected", { reason: b.dataset.r });
    rx.status = "rejected"; rx.reason = b.dataset.r.toLowerCase();
    $("#dlg-reject").close();
    history.save(snapshot());
    paintResult();
  }));
  $("#dlg-reject").showModal();
}
$("#reject").onclick = openReject; $("#reject-m").onclick = openReject;
document.querySelectorAll("[data-close]").forEach((b) => (b.onclick = () => b.closest("dialog").close()));

// ------------------------------------------------------------------ give it: PDF / print / share
function setGiveEnabled(on) { ["#pdf", "#print", "#share"].forEach((s) => ($(s).disabled = !on)); }
function fillPrint() {
  const el = $("#print-sheet");
  el.setAttribute("dir", rx.sheet.rtl ? "rtl" : "ltr");
  el.innerHTML = `<h1>${esc(rx.title)}</h1>` + esc(rx.final).replace(/\n/g, "<br>");
}
$("#pdf").onclick = () => { fillPrint(); window.print(); };   // iPhone: Print > Share > Save to Files as PDF
$("#print").onclick = () => { fillPrint(); window.print(); };
$("#share").onclick = async () => {
  if (navigator.share) { try { await navigator.share({ title: rx.title, text: rx.final }); } catch {} }
  else { try { await navigator.clipboard.writeText(rx.final); $("#share").textContent = "Copied"; } catch {} }
};

// ------------------------------------------------------------------ spoken version
const audio = new Audio();
let audioFor = "";
const fmt = (s) => (isFinite(s) ? `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}` : "0:00");
audio.ontimeupdate = () => {
  $("#bar-fill").style.width = `${(audio.currentTime / (audio.duration || 1)) * 100}%`;
  $("#player-time").textContent = `${fmt(audio.currentTime)} / ${fmt(audio.duration)}`;
};
audio.onended = audio.onpause = () => { $("#play").textContent = "▶"; };
audio.onplay = () => { $("#play").textContent = "❚❚"; };
function stopAudio() {
  audio.pause();
  window.speechSynthesis?.cancel();
  $("#bar-fill").style.width = "0"; $("#player-time").textContent = "0:00 / 0:00";
}
$("#play").onclick = async () => {
  if (!audio.paused) return audio.pause();
  const text = rx.final || rx.sheet.text;
  const key = rx.lang + "::" + text;
  if (audioFor !== key) {
    $("#play").textContent = "…"; $("#play").disabled = true;
    try {
      const blob = await (await postJSON("/api/audio", { text, lang: rx.lang })).blob();
      audio.src = URL.createObjectURL(blob);
      audioFor = key;
    } catch (e) { alert(`Sorry, the spoken version could not be made. Why: ${e.message}`); $("#play").textContent = "▶"; return; }
    finally { $("#play").disabled = false; }
  }
  audio.play();
};
$("#speak-local").onclick = () => {
  if (!("speechSynthesis" in window)) return;
  stopAudio();
  const voiceLang = { en: "en-US", fr: "fr-FR", ar: "ar-SA" }[rx.lang];
  const voice = speechSynthesis.getVoices().find((v) => v.lang.replace("_", "-").toLowerCase().startsWith(rx.lang));
  (rx.final || rx.sheet.text).split("\n").map((l) => l.trim().replace(/^\d+\.\s*/, "")).filter(Boolean).forEach((part) => {
    const u = new SpeechSynthesisUtterance(part);
    u.lang = voiceLang; if (voice) u.voice = voice; u.rate = 0.9;
    speechSynthesis.speak(u);
  });
};
window.speechSynthesis?.getVoices();

// ------------------------------------------------------------------ settings, stats, PIN
$("#avatar").onclick = () => {
  $("#doctor").value = store.get("doctor");
  $("#pin-setting").value = store.get("pin");
  $("#default-lang").innerHTML = Object.entries(LANGS).map(([k, v]) => `<option value="${k}" ${k === store.get("lang", "en") ? "selected" : ""}>${v}</option>`).join("");
  $("#dlg-settings").showModal();
};
$("#settings-save").onclick = () => {
  store.set("doctor", $("#doctor").value.trim());
  store.set("pin", $("#pin-setting").value.trim());
  store.set("lang", $("#default-lang").value);
  if (!rx) { lang = $("#default-lang").value; }
  paintAvatar();
  $("#dlg-settings").close();
};
function paintAvatar() { $("#avatar").textContent = (store.get("doctor") || "Dr").slice(0, 3); }

$("#open-stats").onclick = async () => {
  closeDrawer();
  $("#dlg-stats").showModal();
  try {
    const s = await (await api("/api/stats")).json();
    const pct = (a, b) => (b ? `${Math.round((a / b) * 100)}%` : "–");
    const tile = (l, v, n = "") => `<div class="tile"><div class="label">${esc(l)}</div><div class="value">${esc(v)}</div><div class="note">${esc(n)}</div></div>`;
    $("#stats").innerHTML = [
      tile("Prescriptions", s.total, `${s.doctors} doctor(s) · ${s.voice} by voice`),
      tile("Approved as is", pct(s.approved, s.total), `${s.approved} of ${s.total}`),
      tile("Edited", pct(s.edited, s.total), s.edit_share != null ? `${Math.round(s.edit_share * 100)}% of lines changed on average` : ""),
      tile("Rejected", pct(s.rejected, s.total), `${s.rejected} of ${s.total}`),
      tile("Time per prescription", s.median_seconds != null ? `${s.median_seconds}s` : "–", "median, from Explain to decision"),
      tile("Flags were right", pct(s.flagged_changed, s.flagged), `${s.flagged_changed} of ${s.flagged} flagged needed changes`),
      tile("Clean ones were right", pct(s.clean - s.clean_changed, s.clean), `${s.clean - s.clean_changed} of ${s.clean} approved as is`),
      tile("Languages", Object.entries(s.by_language).map(([k, v]) => `${k.toUpperCase()} ${v}`).join(" · ")),
    ].join("");
    const reasons = Object.entries(s.reasons);
    $("#reasons-table").innerHTML = reasons.length ? `<table>${reasons.map(([r, n]) => `<tr><td>${esc(r)}</td><td class="num">${n}</td></tr>`).join("")}</table>` : "";
  } catch (e) { $("#stats").innerHTML = `<p class="error">${esc(e.message)}</p>`; }
};

$("#pin-go").onclick = async () => {
  store.set("pin", $("#pin-input").value.trim());
  try { await api("/api/check-pin", { method: "POST" }); show($("#pin-error"), false); $("#dlg-pin").close(); }
  catch { show($("#pin-error"), true); }
};

// ------------------------------------------------------------------ start
(async () => {
  paintAvatar();
  newPrescription();
  paintLangs();
  setGiveEnabled(false);
  try {
    const c = await (await api("/api/config")).json();
    $("#model-name").textContent = `${c.provider.toUpperCase()} · ${c.model.split("/").pop()}`;
    $("#about").textContent = `AI: ${c.provider} · ${c.model}. History stays on this device; only decisions are logged on the server.`;
    if (c.pin) { try { await api("/api/check-pin", { method: "POST" }); } catch {} }
  } catch (e) { $("#model-name").textContent = "offline"; }
})();
