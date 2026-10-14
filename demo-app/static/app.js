// Rx Explainer demo — phone app (iPhone Safari first). Plain JS, no build step.

const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const show = (el, on = true) => el.classList.toggle("hidden", !on);
const store = {
  get: (k, d = "") => { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch {} },
};

const LANGS = { en: "English", fr: "Français", ar: "العربية" };
const REASONS = ["Wrong dose", "Missing medicine", "Wrong medicine", "Wrong timing", "Bad translation", "Hard to understand", "Other"];
const EXAMPLE = "Diagnostic : Angine\nAmoxicilline 1 g : 1 cp matin et soir pendant 8 jours, au milieu des repas.\nParacétamol 1 g : 1 cp toutes les 6 h si fièvre, max 3 cp/j.\nRevoir dans 10 jours.";

let state = {};       // current prescription: id, sheet, lang, review, timings, flags
let inputMode = "typed";

// ------------------------------------------------------------------ navigation
const SCREENS = ["pin", "new", "result", "edit", "reject", "done", "stats", "settings"];
function go(name) {
  if ("speechSynthesis" in window) stopSpeaking();
  SCREENS.forEach((s) => show($(`#screen-${s}`), s === name));
  window.scrollTo(0, 0);
}
$("#nav-new").onclick = () => { go("new"); };
$("#nav-stats").onclick = () => { go("stats"); loadStats(); };
$("#nav-settings").onclick = () => { fillSettings(); go("settings"); };
$("#again").onclick = () => { $("#rx-input").value = ""; inputMode = "typed"; go("new"); };
$("#example").onclick = () => { $("#rx-input").value = EXAMPLE; inputMode = "typed"; };

// ------------------------------------------------------------------ server calls
async function api(path, options = {}) {
  const headers = { ...(options.headers || {}), "X-Pin": store.get("pin") };
  const res = await fetch(path, { ...options, headers });
  if (res.status === 401) { go("pin"); throw new Error("PIN needed"); }
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return res;
}
const postJSON = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

// ------------------------------------------------------------------ language pickers
function langPicker(sel, value, onPick) {
  const el = $(sel);
  el.innerHTML = Object.entries(LANGS).map(([k, v]) =>
    `<button role="radio" data-lang="${k}" aria-checked="${k === value}" ${k === "ar" ? 'lang="ar"' : ""}>${v}</button>`).join("");
  el.querySelectorAll("button").forEach((b) => (b.onclick = () => {
    el.querySelectorAll("button").forEach((x) => x.setAttribute("aria-checked", x === b));
    onPick(b.dataset.lang);
  }));
}
let newLang = store.get("lang", "en");
langPicker("#lang-new", newLang, (l) => (newLang = l));

// ------------------------------------------------------------------ dictation (MediaRecorder; iPhone records audio/mp4)
let recorder = null, chunks = [];
$("#mic").onclick = async () => {
  if (recorder && recorder.state === "recording") return recorder.stop();
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    return micStatus("Dictation needs the secure (https) address of the demo. You can still type.");
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const type = ["audio/mp4", "audio/webm;codecs=opus", "audio/webm"].find((t) => MediaRecorder.isTypeSupported(t)) || "";
    recorder = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
    chunks = [];
    recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data);
    recorder.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop());
      $("#mic").setAttribute("aria-pressed", "false");
      $("#mic").textContent = "🎙︎ Dictate";
      const blob = new Blob(chunks, { type: recorder.mimeType || type });
      micStatus("Turning your voice into text…");
      const form = new FormData();
      form.append("audio", blob, (recorder.mimeType || type).includes("mp4") ? "voice.m4a" : "voice.webm");
      try {
        const { text } = await (await api("/api/transcribe", { method: "POST", body: form })).json();
        const box = $("#rx-input");
        box.value = (box.value ? box.value.trim() + "\n" : "") + text;
        inputMode = "voice";
        micStatus("Check the text, then press Explain.");
      } catch (e) { micStatus(`Sorry, I could not turn the recording into text. Why: ${e.message}`); }
    };
    recorder.start();
    $("#mic").setAttribute("aria-pressed", "true");
    $("#mic").textContent = "■ Stop";
    micStatus("Recording… speak the prescription, then press Stop.");
  } catch (e) {
    micStatus("The microphone is not allowed. Allow it in Safari, or type the prescription.");
  }
};
function micStatus(text) { $("#mic-status").textContent = text; show($("#mic-status"), !!text); }

// ------------------------------------------------------------------ explain
$("#explain").onclick = async () => {
  const text = $("#rx-input").value.trim();
  if (!text) return;
  state = { started: performance.now(), lang: newLang };
  go("result");
  show($("#result"), false);
  show($("#preview"), false);
  $("#status").textContent = "Reading the prescription…";
  $("#explain").disabled = true;
  try {
    const res = await postJSON("/api/explain", { text, lang: newLang });
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buffer.indexOf("\n")) >= 0) {
        handle(JSON.parse(buffer.slice(0, nl)));
        buffer = buffer.slice(nl + 1);
      }
    }
  } catch (e) {
    $("#status").textContent = `Sorry, something went wrong. Why: ${e.message}`;
  } finally {
    $("#explain").disabled = false;
  }
};

function handle(ev) {
  if (ev.type === "privacy" && ev.removed.length) {
    $("#status").textContent = `Personal details removed on the server before reading: ${ev.removed.join(", ")}.`;
  } else if (ev.type === "dosing") {
    state.timeToDosing = ev.elapsed;
    $("#preview").innerHTML = formatSheet(ev.lines.join("\n"));
    show($("#preview"), true);
    $("#status").textContent = "Writing the patient sheet…";
  } else if (ev.type === "done") {
    Object.assign(state, { id: ev.id, sheet: ev.sheet, review: ev.review, needsReview: ev.needs_review,
      medicines: ev.medicines, flagsUnsure: ev.flags_unsure, flagsRisk: ev.flags_risk });
    show($("#preview"), false);
    $("#status").textContent = `Ready in ${ev.elapsed}s.`;
    renderResult();
  } else if (ev.type === "error") {
    $("#status").textContent = ev.message;
  }
}

function renderResult() {
  langPicker("#lang-result", state.lang, switchLang);
  paintSheet($("#sheet"), state.sheet);
  const kept = state.sheet.kept_in_english || [];
  $("#kept").textContent = kept.length ? `${kept.length} sentence(s) stayed in English: the translation did not pass the safety check.` : "";
  show($("#kept"), kept.length > 0);
  renderReview();
  $("#confirm").checked = false;
  show($("#confirm-wrap"), state.needsReview);
  $("#approve").disabled = state.needsReview;
  show($("#result"), true);
}

$("#confirm").onchange = () => { $("#approve").disabled = !$("#confirm").checked; };

async function switchLang(lang) {
  if (!state.id || lang === state.lang) return;
  $("#status").textContent = lang === "en" ? "" : "Translating…";
  try {
    state.sheet = await (await postJSON("/api/translate", { id: state.id, lang })).json();
    state.lang = lang;
    paintSheet($("#sheet"), state.sheet);
    const kept = state.sheet.kept_in_english || [];
    $("#kept").textContent = kept.length ? `${kept.length} sentence(s) stayed in English: the translation did not pass the safety check.` : "";
    show($("#kept"), kept.length > 0);
    $("#status").textContent = "";
  } catch (e) { $("#status").textContent = `Sorry, the translation failed. Why: ${e.message}`; }
}

const HEADINGS = new Set(["Your illness", "Your medicines", "How to take them", "What they are for", "When to take them each day",
  "Your doctor also says", "Sorry, I could not understand everything", "Votre maladie", "Vos médicaments",
  "Quand les prendre chaque jour", "Votre médecin dit aussi", "Désolé, je n'ai pas tout compris",
  "مرضك", "أدويتك", "متى تأخذها كل يوم", "يقول طبيبك أيضا", "عذرا، لم أفهم كل شيء"]);
function formatSheet(text) {
  return text.split("\n").map((line) => {
    const t = line.trim();
    if (HEADINGS.has(t)) return `<span class="h">${esc(t)}</span>`;
    if (/^\d+\.\s/.test(t)) return `<span class="med">${esc(t)}</span>`;
    if (/^(Sorry|Désolé|عذرا)/.test(t)) return `<span class="sorry">${esc(line)}</span>`;
    return esc(line);
  }).join("\n");
}
function paintSheet(el, sheet) {
  el.innerHTML = formatSheet(sheet.text);
  el.setAttribute("dir", sheet.rtl ? "rtl" : "ltr");
  el.setAttribute("lang", sheet.lang);
}

function renderReview() {
  const tagOf = (line) => {
    const t = line.trim();
    for (const [key, cls] of [["HIGH RISK", "risk"], ["NOT SURE", "unsure"], ["CORRECTED", "note"], ["PRIVACY", "privacy"], ["AI-ADDED", "ai"]]) {
      if (t.startsWith(key)) return `<span class="tag ${cls}">${key}</span>${esc(t.slice(key.length).trim())}`;
    }
    if (t.startsWith("CHECK BEFORE")) return `<span class="tag risk">CHECK</span>${esc(t.replace("CHECK BEFORE GIVING THIS TO THE PATIENT: ", ""))}`;
    if (t.startsWith("Everything")) return `<span class="tag ok">OK</span>${esc(t)}`;
    return esc(t);
  };
  const lines = state.review.slice(1);
  $("#review-title").textContent = state.needsReview ? "For the doctor — check before giving" : "For the doctor";
  $("#review").innerHTML = lines.map((l) => `<div class="line">${tagOf(l)}</div>`).join("");
  $("#review-box").open = state.needsReview;
}

// ------------------------------------------------------------------ decisions (logged without patient text)
function logDecision(decision, extra = {}) {
  const lines = state.sheet.text.split("\n").filter((l) => l.trim());
  postJSON("/api/decision", {
    id: state.id, doctor: store.get("doctor"), decision, lang: state.lang, input_mode: inputMode,
    seconds: Math.round((performance.now() - state.started) / 100) / 10, time_to_dosing: state.timeToDosing,
    lines_total: lines.length, needs_review: state.needsReview, medicines: state.medicines,
    flags_unsure: state.flagsUnsure, flags_risk: state.flagsRisk, ...extra,
  }).catch(() => {});
}

$("#approve").onclick = () => { logDecision("approved"); finish(state.sheet.text); };
$("#edit").onclick = () => { $("#edit-text").value = state.sheet.text; $("#edit-text").dir = state.sheet.rtl ? "rtl" : "ltr"; go("edit"); };
$("#edit-cancel").onclick = () => go("result");
$("#edit-save").onclick = () => {
  const before = state.sheet.text.split("\n").filter((l) => l.trim());
  const after = new Set($("#edit-text").value.split("\n").map((l) => l.trim()).filter(Boolean));
  const changed = before.filter((l) => !after.has(l.trim())).length;
  logDecision("edited", { lines_changed: changed });
  finish($("#edit-text").value);
};
$("#reject").onclick = () => {
  $("#reasons").innerHTML = REASONS.map((r) => `<button data-r="${esc(r)}">${esc(r)}</button>`).join("");
  $("#reasons").querySelectorAll("button").forEach((b) => (b.onclick = () => {
    logDecision("rejected", { reason: b.dataset.r });
    $("#rx-input").focus();
    go("new");
  }));
  go("reject");
};
$("#reject-cancel").onclick = () => go("result");

const STAMP = {
  en: (d) => `Checked by your doctor on ${d}.`,
  fr: (d) => `Vérifié par votre médecin le ${d}.`,
  ar: (d) => `راجعه طبيبك بتاريخ ${d}.`,
};
function finish(text) {
  const date = new Date().toLocaleDateString("fr-FR");
  state.final = `${text.trim()}\n\n${STAMP[state.lang](date)}`;
  paintSheet($("#final"), { text: state.final, rtl: state.sheet.rtl, lang: state.lang });
  go("done");
}
$("#print").onclick = () => window.print();
$("#share").onclick = async () => {
  if (navigator.share) {
    try { await navigator.share({ title: "Prescription", text: state.final }); } catch {}
  } else {
    try { await navigator.clipboard.writeText(state.final); $("#share").textContent = "Copied"; } catch {}
  }
};

// ------------------------------------------------------------------ audio
// "Listen": the phone's own voice (offline, nothing sent). Sentence by sentence, because
// iOS can stop long texts. "Audio file": an MP3 made on the server, to keep or send.
const VOICE_LANG = { en: "en-US", fr: "fr-FR", ar: "ar-SA" };
function sentencesOf(text) {
  return text.split("\n").map((l) => l.trim().replace(/^\d+\.\s*/, "")).filter(Boolean)
    .flatMap((l) => l.split(/(?<=[.!?؟])\s+/)).filter(Boolean);
}
function stopSpeaking() {
  speechSynthesis.cancel();
  document.querySelectorAll(".listen").forEach((b) => { b.setAttribute("aria-pressed", "false"); b.textContent = "🔊 Listen"; });
}
document.querySelectorAll(".listen").forEach((btn) => (btn.onclick = () => {
  if (!("speechSynthesis" in window)) return alert("This phone cannot read aloud here. Use the audio file.");
  if (btn.getAttribute("aria-pressed") === "true") return stopSpeaking();
  stopSpeaking();
  const lang = state.lang || "en";
  const voice = speechSynthesis.getVoices().find((v) => v.lang.replace("_", "-").toLowerCase().startsWith(lang));
  const text = btn.dataset.target === "final" ? state.final : state.sheet.text;
  const parts = sentencesOf(text);
  btn.setAttribute("aria-pressed", "true");
  btn.textContent = "■ Stop";
  parts.forEach((part, i) => {
    const u = new SpeechSynthesisUtterance(part);
    u.lang = VOICE_LANG[lang];
    if (voice) u.voice = voice;
    u.rate = 0.9;  // a little slower for patients
    if (i === parts.length - 1) u.onend = stopSpeaking;
    speechSynthesis.speak(u);
  });
}));
window.speechSynthesis?.getVoices();  // iOS loads voices lazily

$("#audio-file").onclick = async () => {
  const btn = $("#audio-file");
  btn.disabled = true;
  btn.textContent = "Making the audio…";
  try {
    const blob = await (await postJSON("/api/audio", { text: state.final, lang: state.lang })).blob();
    const file = new File([blob], `prescription-${state.lang}.mp3`, { type: "audio/mpeg" });
    if (navigator.canShare?.({ files: [file] })) {
      await navigator.share({ files: [file], title: "Prescription (audio)" });
    } else {
      const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: file.name });
      a.click();
    }
  } catch (e) {
    if (e.name !== "AbortError") alert(`Sorry, the audio could not be made. Why: ${e.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = "♫ Audio file";
  }
};

// ------------------------------------------------------------------ stats
async function loadStats() {
  try {
    const s = await (await api("/api/stats")).json();
    const pct = (a, b) => (b ? `${Math.round((a / b) * 100)}%` : "–");
    const tile = (label, value, note = "") => `<div class="tile"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div><div class="note">${esc(note)}</div></div>`;
    $("#stats").innerHTML = [
      tile("Prescriptions", s.total, `${s.doctors} doctor(s) · ${s.voice} by voice`),
      tile("Approved as is", pct(s.approved, s.total), `${s.approved} of ${s.total}`),
      tile("Edited", pct(s.edited, s.total), s.edit_share != null ? `${Math.round(s.edit_share * 100)}% of lines changed on average` : ""),
      tile("Rejected", pct(s.rejected, s.total), `${s.rejected} of ${s.total}`),
      tile("Time per prescription", s.median_seconds != null ? `${s.median_seconds}s` : "–", "median, from Explain to decision"),
      tile("Flags were right", pct(s.flagged_changed, s.flagged), `${s.flagged_changed} of ${s.flagged} flagged ones needed changes`),
      tile("Clean ones were right", pct(s.clean - s.clean_changed, s.clean), `${s.clean - s.clean_changed} of ${s.clean} unflagged approved as is`),
      tile("Languages", Object.entries(s.by_language).map(([k, v]) => `${k.toUpperCase()} ${v}`).join(" · ")),
    ].join("");
    const reasons = Object.entries(s.reasons);
    $("#reasons-table").innerHTML = reasons.length
      ? `<h1>Why rejected</h1><table>${reasons.map(([r, n]) => `<tr><td>${esc(r)}</td><td class="num">${n}</td></tr>`).join("")}</table>` : "";
  } catch (e) { $("#stats").innerHTML = `<p class="error">${esc(e.message)}</p>`; }
}

// ------------------------------------------------------------------ settings & PIN
function fillSettings() {
  $("#doctor").value = store.get("doctor");
  $("#pin-setting").value = store.get("pin");
  langPicker("#lang-default", store.get("lang", "en"), (l) => (fillSettings.lang = l));
}
$("#settings-save").onclick = () => {
  store.set("doctor", $("#doctor").value.trim());
  store.set("pin", $("#pin-setting").value.trim());
  if (fillSettings.lang) { store.set("lang", fillSettings.lang); newLang = fillSettings.lang; langPicker("#lang-new", newLang, (l) => (newLang = l)); }
  go("new");
};
$("#pin-go").onclick = async () => {
  store.set("pin", $("#pin-input").value.trim());
  try { await api("/api/check-pin", { method: "POST" }); show($("#pin-error"), false); go("new"); }
  catch { show($("#pin-error"), true); }
};

(async () => {
  try {
    const c = await (await api("/api/config")).json();
    $("#about").textContent = `AI: ${c.provider} · ${c.model}. Patient text is never stored; only decisions are logged.`;
    if (c.pin) {
      try { await api("/api/check-pin", { method: "POST" }); } catch { /* api() already showed the PIN screen */ }
    }
  } catch (e) { $("#about").textContent = `Server not reachable: ${e.message}`; }
})();
