"use strict";
/* AgentFlight panel client.
 * Events from the server (SSE) build the run state; the UI is drawn from that state.
 * A run = one program process (started in a terminal, by an editor extension or by the panel itself);
 * its agent calls are kept as a tree. */

const TOKEN = document.querySelector('meta[name="agentflight-token"]').content;
const VERSION = document.querySelector('meta[name="agentflight-version"]').content;
const EMBEDDED = (() => { try { return window.parent !== window; } catch { return true; } })();  // inside an editor extension
const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => Array.from(root.querySelectorAll(s));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const api = {
  async get(path) {
    const r = await fetch(path);
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || r.statusText);
    return j;
  },
  async post(path, data) {
    const r = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-AgentFlight-Token": TOKEN },
      body: JSON.stringify(data || {}),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || r.statusText);
    return j;
  },
};

// ---------------------------------------------------------------- state
const state = {
  runs: new Map(),              // sid -> run
  order: [],                    // sids in arrival order
  selected: null,               // sid of the run shown
  selectedCall: null,
  replayingHistory: false,
  approvalQueue: [],
  openApproval: null,
  tab: "steps",
  accounts: null,
  settings: null,
};

const dirty = { tree: true, steps: true, eval: true, counters: true };

const TABS = ["steps", "output", "eval", "history"];
const SETTINGS_TABS = ["accounts", "keys", "general", "shortcuts", "about"];
const EMPTY_HINT = "Run <code>python main.py</code> or <code>agentflight run main.py</code> in a terminal; agents show up here live.";

function newRun(o) {
  return {
    sid: o.sid, kind: o.kind || "program", path: o.path, file: o.file, model: o.model || "", replay: o.replay,
    external: !!o.external, python: o.python || null, project: o.project || null, command: o.command || "",
    envModel: o.env_model || null, step: o.step, runId: null, status: "starting", startedAt: Date.now(), durationMs: 0,
    cost: 0, tokens: 0, calls: new Map(), roots: [], output: [], approvals: new Map(), evals: new Map(),
    error: null, processEnded: false, fromHistory: !!o.from_history, globalPaused: false, diagnostics: [],
  };
}

function selectedRun() { return state.runs.get(state.selected) || null; }

function isLive(run) { return run && !run.processEnded && !run.fromHistory; }

// ---------------------------------------------------------------- helpers
function duration(ms) {
  if (ms == null) return "";
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}
function money(x) { return `$${(x || 0).toFixed(4)}`; }
function plural(n, word) { return `${n} ${word}${n === 1 ? "" : "s"}`; }
function percent(rate) { return `${Math.round(rate * 100)}%`; }
function count(n) { return (n || 0).toLocaleString("en-US"); }
function short(value, n = 80) {
  let s = typeof value === "string" ? value : JSON.stringify(value);
  s = (s || "").replace(/\s+/g, " ").trim();
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}
function basename(path) { return String(path || "").split(/[\\/]/).pop(); }
function valueText(value) { return typeof value === "string" ? value : JSON.stringify(value, null, 2); }
function jsonHtml(value) {
  if (typeof value === "string") return `<pre class="json">${esc(value)}</pre>`;
  const text = JSON.stringify(value, null, 2) ?? "null";
  const colored = esc(text).replace(
    /(&quot;(?:\\.|[^&]|&(?!quot;))*?&quot;)(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?/g,
    (m, str, colon, bool) => {
      if (str) return colon ? `<span class="k">${str}</span>${colon}` : `<span class="s">${str}</span>`;
      if (bool) return `<span class="b">${bool}</span>`;
      return `<span class="n">${m}</span>`;
    });
  return `<pre class="json">${colored}</pre>`;
}
function toast(html, kind = "") {
  const t = document.createElement("div");
  t.className = `toast ${kind}`;
  t.innerHTML = html;
  $("#toasts").appendChild(t);
  setTimeout(() => t.remove(), kind === "error" ? 7000 : 4200);
}
function storageGet(key, fallback) { try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; } }
function storageSet(key, value) { try { localStorage.setItem(key, value); } catch { /* ignore */ } }
function openExternal(url) { window.open(url, "_blank", "noopener"); }
function copyText(text) {
  if (!navigator.clipboard) { toast("Could not copy.", "error"); return Promise.resolve(false); }
  return navigator.clipboard.writeText(text).then(
    () => { toast(`Copied: <code>${esc(text)}</code>`); return true; },
    () => { toast("Could not copy.", "error"); return false; });
}

// Opens a file in the user's editor: JetBrains plugin bridge, VS Code extension (iframe) or an editor URL.
function openInEditor(path, line) {
  if (!path) return;
  if (typeof window.agentFlightEditor === "function") {  // JetBrains plugin (JCEF bridge)
    window.agentFlightEditor(`${line || 1}|${path}`);
    return;
  }
  if (EMBEDDED) {  // VS Code / Cursor extension (iframe inside a webview)
    window.parent.postMessage({ agentflight: "open", file: path, line: line || 1 }, "*");
    return;
  }
  const editor = storageGet("agentflight.editor", "vscode");
  const slashed = String(path).replace(/\\/g, "/");
  const urls = {
    vscode: `vscode://file/${encodeURI(slashed)}:${line || 1}`,
    cursor: `cursor://file/${encodeURI(slashed)}:${line || 1}`,
    pycharm: `pycharm://open?file=${encodeURIComponent(path)}&line=${line || 1}`,
  };
  if (urls[editor]) { window.location.href = urls[editor]; return; }
  copyText(`${path}${line ? ":" + line : ""}`);
}

function linkify(text) {
  return esc(text).replace(/https?:\/\/[^\s<]+[^\s<.,;:)'"]/g, (u) => `<a href="#" data-external="${u}">${u}</a>`);
}
document.addEventListener("click", (e) => {
  const a = e.target.closest && e.target.closest("[data-external]");
  if (a) { e.preventDefault(); openExternal(a.dataset.external); }
});

const STATUS_LABELS = {
  running: "running", done: "done", error: "error", paused: "paused", stopped: "stopped", approval: "awaiting approval",
  rejected: "rejected", cancelled: "cancelled", check_failed: "check failed", starting: "starting",
};
function statusLabel(status) { return STATUS_LABELS[status] || status; }

const DECISION_LABELS = { approve: "approved", reject: "rejected", edit: "edited and approved" };

// ---------------------------------------------------------------- commands
function sendCommand(command) {
  const run = selectedRun();
  if (!isLive(run)) return Promise.resolve();
  return api.post("/api/command", { sid: run.sid, ...command }).catch((e) => toast(esc(e.message), "error"));
}

function stopProgram() {
  const run = selectedRun();
  if (isLive(run)) api.post("/api/stop", { sid: run.sid }).catch((e) => toast(esc(e.message), "error"));
}

function togglePauseAll() {
  const run = selectedRun();
  if (isLive(run)) sendCommand({ command: run.globalPaused ? "resume" : "pause" });
}

function replayFromStep(step) {
  const run = selectedRun();
  if (!run) return;
  api.post("/api/run", { file: run.file, python: run.python, replay: run.runId, step, env_model: run.envModel || undefined })
    .catch((e) => toast("Could not replay: " + esc(e.message), "error"));
  toast(`Replaying from step ${step}; earlier steps come from the record.`);
}

// ---------------------------------------------------------------- events
function handleEvent(o) {
  const sid = o.sid;
  if (o.type === "account_changed") { accountChanged(o); return; }
  if (o.type === "history_start") { state.replayingHistory = true; return; }
  if (o.type === "history_end") {
    state.replayingHistory = false;
    for (const run of state.runs.values()) {
      if (!isLive(run)) continue;
      for (const approval of run.approvals.values()) if (!approval.decision) queueApproval(run, approval);
    }
    markAllDirty();
    return;
  }
  if (o.type === "process_started") {
    const run = newRun(o);
    state.runs.set(sid, run);
    state.order.push(sid);
    if (!state.replayingHistory || !state.selected) state.selected = sid;
    if (!state.replayingHistory) {
      state.selectedCall = null;
      $("#console").innerHTML = "";
      consoleLine(run, `▶ ${o.path}${o.command ? "  (" + o.command + ")" : ""}`, "system");
      if (run.kind === "eval") selectTab("eval");
    }
    renderRunSelect();
    markAllDirty();
    return;
  }
  const run = state.runs.get(sid);
  if (!run) return;

  switch (o.type) {
    case "run_started":
      run.runId = o.run_id; run.status = "running"; run.model = o.model || run.model; run.file = o.file;
      renderRunSelect();
      break;
    case "diagnostics":
      run.diagnostics = o.diagnostics || [];
      break;
    case "call_started": {
      const call = {
        id: o.id, step: o.step, parent: o.parent, agent: o.agent, method: o.method, model: o.model, input: o.input,
        line: o.line, file: o.file, status: "running", text: "", tools: [], formatErrors: [], interjections: [],
        result: null, durationMs: null, cost: 0, inputTokens: 0, outputTokens: 0, turn: 0, fromRecord: false,
        error: null, subscription: !!o.subscription, children: [], system: o.system, signature: o.signature,
        messages: null, instructionsFile: o.instructions_file, returns: o.returns,
      };
      run.calls.set(o.id, call);
      const parent = o.parent && run.calls.get(o.parent);
      (parent ? parent.children : run.roots).push({ kind: "call", id: o.id });
      if (!state.replayingHistory && (!state.selectedCall || !run.calls.has(state.selectedCall)) && sid === state.selected) {
        state.selectedCall = o.id;
      }
      dirty.tree = dirty.steps = dirty.counters = true;
      break;
    }
    case "token": {
      const call = run.calls.get(o.id);
      if (call) {
        call.text += o.text;
        if (call.text.length > 4000) call.text = call.text.slice(-3000);
        updateStream(call);
      }
      return;
    }
    case "turn_started": {
      const call = run.calls.get(o.id);
      if (call) { call.turn = o.turn; if (o.turn > 1) call.text = ""; }
      dirty.tree = true;
      break;
    }
    case "turn_finished": {
      const call = run.calls.get(o.id);
      if (call) { call.inputTokens += o.input_tokens || 0; call.outputTokens += o.output_tokens || 0; call.cost += o.cost || 0; }
      run.cost = o.total_cost ?? run.cost;
      run.tokens += (o.input_tokens || 0) + (o.output_tokens || 0);
      dirty.tree = dirty.counters = true;
      break;
    }
    case "tool_started": {
      const call = run.calls.get(o.id);
      if (call) call.tools.push({ toolCallId: o.tool_call_id, name: o.name, arguments: o.arguments, state: "pending" });
      dirty.tree = dirty.steps = true;
      break;
    }
    case "tool_finished": {
      const call = run.calls.get(o.id);
      const tool = call && call.tools.find((t) => t.toolCallId === o.tool_call_id);
      if (tool) Object.assign(tool, { state: o.error ? "error" : "ok", result: o.result, durationMs: o.duration_ms });
      dirty.tree = dirty.steps = true;
      break;
    }
    case "format_error": {
      const call = run.calls.get(o.id);
      if (call) call.formatErrors.push({ errors: o.errors, attempt: o.attempt, maxAttempts: o.max_attempts, text: o.text });
      dirty.tree = dirty.steps = true;
      break;
    }
    case "state": {
      const call = run.calls.get(o.id);
      if (call && (call.status === "running" || call.status === "paused")) call.status = o.state;
      dirty.tree = dirty.steps = true;
      break;
    }
    case "model_switched": {
      const call = run.calls.get(o.id);
      if (call) call.model = o.new;
      consoleLine(run, `⚠ ${o.old} unavailable (${short(o.reason, 120)}); continuing with ${o.new}`, "stderr");
      if (!state.replayingHistory) toast(`<b>${esc(o.old)}</b> is unavailable; continuing with <b>${esc(o.new)}</b>.`);
      dirty.tree = dirty.steps = true;
      break;
    }
    case "message_added": {
      const call = run.calls.get(o.id);
      if (call) call.interjections.push(o.text);
      dirty.tree = true;
      break;
    }
    case "command_received":
      if (o.command === "pause" && !o.id) run.globalPaused = true;
      if (o.command === "resume" && !o.id) run.globalPaused = false;
      dirty.counters = true;
      break;
    case "call_finished": {
      const call = run.calls.get(o.id);
      if (call) {
        Object.assign(call, {
          status: o.status, result: o.result, durationMs: o.duration_ms, fromRecord: !!o.from_record, error: o.error || null,
          messages: o.messages || call.messages,
        });
        if (o.input_tokens != null) { call.inputTokens = o.input_tokens; call.outputTokens = o.output_tokens; }
        if (o.cost != null) call.cost = o.cost;
        if (o.turns != null) call.turn = o.turns;
      }
      dirty.tree = dirty.steps = dirty.counters = true;
      break;
    }
    case "parallel": {
      const parent = o.parent && run.calls.get(o.parent);
      (parent ? parent.children : run.roots).push({ kind: "parallel", n: o.n, line: o.line });
      dirty.tree = true;
      break;
    }
    case "approval_requested": {
      const approval = {
        id: o.approval_id, message: o.message, value: o.value, line: o.line, file: o.file, decision: null, editable: o.editable,
      };
      run.approvals.set(o.approval_id, approval);
      const parent = o.parent && run.calls.get(o.parent);
      (parent ? parent.children : run.roots).push({ kind: "approval", id: o.approval_id });
      if (!state.replayingHistory && isLive(run)) queueApproval(run, approval);
      dirty.tree = dirty.counters = true;
      break;
    }
    case "approval_answered": {
      const approval = run.approvals.get(o.approval_id);
      if (approval) approval.decision = o.decision;
      if (state.openApproval && state.openApproval.approval.id === o.approval_id) closeApproval();
      state.approvalQueue = state.approvalQueue.filter((x) => x.approval.id !== o.approval_id);
      dirty.tree = dirty.counters = true;
      break;
    }
    case "program_error":
      run.error = o;
      consoleLine(run, `✗ ${o.error_type}${o.line ? ` (${basename(o.file)}:${o.line})` : ""}: ${o.message}`, "error");
      dirty.tree = true;
      break;
    case "eval_started":
      run.evals.set(o.name, { name: o.name, target: o.target, n: o.n, threshold: o.threshold, samples: [], done: false });
      dirty.eval = true;
      break;
    case "eval_sample": {
      const ev = run.evals.get(o.name);
      if (ev) ev.samples.push(o);
      dirty.eval = true;
      break;
    }
    case "eval_finished": {
      const ev = run.evals.get(o.name);
      if (ev) Object.assign(ev, o, { done: true });
      dirty.eval = true;
      if (!state.replayingHistory) {
        toast(`Eval <b>${esc(o.name)}</b>: ${o.passed_count}/${o.sample_count} — ${o.passed ? "passed" : "failed"}`, o.passed ? "success" : "error");
      }
      break;
    }
    case "run_finished":
      run.status = o.status; run.durationMs = o.duration_ms; run.cost = o.total_cost; run.tokens = o.total_tokens;
      if (o.run_id) run.runId = o.run_id;
      dirty.tree = dirty.counters = true;
      break;
    case "output":
      run.output.push({ stream: o.stream, text: o.text });
      if (sid === state.selected) consoleLine(run, o.text, o.stream);
      break;
    case "process_finished":
      run.processEnded = true;
      if (run.status === "running" || run.status === "starting") run.status = o.exit_code === 0 ? "done" : "error";
      for (const call of run.calls.values()) if (call.status === "running" || call.status === "paused") call.status = "stopped";
      state.approvalQueue = state.approvalQueue.filter((x) => x.run !== run);
      if (state.openApproval && state.openApproval.run === run) closeApproval();
      if (!state.replayingHistory) {
        loadHistory();
        consoleLine(run, `■ ${statusLabel(run.status)} (exit code ${o.exit_code ?? "?"})`, "faint");
      }
      dirty.tree = dirty.steps = dirty.counters = true;
      renderRunSelect();
      break;
    default:
      return;
  }
  draw();
}

function markAllDirty() { dirty.tree = dirty.steps = dirty.eval = dirty.counters = true; draw(); }

// ---------------------------------------------------------------- drawing loop
let drawPending = false;
let lastDraw = 0;
function draw() {
  if (drawPending) return;
  drawPending = true;
  const wait = Math.max(0, 60 - (performance.now() - lastDraw));
  setTimeout(() => requestAnimationFrame(() => {
    drawPending = false;
    lastDraw = performance.now();
    if (state.replayingHistory) return;
    if (dirty.tree) { renderTree(); dirty.tree = false; }
    if (dirty.steps && state.tab === "steps") { renderSteps(); dirty.steps = false; }
    if (dirty.eval && state.tab === "eval") { renderEval(); dirty.eval = false; }
    if (dirty.counters) { renderCounters(); dirty.counters = false; }
    renderComposer();
  }), wait);
}

const streamFrames = new Map();
function updateStream(call) {
  if (streamFrames.has(call.id) || state.replayingHistory) return;
  streamFrames.set(call.id, requestAnimationFrame(() => {
    streamFrames.delete(call.id);
    const box = document.querySelector(`[data-stream="${CSS.escape(call.id)}"]`);
    if (box) box.firstElementChild.firstElementChild.textContent = call.text.slice(-700);
    else { dirty.tree = true; draw(); }
  }));
}

// agents panel: call tree
function callHtml(run, call) {
  const selected = call.id === state.selectedCall ? " selected" : "";
  const meta = [];
  if (!call.fromRecord) {
    if (call.subscription) meta.push("subscription");
    else if (call.cost) meta.push(money(call.cost));
    if (call.durationMs != null) meta.push(duration(call.durationMs));
    else if (call.status === "running" && call.turn > 1) meta.push(`turn ${call.turn}`);
  }
  const pill = call.fromRecord ? '<span class="pill from-record">from record</span>'
    : `<span class="pill ${esc(call.status)}">${esc(statusLabel(call.status))}</span>`;
  let body = "";
  for (const t of call.tools) {
    const mark = t.state === "ok" ? '<span class="mark ok">✓</span>'
      : t.state === "error" ? '<span class="mark error">✗</span>' : '<span class="mark pending">⏳</span>';
    body += `<div class="tool-row">${mark}<span class="name">${esc(t.name)}(${esc(short(t.arguments, 60))})</span><span class="time">${t.durationMs != null ? duration(t.durationMs) : "running"}</span></div>`;
  }
  for (const f of call.formatErrors) body += `<div class="format-box">⚠ format error (${f.attempt}/${f.maxAttempts}): ${esc((f.errors || [])[0] || "")} → asked for a fix</div>`;
  for (const m of call.interjections) body += `<div class="interjection">✉ ${esc(m)}</div>`;
  if ((call.status === "running" || call.status === "paused") && !call.fromRecord) {
    body += `<div class="stream-box" data-stream="${esc(call.id)}"><div><span>${esc(call.text.slice(-700))}</span>${call.status === "running" ? '<span class="cursor"></span>' : ""}</div></div>`;
  }
  if (call.status === "done" && call.result != null) body += `<div class="result-preview"><span class="arrow">→</span> ${esc(short(call.result, 90))}</div>`;
  if (call.error) body += `<div class="error-line">${linkify(call.error)}</div>`;
  for (const child of call.children) body += itemHtml(run, child);
  return `<div class="node${selected}" data-call="${esc(call.id)}">
    <div class="node-head"><span class="dot ${esc(call.fromRecord ? "done" : call.status)}"></span>
      <span class="node-name">${esc(call.agent)}<span class="method">.${esc(call.method)}</span></span>${pill}
      <span class="node-meta">${meta.join(" · ")}</span></div>
    <div class="node-input">#${call.step} · ${esc(short(call.input, 70))}</div>
    <div class="node-body">${body}</div></div>`;
}

function itemHtml(run, item) {
  if (item.kind === "call") { const call = run.calls.get(item.id); return call ? callHtml(run, call) : ""; }
  if (item.kind === "parallel") return `<div class="parallel-label">⇉ parallel · ${plural(item.n, "task")}${item.line ? ` · line ${item.line}` : ""}</div>`;
  if (item.kind === "approval") {
    const approval = run.approvals.get(item.id);
    if (!approval) return "";
    const label = approval.decision ? DECISION_LABELS[approval.decision] || approval.decision : "awaiting approval";
    const status = approval.decision ? (approval.decision === "reject" ? "error" : "done") : "approval";
    return `<div class="node approval-node" data-approval="${esc(approval.id)}"><div class="node-head"><span class="dot ${status}"></span>
      <span class="node-name">⏸ ${esc(approval.message)}</span><span class="pill ${status}">${esc(label)}</span>
      <span class="node-meta">${approval.line ? `line ${approval.line}` : ""}</span></div>
      <div class="node-input">${esc(short(approval.value, 80))}</div></div>`;
  }
  return "";
}

function renderTree() {
  const box = $("#agentTree");
  const run = selectedRun();
  $("#pulse").classList.toggle("active", !!(run && isLive(run)));
  if (!run || (!run.roots.length && !run.error)) {
    const text = run ? (isLive(run) ? "The program started; waiting for an agent call…" : "No agent calls in this run.")
      : "No agents have run yet.";
    box.innerHTML = `<div class="empty-state">${text}<br><span>${EMPTY_HINT}</span></div>`;
    return;
  }
  let html = run.roots.map((item) => itemHtml(run, item)).join("");
  if (run.error) {
    html += `<div class="error-box" style="margin-top:8px">✗ ${esc(run.error.error_type)}: ${esc(run.error.message)}${run.error.line ? ` (line ${run.error.line})` : ""}</div>`;
  }
  box.innerHTML = html;
}

function renderRunSelect() {
  const select = $("#runSelect");
  const options = [...state.order].reverse().slice(0, 30).map((sid) => {
    const run = state.runs.get(sid);
    const label = `${run.fromHistory ? "↺ " : ""}${run.path || ""}${run.kind === "eval" ? " (eval)" : ""} · ${statusLabel(run.status)}`;
    return `<option value="${esc(sid)}"${sid === state.selected ? " selected" : ""}>${esc(label)}</option>`;
  });
  select.innerHTML = options.join("") || '<option value="">no runs</option>';
}

function renderComposer() {
  const run = selectedRun();
  const call = run && state.selectedCall ? run.calls.get(state.selectedCall) : null;
  const live = !!(call && isLive(run) && (call.status === "running" || call.status === "paused"));
  $("#composerTarget").textContent = call ? `Selected: #${call.step} ${call.agent}.${call.method} · ${statusLabel(call.status)}` : "Select an agent";
  const pause = $("#agentPauseBtn");
  pause.disabled = !live;
  pause.textContent = call && call.status === "paused" ? "▶ Resume" : "⏸ Pause";
  $("#agentKillBtn").disabled = !live;
  $("#messageInput").disabled = !live;
  $("#sendBtn").disabled = !live;
}

function renderCounters() {
  const run = selectedRun();
  const pill = $("#statusPill");
  let text = "ready", cls = "idle";
  if (run) {
    const live = isLive(run);
    const awaiting = live && [...run.approvals.values()].some((a) => !a.decision);
    const paused = run.globalPaused || [...run.calls.values()].some((c) => c.status === "paused");
    if (live) {
      cls = awaiting ? "approval" : paused ? "paused" : "running";
      text = statusLabel(cls);
    } else {
      text = statusLabel(run.status);
      cls = run.status === "done" ? "done" : ["error", "rejected", "check_failed"].includes(run.status) ? "error" : "idle";
    }
    $("#counterTime").textContent = duration(live ? Date.now() - run.startedAt : run.durationMs);
    $("#counterTokens").textContent = `${count(run.tokens)} tokens`;
    $("#counterCost").textContent = money(run.cost);
  }
  pill.textContent = text;
  pill.className = `status-pill ${cls}`;
  const bar = $("#statusbar");
  bar.classList.toggle("running", cls === "running" || cls === "approval" || cls === "paused");
  bar.classList.toggle("error", cls === "error");
  $("#sbRun").textContent = run ? `${run.kind === "eval" ? "eval" : "▶"} ${basename(run.path)} · ${text}` : "";
  const live = isLive(run);
  $("#stopBtn").disabled = !live;
  $("#pauseAllBtn").disabled = !live;
  $("#pauseAllBtn").textContent = run && run.globalPaused ? "▶ Resume" : "⏸ Pause";
}
setInterval(() => { if (isLive(selectedRun())) { dirty.counters = true; draw(); } }, 500);

// details: steps
const STEPS_HINT = "Every agent call shows up here as a step. Click a step to see the prompt sent to the model and the answer "
  + "that came back; fix the instructions and <b>replay from that step</b> (earlier steps come from the record, the model "
  + "is not called again).";

function renderSteps() {
  const box = $("#stepsPage");
  const run = selectedRun();
  if (!run || !run.calls.size) { box.innerHTML = `<div class="empty-note">${STEPS_HINT}</div>`; return; }
  const steps = [...run.calls.values()].sort((a, b) => a.step - b.step);
  const scroll = box.querySelector(".step-strip")?.scrollLeft || 0;
  let strip = '<div class="step-strip">';
  for (const call of steps) {
    const cls = call.status === "error" ? "error" : call.formatErrors.length && call.status !== "done" ? "format" : call.status === "stopped" ? "stopped" : "";
    const status = call.fromRecord ? '<span class="step-status from-record">from record</span>'
      : `<span class="step-status ${esc(call.status)}">${esc(statusLabel(call.status))}${call.durationMs != null ? " · " + duration(call.durationMs) : ""}${call.formatErrors.length ? ` · ${call.formatErrors.length} fixes` : ""}</span>`;
    strip += `<div class="step-card ${cls}${call.id === state.selectedCall ? " selected" : ""}" data-call="${esc(call.id)}">
      <span class="step-no">STEP ${call.step}</span><span class="step-name">${esc(call.agent)}.${esc(call.method)}</span>${status}</div>`;
  }
  strip += "</div>";
  const call = run.calls.get(state.selectedCall) || steps[steps.length - 1];
  box.innerHTML = strip + stepDetail(run, call);
  const s = box.querySelector(".step-strip");
  if (s) s.scrollLeft = scroll;
}

function messageHtml(m) {
  if (m.role === "tool") {
    return (m.results || []).map((r) => `<div class="msg tool"><div class="msg-role">TOOL RESULT · ${esc(r.name)}${r.error ? " (error)" : ""}</div><div class="msg-text">${esc(r.content)}</div></div>`).join("");
  }
  const role = m.role === "assistant" ? "MODEL" : "USER";
  let body = m.content ? `<div class="msg-text">${esc(m.content)}</div>` : "";
  for (const t of m.tool_calls || []) body += `<div class="msg-text">→ ${esc(t.name)}(${esc(JSON.stringify(t.arguments))})</div>`;
  return `<div class="msg ${esc(m.role)}"><div class="msg-role">${role}</div>${body}</div>`;
}

function stepDetail(run, call) {
  if (!call) return "";
  const tokens = call.inputTokens + call.outputTokens;
  const meta = [
    call.model,
    call.turn ? `${call.turn} turn${call.turn === 1 ? "" : "s"}` : "",
    tokens ? `${count(tokens)} tokens` : "",
    call.subscription ? "subscription" : (call.cost ? money(call.cost) : ""),
    call.durationMs != null ? duration(call.durationMs) : "",
  ].filter(Boolean).join(" · ");
  const canReplay = run.runId && run.kind === "program" && !isLive(run) && run.path && run.file && run.python;
  const sent = call.messages && call.messages.length
    ? call.messages.map(messageHtml).join("")
    : `<div class="msg user"><div class="msg-role">INPUT</div>${jsonHtml(call.input)}</div>`;
  let received = "";
  for (const f of call.formatErrors) received += `<div class="format-box">⚠ Attempt ${f.attempt}: did not match the schema\n${esc((f.errors || []).join("\n"))}</div>`;
  if (call.error) received += `<div class="error-box">${linkify(call.error)}</div>`;
  else if (call.status === "done") received += jsonHtml(call.result);
  else if (call.status === "stopped") received += '<div class="empty-note">The agent was stopped; no result (None).</div>';
  else received += `<pre class="json">${esc(call.text.slice(-3000))}</pre>`;
  return `<div class="step-detail">
    <div class="detail-head"><h3>STEP ${call.step} · ${esc(call.agent)}.${esc(call.method)}</h3>
      <span class="detail-meta">${esc(call.signature || "")}</span><span class="detail-meta">${esc(meta)}</span><span class="spacer"></span>
      ${call.instructionsFile ? `<button class="btn small" data-action="instructions" data-file="${esc(call.instructionsFile)}">✎ Open instructions</button>` : ""}
      ${call.line && call.file ? `<button class="btn small" data-action="line" data-file="${esc(call.file)}" data-line="${call.line}">↗ Line ${call.line}</button>` : ""}
      ${canReplay ? `<button class="btn small primary" data-action="replay" data-step="${call.step}">↻ Replay from this step</button>` : ""}
    </div>
    <div class="detail-body">
      <div class="detail-col"><div class="col-title">SENT</div>
        ${call.system ? `<details class="system-prompt"><summary>System prompt (instructions + task + schema)</summary><div class="msg-text">${esc(call.system)}</div></details>` : ""}
        ${sent}</div>
      <div class="detail-col"><div class="col-title">RECEIVED${call.returns ? " · " + esc(call.returns) : ""}</div>${received}</div>
    </div></div>`;
}

// details: eval
function renderEval() {
  const box = $("#evalPage");
  let run = selectedRun();
  if (!run || !run.evals.size) run = [...state.runs.values()].reverse().find((r) => r.evals.size) || null;
  if (!run) {
    box.innerHTML = '<div class="empty-note">Eval results show up here: <code>agentflight eval main.py</code></div>';
    return;
  }
  let html = "<table class=\"eval-table\"><tr><th>EVAL</th><th>TARGET</th><th>RESULT</th><th>SCORE</th><th>THRESHOLD</th><th>COST</th><th>TIME</th></tr>";
  for (const ev of run.evals.values()) {
    const passed = ev.samples.filter((s) => s.passed).length;
    const rate = ev.samples.length ? passed / ev.samples.length : 0;
    const result = ev.done ? (ev.passed ? '<span class="pass">PASS</span>' : '<span class="fail">FAIL</span>')
      : `<span class="detail-meta">${ev.samples.length}/${ev.n} running…</span>`;
    html += `<tr class="summary-row"><td><b>${esc(ev.name)}</b></td><td class="mono">${esc(ev.target || "")}</td><td>${result}</td>
      <td>${passed}/${ev.samples.length} · ${percent(rate)}</td><td>${percent(ev.threshold || 0)}</td>
      <td>${ev.done ? money(ev.cost) : ""}</td><td>${ev.done ? duration(ev.duration_ms) : ""}</td></tr>`;
    for (const s of ev.samples) {
      html += `<tr class="sample-row"><td>${s.passed ? '<span class="pass">✓</span>' : '<span class="fail">✗</span>'} #${s.index + 1}</td>
        <td colspan="3">${esc(short(s.input, 90))}${s.note ? `<br><span style="color:var(--orange)">${esc(s.note)}</span>` : ""}</td>
        <td colspan="3">${esc(short(s.result, 90))}</td></tr>`;
    }
  }
  box.innerHTML = html + "</table>";
}

// details: history
async function loadHistory() {
  try {
    const list = await api.get("/api/runs");
    const box = $("#historyPage");
    if (!list.length) {
      box.innerHTML = '<div class="empty-note">Every run is recorded under <code>.agentflight/runs</code> and listed here.</div>';
      return;
    }
    box.innerHTML = list.map((r) => {
      const color = r.status === "done" ? "var(--green)" : r.status === "running" ? "var(--orange)" : "var(--red)";
      const project = r.project ? `<span class="location">${esc(basename(r.project))}/</span>` : "";
      return `<div class="list-item" data-run-id="${esc(r.run_id)}" data-project="${esc(r.project || "")}">
        <span class="history-status" style="color:${color}">${esc(statusLabel(r.status || "?"))}</span>
        <span class="location">${esc(r.run_id)}</span><span>${project}<b>${esc(r.file_name || "")}</b>${r.kind === "eval" ? " (eval)" : ""}</span>
        <span class="location" style="margin-left:auto">${[plural(r.call_count || 0, "call"), money(r.total_cost), esc(r.model || r.env_model || "")].filter(Boolean).join(" · ")}</span></div>`;
    }).join("");
  } catch { /* silent */ }
}

async function openRecord(runId, project = "") {
  try {
    const record = await api.get(`/api/run?run_id=${encodeURIComponent(runId)}${project ? "&project=" + encodeURIComponent(project) : ""}`);
    const sid = `r:${runId}`;
    const summary = record.summary || {};
    if (state.runs.has(sid)) { state.runs.delete(sid); state.order = state.order.filter((x) => x !== sid); }
    const before = state.replayingHistory;
    state.replayingHistory = true;
    handleEvent({
      type: "process_started", sid, kind: summary.kind, path: summary.file_name || basename(summary.file), file: summary.file,
      model: summary.model, from_history: true, python: summary.python, project: project || null, env_model: summary.env_model,
    });
    for (const e of record.events) handleEvent({ ...e, sid });
    const run = state.runs.get(sid);
    run.processEnded = true;
    run.runId = runId;
    if (run.status === "running") run.status = summary.status || "done";
    state.replayingHistory = before;
    state.selected = sid;
    state.selectedCall = null;
    renderRunSelect();
    redrawConsole();
    selectTab("steps");
    markAllDirty();
    toast(`Opened run <code>${esc(runId)}</code>. Pick a step and choose “Replay from this step”.`);
  } catch (e) { toast("Could not open the run: " + esc(e.message), "error"); }
}

// details: output
function consoleLine(run, text, kind, countBadge = true) {
  if (run.sid !== state.selected) return;
  const out = $("#console");
  const scroller = out.parentElement;
  const atBottom = scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 30;
  const line = document.createElement("span");
  line.className = kind || "";
  line.textContent = text + "\n";
  out.appendChild(line);
  if (out.childNodes.length > 4000) out.removeChild(out.firstChild);
  if (atBottom) scroller.scrollTop = scroller.scrollHeight;
  if (countBadge && state.tab !== "output" && kind !== "system" && kind !== "faint" && !state.replayingHistory) {
    const badge = $("#outputBadge");
    badge.textContent = String((parseInt(badge.textContent, 10) || 0) + 1);
    badge.classList.remove("hidden");
    badge.classList.add("info");
  }
}

function redrawConsole() {
  const run = selectedRun();
  $("#console").innerHTML = "";
  if (!run) return;
  for (const line of run.output) consoleLine(run, line.text, line.stream, false);
  if (run.error) consoleLine(run, `✗ ${run.error.error_type}: ${run.error.message}`, "error", false);
}

function selectTab(name) {
  if (!TABS.includes(name)) name = "steps";
  state.tab = name;
  $$("#detailTabs button").forEach((b) => b.classList.toggle("selected", b.dataset.tab === name));
  $$(".tab-page").forEach((p) => p.classList.toggle("hidden", p.dataset.page !== name));
  if (name === "output") { $("#outputBadge").classList.add("hidden"); $("#outputBadge").textContent = ""; }
  if (name === "history") loadHistory();
  storageSet("agentflight.panel.tab", name);
  dirty.steps = dirty.eval = true;
  draw();
}

// ---------------------------------------------------------------- approval dialog
function queueApproval(run, approval) {
  if (state.approvalQueue.some((x) => x.approval.id === approval.id)
    || (state.openApproval && state.openApproval.approval.id === approval.id)) return;
  state.approvalQueue.push({ run, approval });
  if (!state.openApproval) showNextApproval();
}

function showNextApproval() {
  if (state.openApproval) return;
  const next = state.approvalQueue.shift();
  if (!next) return;
  state.openApproval = next;
  const { run, approval } = next;
  $("#approvalTitle").textContent = approval.message || "Approval needed";
  const fileName = basename(approval.file || run.path || "");
  $("#approvalLocation").textContent = `${fileName}${approval.line ? `:${approval.line}` : ""} · the program is waiting on this line`;
  const box = $("#approvalValue");
  box.value = valueText(approval.value);
  box.readOnly = true;
  $("#editBtn").textContent = "✎ Edit";
  $("#approveBtn").textContent = "✓ Approve";
  $("#editBtn").disabled = !approval.editable;
  $("#approvalOverlay").classList.remove("hidden");
  $("#approveBtn").focus();
}

function closeApproval() {
  $("#approvalOverlay").classList.add("hidden");
  state.openApproval = null;
  setTimeout(showNextApproval, 50);
}

async function answerApproval(decision) {
  const open = state.openApproval;
  if (!open) return;
  const command = { command: "approve", approval_id: open.approval.id, decision };
  if (decision === "edit") {
    const text = $("#approvalValue").value;
    try { command.value = typeof open.approval.value === "string" ? text : JSON.parse(text); }
    catch { toast("The edited value is not valid JSON.", "error"); return; }
  }
  await api.post("/api/command", { sid: open.run.sid, ...command }).catch((e) => toast(esc(e.message), "error"));
  closeApproval();
}

// ---------------------------------------------------------------- confirmation dialog
let confirmResolve = null;

function confirmDialog(title, text, okLabel = "Yes", danger = false, singleButton = false) {
  return new Promise((resolve) => {
    confirmResolve = resolve;
    $("#confirmTitle").textContent = title;
    $("#confirmText").textContent = text;
    $("#confirmCancel").classList.toggle("hidden", singleButton);
    $("#confirmOk").textContent = okLabel;
    $("#confirmOk").classList.toggle("danger", danger);
    $("#confirmOverlay").classList.remove("hidden");
    $("#confirmOk").focus();
  });
}

function closeConfirm(value) {
  $("#confirmOverlay").classList.add("hidden");
  const resolve = confirmResolve;
  confirmResolve = null;
  if (resolve) resolve(value);
}

// ---------------------------------------------------------------- settings
const KEY_INFO = {
  ANTHROPIC_API_KEY: ["Anthropic (Claude)", "claude-opus-5-5, claude-sonnet-5-5…  console.anthropic.com"],
  GEMINI_API_KEY: ["Google Gemini", "gemini-flash-latest…  aistudio.google.com"],
  OPENAI_API_KEY: ["OpenAI", "gpt-5, gpt-5-mini…  platform.openai.com"],
  OPENAI_BASE_URL: ["OpenAI-compatible server URL", "Leave empty to use api.openai.com"],
  OLLAMA_HOST: ["Ollama address", "Leave empty for http://localhost:11434"],
};

const EDITORS = [["vscode", "VS Code"], ["cursor", "Cursor"], ["pycharm", "PyCharm"], ["copy", "Copy the path"]];

const SHORTCUTS = {
  "Shift+F5": { name: "Stop the program", run: () => stopProgram() },
  "Ctrl+,": { name: "Settings", run: () => openSettings() },
  "Ctrl+J": { name: "Show / hide the details panel", run: () => togglePanel("details") },
  "Ctrl+Alt+B": { name: "Show / hide the agents panel", run: () => togglePanel("agents") },
};

async function openSettings(tab = "accounts") {
  try { state.settings = await api.get("/api/settings"); } catch (e) { toast(esc(e.message), "error"); return; }
  $("#settingsOverlay").classList.remove("hidden");
  showSettingsTab(SETTINGS_TABS.includes(tab) ? tab : "accounts");
}

function closeSettings() { $("#settingsOverlay").classList.add("hidden"); }

function showSettingsTab(name) {
  $$("#settingsTabs button").forEach((b) => b.classList.toggle("selected", b.dataset.settings === name));
  const box = $("#settingsContent");
  const v = state.settings;
  if (name === "accounts") {
    box.innerHTML = '<div class="empty-note">Loading accounts…</div>';
    renderAccounts();
  } else if (name === "keys") {
    renderKeys();
  } else if (name === "general") {
    const editor = storageGet("agentflight.editor", "vscode");
    box.innerHTML = `<div class="setting"><label>Open files in</label>
        <div class="setting-row"><select id="editorSelect">
          ${EDITORS.map(([id, label]) => `<option value="${id}"${editor === id ? " selected" : ""}>${label}</option>`).join("")}
        </select></div>
        <p class="hint">The “Line” and “Open instructions” buttons open the file in this editor. Inside an editor extension, the extension is used.</p></div>`;
    $("#editorSelect").onchange = () => { storageSet("agentflight.editor", $("#editorSelect").value); toast("Saved.", "success"); };
  } else if (name === "shortcuts") {
    box.innerHTML = `<table class="shortcut-table">${Object.entries(SHORTCUTS).map(([keys, s]) => `<tr><td>${esc(s.name)}</td><td>${esc(keys)}</td></tr>`).join("")}</table>`;
  } else {
    box.innerHTML = `<div class="setting"><label>AgentFlight ${esc(VERSION)}</label>
      <p class="hint">Library and live panel for writing LLM agents in Python, watching them run and testing them.<br>
      Settings file: <code>${esc(v.settings_file)}</code></p></div>`;
  }
}

function renderKeys() {
  const box = $("#settingsContent");
  const v = state.settings;
  box.innerHTML = Object.entries(KEY_INFO).map(([name, [title, hint]]) => {
    const k = v.keys[name] || {};
    const pill = k.set ? `<span class="source-pill set">${k.source === "env" ? "from environment" : "saved"} · ${esc(k.display)}</span>`
      : '<span class="source-pill">not set</span>';
    return `<div class="setting"><label>${esc(title)} ${pill}</label>
      <div class="setting-row"><input type="${k.secret ? "password" : "text"}" data-key="${name}" placeholder="${k.set ? "type a new value to replace it" : esc(name)}" autocomplete="off" spellcheck="false">
      ${k.source === "settings" ? `<button class="btn small" data-remove="${name}">Remove</button>` : ""}</div>
      <p class="hint">${esc(hint)}</p></div>`;
  }).join("") + `<div class="setting"><button class="btn primary" id="saveKeys">Save</button>
    <p class="hint">An API key is the alternative to connecting an account: requests are billed to the provider's API account.
    Keys are stored on this computer (encrypted to your Windows account on Windows) in <code>${esc(v.settings_file)}</code>.
    A saved key overrides the environment variable of the same name.</p></div>`;
  $("#saveKeys").onclick = async () => {
    const changes = {};
    $$("#settingsContent [data-key]").forEach((input) => { if (input.value.trim()) changes[input.dataset.key] = input.value.trim(); });
    if (!Object.keys(changes).length) { toast("No keys to change."); return; }
    await saveKeys(changes);
  };
  $$("#settingsContent [data-remove]").forEach((b) => { b.onclick = () => saveKeys({ [b.dataset.remove]: "" }); });
}

async function saveKeys(changes) {
  try {
    const r = await api.post("/api/settings", { keys: changes });
    state.settings.keys = r.keys;
    showSettingsTab("keys");
    toast("Keys saved.", "success");
  } catch (e) { toast(esc(e.message), "error"); }
}

// ---------------------------------------------------------------- accounts
function accountsTabOpen() {
  const tab = $("#settingsTabs [data-settings=accounts]");
  return !!tab && tab.classList.contains("selected") && !$("#settingsOverlay").classList.contains("hidden");
}

function commandBlock(label, command) {
  return `<p class="hint">${label}</p><div class="command-row"><code class="command">${esc(command)}</code>
    <button class="btn small" data-claude-command="${esc(command)}">Copy</button></div>`;
}

async function renderAccounts(refresh = false) {
  const box = $("#settingsContent");
  let h;
  try { h = await api.get("/api/accounts" + (refresh ? "?refresh=1" : "")); } catch (e) { box.innerHTML = `<div class="empty-note">${esc(e.message)}</div>`; return; }
  state.accounts = h;
  if (!accountsTabOpen()) return;
  const g = h.chatgpt, c = h.claude, a = g.active;
  const pending = g.login && g.login.status === "pending";
  const notConnected = '<span class="source-pill">not connected</span>';

  let gPill = notConnected;
  if (g.connected) gPill = `<span class="source-pill set">connected · ${esc(a.email || a.name || "account")}</span>`;
  else if (a && a.signed_in && !a.plan_usage) gPill = '<span class="source-pill warn">plan usage not allowed</span>';
  let gButtons;
  if (g.connected) {
    gButtons = `<button class="btn small" data-external="${esc(g.usage_url)}">Manage usage ↗</button>
      <button class="btn small" id="chatgptRefreshModels">Refresh models</button>
      <button class="btn small" id="chatgptAdd">Add another account</button>
      <button class="btn small" id="chatgptSignOut">Sign out</button>`;
  } else {
    gButtons = '<button class="btn chatgpt-btn" id="chatgptSignIn">Continue with ChatGPT</button>'
      + (pending ? ' <span class="hint">Finish signing in in your browser…</span>' : "");
  }
  const picker = g.accounts.length > 1
    ? `<div class="setting-row"><select id="chatgptAccount">${g.accounts.map((x) => `<option value="${esc(x.key)}"${x.active ? " selected" : ""}>${esc(x.email || x.key)}${x.signed_in ? "" : " (signed out)"}</option>`).join("")}</select></div>` : "";
  const models = (a && a.models) || [];
  const gModels = g.connected && models.length
    ? `<p class="hint">Available models: ${models.slice(0, 8).map((m) => `<code>${esc(m.slug)}</code>`).join(", ")}${models.length > 8 ? "…" : ""}</p>` : "";

  let cPill, cCommand = "", cButtons;
  const refreshButton = '<button class="btn small" id="claudeRefresh">Refresh</button>';
  if (!c.installed) {
    cPill = '<span class="source-pill">Claude Code is not installed</span>';
    if (h.claude_install_command) cCommand = commandBlock("Install it with Anthropic's official command (run it in a terminal):", h.claude_install_command);
    cButtons = `<button class="btn small" data-external="${esc(c.install_url)}">Install guide ↗</button> ${refreshButton}`;
  } else if (!c.signed_in) {
    cPill = notConnected;
    cCommand = commandBlock("Run in a terminal:", "agentflight login claude");
    cButtons = refreshButton;
  } else {
    cPill = `<span class="source-pill set">connected · ${esc(c.email)}${c.subscription ? " · " + esc(c.subscription) : ""}</span>`;
    cButtons = `<button class="btn small" data-external="${esc(c.usage_url)}">Usage ↗</button>
      ${refreshButton} <button class="btn small" id="claudeSignOut">Sign out</button>`;
  }

  const prioritySelect = (name, value) => `<select data-priority="${name}"><option value="account"${value === "account" ? " selected" : ""}>Use the account (subscription) first</option><option value="key"${value === "key" ? " selected" : ""}>Use the API key first</option></select>`;
  box.innerHTML = `
    <div class="account-card"><div class="account-head"><b>ChatGPT</b>${gPill}</div>
      <p class="hint">Runs GPT agents on your ChatGPT plan (Plus, Pro…); requests count against your plan's usage. You sign in on OpenAI's own page.</p>
      ${picker}<div class="account-buttons">${gButtons}</div>${gModels}</div>
    <div class="account-card"><div class="account-head"><b>Claude</b>${cPill}</div>
      <p class="hint">Runs Claude agents on your Claude subscription (Pro, Max). Sign-in and requests go through Claude Code on your computer; AgentFlight never sees or stores your Claude credentials.${c.version ? ` Claude Code ${esc(c.version)}.` : ""}</p>
      ${cCommand}<div class="account-buttons">${cButtons}</div></div>
    <div class="account-card"><div class="account-head"><b>Gemini</b>${h.keys.gemini ? '<span class="source-pill set">API key set</span>' : notConnected}</div>
      <p class="hint">Google does not allow Gemini subscriptions to be used in other apps; Gemini agents run with an API key.</p>
      <div class="account-buttons"><button class="btn small" id="geminiKey">Enter API key</button></div></div>
    <div class="setting"><label>When both an account and an API key are set</label>
      <div class="setting-row"><span class="priority-name">Claude</span>${prioritySelect("anthropic", h.priority.anthropic)}</div>
      <div class="setting-row"><span class="priority-name">GPT</span>${prioritySelect("openai", h.priority.openai)}</div>
      <p class="hint">Agents name a model (<code>model = "claude-opus-5-5"</code>, <code>model = "gpt-5.5"</code>); this setting decides which account the request goes through. To force a route, use a prefix: <code>claude-code/</code>, <code>anthropic/</code>, <code>chatgpt/</code>, <code>openai/</code>.</p></div>`;

  const onClick = (selector, fn) => { const b = $(selector); if (b) b.onclick = fn; };
  onClick("#chatgptSignIn", () => chatgptSignIn(false));
  onClick("#chatgptAdd", () => chatgptSignIn(true));
  onClick("#chatgptSignOut", async () => {
    if (!await confirmDialog("Sign out of ChatGPT", "The ChatGPT session on this computer is closed; GPT agents will no longer use this account.", "Sign out", true)) return;
    try {
      const r = await api.post("/api/accounts/chatgpt/logout", {});
      toast(r.revocation_confirmed ? "Signed out of ChatGPT."
        : "Signed out on this computer; remote revocation could not be confirmed. You can disconnect the app in ChatGPT settings.",
      r.revocation_confirmed ? "success" : "");
    } catch (e) { toast(esc(e.message), "error"); }
    renderAccounts();
  });
  onClick("#chatgptRefreshModels", async () => {
    try { const r = await api.post("/api/accounts/chatgpt/models", {}); toast(`${r.models.length} models found.`, "success"); } catch (e) { toast(esc(e.message), "error"); }
    renderAccounts();
  });
  const accountSelect = $("#chatgptAccount");
  if (accountSelect) {
    accountSelect.onchange = async () => {
      await api.post("/api/accounts/chatgpt/select", { key: accountSelect.value }).catch((e) => toast(esc(e.message), "error"));
      renderAccounts();
    };
  }
  onClick("#claudeRefresh", () => renderAccounts(true));
  onClick("#claudeSignOut", async () => {
    if (!await confirmDialog("Sign out of Claude", "Claude Code on this computer is signed out (also wherever else you use Claude Code).", "Sign out", true)) return;
    await api.post("/api/accounts/claude/logout", {}).catch((e) => toast(esc(e.message), "error"));
    renderAccounts(true);
  });
  $$("#settingsContent [data-claude-command]").forEach((b) => {
    b.onclick = async () => { if (await copyText(b.dataset.claudeCommand)) watchClaudeStatus(); };
  });
  onClick("#geminiKey", () => showSettingsTab("keys"));
  $$("#settingsContent [data-priority]").forEach((select) => {
    select.onchange = async () => {
      try {
        await api.post("/api/accounts/priority", { [select.dataset.priority]: select.value });
        toast("Saved.", "success");
      } catch (e) { toast(esc(e.message), "error"); }
    };
  });
}

async function chatgptSignIn(addAccount) {
  try {
    const r = await api.post("/api/accounts/chatgpt/login", { new: addAccount });
    openExternal(r.url);
    toast("ChatGPT sign-in opened in your browser. Come back here when you are done.");
    renderAccounts();
  } catch (e) { toast("Could not start ChatGPT sign-in: " + esc(e.message), "error"); }
}

function accountChanged(o) {
  if (o.account === "chatgpt" && o.status === "done") {
    const info = o.info || {};
    if (info.plan_usage) {
      toast("ChatGPT connected: " + esc(info.email || ""), "success");
      if (!info.welcomed) chatgptWelcome(info);
    } else {
      toast("Signed in to ChatGPT, but plan usage was not allowed; GPT agents will not use this account.", "error");
    }
  } else if (o.account === "chatgpt" && (o.status === "error" || o.status === "rejected")) {
    toast(esc(o.message || "ChatGPT sign-in did not complete."), "error");
  }
  if (accountsTabOpen()) renderAccounts();
}

async function chatgptWelcome(info) {
  await confirmDialog("You're using your ChatGPT plan",
    "Requests of GPT agents in this app use your ChatGPT plan. You can review usage and this app's limit in ChatGPT settings (chatgpt.com/settings/usage).",
    "Got it", false, true);
  api.post("/api/accounts/chatgpt/welcomed", { key: info.key }).catch(() => {});
}

// After the user copies the Claude Code login / install command, watch for the sign-in for a while.
let claudeWatch = null;
function watchClaudeStatus() {
  clearInterval(claudeWatch);
  const until = Date.now() + 5 * 60 * 1000;
  claudeWatch = setInterval(async () => {
    if (Date.now() > until) { clearInterval(claudeWatch); return; }
    try {
      const h = await api.get("/api/accounts?refresh=1");
      if (h.claude.signed_in) {
        clearInterval(claudeWatch);
        toast(`Claude connected: ${esc(h.claude.email)}${h.claude.subscription ? " · " + esc(h.claude.subscription) : ""}`, "success");
        if (accountsTabOpen()) renderAccounts();
      }
    } catch { /* try again */ }
  }, 3000);
}

// ---------------------------------------------------------------- event stream
function listenToEvents() {
  const source = new EventSource("/api/events");
  source.onmessage = (e) => {
    try { handleEvent(JSON.parse(e.data)); } catch (err) { console.error(err, e.data); }
  };
  source.onerror = () => {
    if (source.readyState === EventSource.CLOSED) setTimeout(listenToEvents, 1500);
  };
  source.addEventListener("open", () => {
    state.runs.clear(); state.order = []; state.selected = null; state.selectedCall = null;
  });
}

// ---------------------------------------------------------------- layout
const PANEL_CLASSES = { details: "details-hidden", agents: "agents-hidden" };

function togglePanel(name) {
  const cls = PANEL_CLASSES[name];
  document.body.classList.toggle(cls);
  storageSet(`agentflight.panel.${name}`, document.body.classList.contains(cls) ? "1" : "0");
}

function setupResizeHandle(selector, cssVar, min, max) {
  const handle = $(selector);
  const saved = storageGet(`agentflight.${cssVar}`, null);
  if (saved) document.documentElement.style.setProperty(`--${cssVar}`, saved);
  handle.addEventListener("mousedown", (e) => {
    e.preventDefault();
    handle.classList.add("dragging");
    const move = (ev) => {
      const px = `${Math.max(min, Math.min(max(), window.innerWidth - ev.clientX))}px`;
      document.documentElement.style.setProperty(`--${cssVar}`, px);
      storageSet(`agentflight.${cssVar}`, px);
    };
    const release = () => {
      handle.classList.remove("dragging");
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", release);
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", release);
  });
}

function shortcutString(e) {
  const parts = [];
  if (e.ctrlKey || e.metaKey) parts.push("Ctrl");
  if (e.altKey) parts.push("Alt");
  if (e.shiftKey) parts.push("Shift");
  let key = e.key || "";
  if (e.code === "Comma") key = ",";
  if (e.code === "KeyB") key = "B";
  if (key.length === 1) key = key.toUpperCase();
  parts.push(key);
  return parts.join("+");
}

function handleShortcut(e) {
  const shortcut = SHORTCUTS[shortcutString(e)];
  if (!shortcut) return;
  e.preventDefault();
  e.stopPropagation();
  shortcut.run();
}

// ---------------------------------------------------------------- UI bindings
function bindUi() {
  $("#stopBtn").addEventListener("click", stopProgram);
  $("#pauseAllBtn").addEventListener("click", togglePauseAll);
  $("#settingsBtn").addEventListener("click", () => openSettings());
  $("#runSelect").addEventListener("change", (e) => {
    state.selected = e.target.value; state.selectedCall = null;
    redrawConsole(); markAllDirty();
  });
  $("#agentTree").addEventListener("click", (e) => {
    const approvalNode = e.target.closest("[data-approval]");
    if (approvalNode) {
      const run = selectedRun();
      const approval = run && run.approvals.get(approvalNode.dataset.approval);
      if (approval && !approval.decision && isLive(run) && !state.openApproval) {
        state.approvalQueue = state.approvalQueue.filter((x) => x.approval.id !== approval.id);
        state.approvalQueue.unshift({ run, approval });
        showNextApproval();
      }
      return;
    }
    const node = e.target.closest("[data-call]");
    if (!node) return;
    state.selectedCall = node.dataset.call;
    dirty.tree = dirty.steps = true;
    if (state.tab !== "steps") selectTab("steps"); else draw();
  });
  $("#stepsPage").addEventListener("click", (e) => {
    const card = e.target.closest(".step-card");
    if (card) { state.selectedCall = card.dataset.call; dirty.tree = dirty.steps = true; draw(); return; }
    const b = e.target.closest("[data-action]");
    if (!b) return;
    if (b.dataset.action === "instructions") openInEditor(b.dataset.file);
    if (b.dataset.action === "line") openInEditor(b.dataset.file, parseInt(b.dataset.line, 10));
    if (b.dataset.action === "replay") replayFromStep(parseInt(b.dataset.step, 10));
  });
  $("#evalPage").addEventListener("click", (e) => {
    const row = e.target.closest(".summary-row");
    if (!row) return;
    let next = row.nextElementSibling;
    while (next && next.classList.contains("sample-row")) { next.classList.toggle("hidden"); next = next.nextElementSibling; }
  });
  $("#historyPage").addEventListener("click", (e) => {
    const item = e.target.closest("[data-run-id]");
    if (item) openRecord(item.dataset.runId, item.dataset.project || "");
  });
  $$("#detailTabs button").forEach((b) => b.addEventListener("click", () => selectTab(b.dataset.tab)));

  // composer
  $("#agentPauseBtn").addEventListener("click", () => {
    const run = selectedRun();
    const call = run && run.calls.get(state.selectedCall);
    if (call) sendCommand({ command: call.status === "paused" ? "resume" : "pause", id: call.id });
  });
  $("#agentKillBtn").addEventListener("click", () => {
    const run = selectedRun();
    const call = run && run.calls.get(state.selectedCall);
    if (call) sendCommand({ command: "kill", id: call.id });
  });
  const sendMessage = () => {
    const input = $("#messageInput");
    const text = input.value.trim();
    if (!text || !state.selectedCall) return;
    sendCommand({ command: "message", id: state.selectedCall, text });
    input.value = "";
  };
  $("#sendBtn").addEventListener("click", sendMessage);
  $("#messageInput").addEventListener("keydown", (e) => { if (e.key === "Enter") sendMessage(); });

  // approval dialog
  $("#approveBtn").addEventListener("click", () => answerApproval($("#approvalValue").readOnly ? "approve" : "edit"));
  $("#rejectBtn").addEventListener("click", () => answerApproval("reject"));
  $("#editBtn").addEventListener("click", () => {
    const box = $("#approvalValue");
    box.readOnly = !box.readOnly;
    $("#editBtn").textContent = box.readOnly ? "✎ Edit" : "↺ Cancel edit";
    $("#approveBtn").textContent = box.readOnly ? "✓ Approve" : "✓ Approve edited value";
    if (!box.readOnly) box.focus();
    else if (state.openApproval) box.value = valueText(state.openApproval.approval.value);
  });

  // confirmation dialog
  $("#confirmOk").addEventListener("click", () => closeConfirm(true));
  $("#confirmCancel").addEventListener("click", () => closeConfirm(false));
  $("#confirmOverlay").addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); closeConfirm(false); }
    if (e.key === "Enter") { e.preventDefault(); closeConfirm(true); }
  });

  // settings
  $("#settingsClose").addEventListener("click", closeSettings);
  $("#settingsOverlay").addEventListener("keydown", (e) => { if (e.key === "Escape") closeSettings(); });
  $("#settingsTabs").addEventListener("click", (e) => {
    const b = e.target.closest("[data-settings]");
    if (b) showSettingsTab(b.dataset.settings);
  });

  window.addEventListener("keydown", handleShortcut, true);
  setupResizeHandle("#agentsHandle", "agents-width", 320, () => window.innerWidth * 0.6);
}

// ---------------------------------------------------------------- start
async function start() {
  bindUi();
  for (const name of Object.keys(PANEL_CLASSES)) {
    if (storageGet(`agentflight.panel.${name}`, "0") === "1") document.body.classList.add(PANEL_CLASSES[name]);
  }
  if (EMBEDDED) document.body.classList.add("embedded");
  $("#sbVersion").textContent = `AgentFlight ${VERSION}`;
  try {
    await api.get("/api/status");
  } catch (e) { toast("Could not reach the server: " + esc(e.message), "error"); return; }
  listenToEvents();
  selectTab(storageGet("agentflight.panel.tab", "steps"));
  loadHistory();
  const settingsTab = new URLSearchParams(location.search).get("settings");
  if (settingsTab) openSettings(settingsTab);
}

start();
