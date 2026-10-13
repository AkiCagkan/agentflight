"use strict";
/* AgentFlight for VS Code and Cursor.
 *
 * The extension does not reimplement anything: it starts the AgentFlight panel server (`agentflight watch`) with the
 * workspace's Python, shows the panel in the editor's bottom area, and listens to the panel's event stream
 * to highlight the lines whose agent calls are running and to surface approval requests. Files opened from
 * the panel ("Line 42", "Open instructions") come back here through postMessage.
 */

const vscode = require("vscode");
const childProcess = require("child_process");
const crypto = require("crypto");
const fs = require("fs");
const http = require("http");
const net = require("net");
const os = require("os");
const path = require("path");

/** Shared state; exposed through the hidden `agentflight._state` command for tests. */
const S = {
  context: null,
  panelProcess: null,
  info: null,             // contents of panel.json
  view: null,             // WebviewView
  tab: null,              // WebviewPanel
  running: new Map(),     // "sid:id" -> {file, line, agent, method, model}
  approvals: new Map(),   // "sid:approval_id" -> {file, line, message}
  eventCount: 0,
  recentEvents: [],
  historyDone: false,
  request: null,
  statusBar: null,
  runningDecoration: null,
  approvalDecoration: null,
  diagnostics: null,
  python: null,
};

// ---------------------------------------------------------------- python and the panel server
function settingsDir() {
  return process.env.AGENTFLIGHT_HOME || path.join(os.homedir(), ".agentflight");
}

function readPanelJson() {
  try {
    return JSON.parse(fs.readFileSync(path.join(settingsDir(), "panel.json"), "utf8"));
  } catch {
    return null;
  }
}

function isPortOpen(port) {
  return new Promise((resolve) => {
    const s = net.connect({ host: "127.0.0.1", port, timeout: 400 }, () => { s.destroy(); resolve(true); });
    s.on("error", () => resolve(false));
    s.on("timeout", () => { s.destroy(); resolve(false); });
  });
}

async function runningPanel() {
  const info = readPanelJson();
  if (!info) return null;
  if (!(await isPortOpen(info.runner_port)) || !(await isPortOpen(info.port))) return null;
  return info;
}

async function pythonPath(document) {
  const setting = vscode.workspace.getConfiguration("agentflight").get("pythonPath");
  if (setting) return setting;
  try {
    const extension = vscode.extensions.getExtension("ms-python.python");
    if (extension) {
      const api = extension.isActive ? extension.exports : await extension.activate();
      const environments = api && api.environments;
      if (environments) {
        const envPath = environments.getActiveEnvironmentPath(document ? document.uri : undefined);
        const resolved = await environments.resolveEnvironment(envPath);
        const exe = resolved && resolved.executable && resolved.executable.uri && resolved.executable.uri.fsPath;
        if (exe) return exe;
      }
    }
  } catch { /* the Python extension is optional */ }
  return process.platform === "win32" ? "python" : "python3";
}

function agentFlightVersion(python) {
  return new Promise((resolve) => {
    childProcess.execFile(python, ["-c", "import agentflight, sys; sys.stdout.write(agentflight.__version__)"], { timeout: 20000, windowsHide: true },
      (error, stdout) => resolve(error ? null : String(stdout).trim()));
  });
}

async function ensurePanel({ quiet = false } = {}) {
  let info = await runningPanel();
  if (info) { S.info = info; listenToEvents(); return info; }
  const python = await pythonPath(vscode.window.activeTextEditor && vscode.window.activeTextEditor.document);
  const version = await agentFlightVersion(python);
  if (!version) {
    if (!quiet) {
      vscode.window.showErrorMessage(
        `The agentflight package was not found in this Python: ${python}. Install it with: pip install agentflight (or change the "agentflight.pythonPath" setting).`);
    }
    return null;
  }
  S.python = python;
  const port = vscode.workspace.getConfiguration("agentflight").get("panelPort") || 7790;
  S.panelProcess = childProcess.spawn(python, ["-m", "agentflight", "watch", "--no-browser", "--port", String(port)], {
    windowsHide: true, env: { ...process.env, PYTHONIOENCODING: "utf-8" }, stdio: "ignore",
  });
  S.panelProcess.on("exit", () => { S.panelProcess = null; });
  for (let i = 0; i < 100; i++) {
    await new Promise((r) => setTimeout(r, 150));
    info = await runningPanel();
    if (info) break;
  }
  if (!info) {
    if (!quiet) vscode.window.showErrorMessage("Could not start the AgentFlight panel.");
    return null;
  }
  S.info = info;
  listenToEvents();
  return info;
}

async function panelUrl(query = "") {
  const info = S.info || (await runningPanel());
  if (!info) return null;
  const local = vscode.Uri.parse(`http://127.0.0.1:${info.port}/${query}`);
  return (await vscode.env.asExternalUri(local)).toString(true);
}

// ---------------------------------------------------------------- webview (bottom panel and editor tab)
function webviewHtml(webview, url) {
  const nonce = crypto.randomBytes(16).toString("base64");
  if (!url) {
    return `<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';">
<style>body{font-family:var(--vscode-font-family);color:var(--vscode-foreground);padding:16px}
button{background:var(--vscode-button-background);color:var(--vscode-button-foreground);border:0;padding:6px 12px;cursor:pointer}</style></head>
<body><p>The AgentFlight panel is not running.</p>
<button id="b">Start the panel</button>
<script nonce="${nonce}">const vscode = acquireVsCodeApi(); document.getElementById("b").onclick = () => vscode.postMessage({ cmd: "start" });</script>
</body></html>`;
  }
  const origin = new URL(url).origin;
  return `<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; frame-src ${origin}; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';">
<style>html,body{margin:0;padding:0;height:100%;overflow:hidden;background:#151821}iframe{border:0;width:100%;height:100%;display:block}</style></head>
<body><iframe id="panel" src="${url}" allow="clipboard-read; clipboard-write"></iframe>
<script nonce="${nonce}">
  const vscode = acquireVsCodeApi();
  window.addEventListener("message", (e) => { if (e.data && e.data.agentflight) vscode.postMessage(e.data); });
</script></body></html>`;
}

async function setupWebview(webview, query = "") {
  webview.options = { enableScripts: true };
  webview.onDidReceiveMessage(async (m) => {
    if (m && m.agentflight === "open" && m.file) await openFile(m.file, m.line);
    if (m && m.cmd === "start") { await ensurePanel(); webview.html = webviewHtml(webview, await panelUrl(query)); }
  });
  const info = await ensurePanel({ quiet: true });
  webview.html = webviewHtml(webview, info ? await panelUrl(query) : null);
}

class PanelViewProvider {
  resolveWebviewView(view) {
    S.view = view;
    view.onDidDispose(() => { S.view = null; });
    return setupWebview(view.webview);
  }
}

async function showPanel(query = "") {
  await ensurePanel();
  if (query) { await openPanelInTab(query); return; }
  await vscode.commands.executeCommand("agentflight.panel.focus");
}

async function openPanelInTab(query = "") {
  if (S.tab) { S.tab.reveal(); return; }
  S.tab = vscode.window.createWebviewPanel("agentflight.panelTab", "AgentFlight", vscode.ViewColumn.Beside, { enableScripts: true, retainContextWhenHidden: true });
  S.tab.iconPath = vscode.Uri.joinPath(S.context.extensionUri, "media", "icon.png");
  S.tab.onDidDispose(() => { S.tab = null; });
  await setupWebview(S.tab.webview, query);
}

async function openFile(file, line) {
  try {
    const document = await vscode.workspace.openTextDocument(vscode.Uri.file(file));
    const editor = await vscode.window.showTextDocument(document, { preview: false, viewColumn: vscode.ViewColumn.One });
    if (line) {
      const position = new vscode.Position(Math.max(0, line - 1), 0);
      editor.selection = new vscode.Selection(position, position);
      editor.revealRange(new vscode.Range(position, position), vscode.TextEditorRevealType.InCenter);
    }
  } catch (e) {
    vscode.window.showErrorMessage("Could not open the file: " + file);
  }
}

// ---------------------------------------------------------------- running programs
function terminal() {
  const name = "AgentFlight";
  return vscode.window.terminals.find((x) => x.name === name) || vscode.window.createTerminal({ name });
}

function commandLine(python, args) {
  const shell = (vscode.env.shell || "").toLowerCase();
  const quote = (x) => (/[\s"'&()]/.test(x) ? `"${x.replace(/"/g, '\\"')}"` : x);
  const line = [quote(python), ...args.map(quote)].join(" ");
  return shell.includes("powershell") || shell.includes("pwsh") ? `& ${line}` : line;
}

async function runFile(uri, kind, evalName) {
  const editor = vscode.window.activeTextEditor;
  const document = uri ? await vscode.workspace.openTextDocument(uri) : editor && editor.document;
  if (!document) { vscode.window.showWarningMessage("Open a Python file first."); return; }
  if (document.isDirty) await document.save();
  const info = await ensurePanel();
  if (!info) return;
  const python = S.python || (await pythonPath(document));
  const args = ["-m", "agentflight", kind, document.uri.fsPath];
  if (evalName) args.push("--name", evalName);
  const term = terminal();
  term.show(true);
  term.sendText(commandLine(python, args), true);
  vscode.commands.executeCommand("agentflight.panel.focus").then(undefined, () => {});
}

// ---------------------------------------------------------------- event stream: running lines and approvals
function listenToEvents() {
  if (S.request || !S.info) return;
  const port = S.info.port;
  let buffer = "";
  S.historyDone = false;
  const request = http.get({ host: "127.0.0.1", port, path: "/api/events", headers: { Host: `127.0.0.1:${port}` } }, (response) => {
    response.setEncoding("utf8");
    response.on("data", (chunk) => {
      buffer += chunk;
      let i;
      while ((i = buffer.indexOf("\n\n")) >= 0) {
        const block = buffer.slice(0, i);
        buffer = buffer.slice(i + 2);
        for (const line of block.split("\n")) {
          if (!line.startsWith("data: ")) continue;
          try { handleEvent(JSON.parse(line.slice(6))); } catch { /* ignore */ }
        }
      }
    });
    response.on("end", reconnect);
  });
  request.on("error", reconnect);
  S.request = request;
}

function reconnect() {
  if (S.request) { try { S.request.destroy(); } catch { /* */ } }
  S.request = null;
  S.running.clear();
  S.approvals.clear();
  updateDecorations();
  setTimeout(async () => {
    const info = await runningPanel();
    if (info) { S.info = info; listenToEvents(); } else setTimeout(reconnect, 3000);
  }, 1500);
}

function handleEvent(e) {
  S.eventCount += 1;
  S.recentEvents.push(e.type);
  if (S.recentEvents.length > 50) S.recentEvents.shift();
  const key = (x) => `${e.sid}:${x}`;
  switch (e.type) {
    case "history_end": S.historyDone = true; break;
    case "call_started":
      if (e.file && e.line) S.running.set(key(e.id), { file: e.file, line: e.line, agent: e.agent, method: e.method, model: e.model });
      break;
    case "call_finished": S.running.delete(key(e.id)); break;
    case "approval_requested":
      if (e.file && e.line) S.approvals.set(key(e.approval_id), { file: e.file, line: e.line, message: e.message });
      if (S.historyDone) notifyApproval(e);
      break;
    case "approval_answered": S.approvals.delete(key(e.approval_id)); break;
    case "process_finished":
      for (const k of [...S.running.keys()]) if (k.startsWith(`${e.sid}:`)) S.running.delete(k);
      for (const k of [...S.approvals.keys()]) if (k.startsWith(`${e.sid}:`)) S.approvals.delete(k);
      break;
    default:
      return;
  }
  updateDecorations();
}

async function notifyApproval(e) {
  const open = "Open panel";
  const choice = await vscode.window.showInformationMessage(`⏸ ${e.message || "Approval needed"}`, open);
  if (choice === open) showPanel();
}

function sameFile(a, b) {
  const n = (x) => path.normalize(x || "").toLowerCase();
  return process.platform === "win32" ? n(a) === n(b) : path.normalize(a) === path.normalize(b);
}

function updateDecorations() {
  const enabled = vscode.workspace.getConfiguration("agentflight").get("runningLines");
  for (const editor of vscode.window.visibleTextEditors) {
    const file = editor.document.uri.fsPath;
    const running = [];
    const waiting = [];
    if (enabled) {
      for (const c of S.running.values()) {
        if (!sameFile(c.file, file)) continue;
        const r = new vscode.Range(c.line - 1, 0, c.line - 1, 0);
        running.push({ range: r, renderOptions: { after: { contentText: `  ⟳ ${c.agent}.${c.method} · ${c.model || ""}` } } });
      }
      for (const c of S.approvals.values()) {
        if (!sameFile(c.file, file)) continue;
        const r = new vscode.Range(c.line - 1, 0, c.line - 1, 0);
        waiting.push({ range: r, renderOptions: { after: { contentText: `  ⏸ waiting for approval: ${c.message || ""}` } } });
      }
    }
    editor.setDecorations(S.runningDecoration, running);
    editor.setDecorations(S.approvalDecoration, waiting);
  }
  const n = S.running.size;
  const pending = S.approvals.size;
  if (pending) S.statusBar.text = `$(debug-pause) AgentFlight: ${pending} awaiting approval`;
  else if (n) S.statusBar.text = `$(sync~spin) AgentFlight: ${n} ${n === 1 ? "agent running" : "agents running"}`;
  else S.statusBar.text = "$(pulse) AgentFlight";
  S.statusBar.show();
}

// ---------------------------------------------------------------- CodeLens, model names, check on save
function isAgentFlightFile(document) {
  if (document.languageId !== "python") return false;
  const text = document.getText(new vscode.Range(0, 0, Math.min(document.lineCount, 400), 0));
  return /^\s*(from\s+agentflight\s+import|import\s+agentflight\b)/m.test(text);
}

class AgentFlightCodeLenses {
  provideCodeLenses(document) {
    if (!vscode.workspace.getConfiguration("agentflight").get("codeLens") || !isAgentFlightFile(document)) return [];
    const lenses = [];
    for (let i = 0; i < document.lineCount; i++) {
      const line = document.lineAt(i).text;
      const r = new vscode.Range(i, 0, i, 0);
      if (/^\s*class\s+\w+\s*\((?:[^)]*\b)?Agent\b/.test(line)) {
        lenses.push(new vscode.CodeLens(r, { title: "▶ Run with AgentFlight", command: "agentflight.run", arguments: [document.uri] }));
        lenses.push(new vscode.CodeLens(r, { title: "◉ Panel", command: "agentflight.openPanel" }));
      }
      const ev = /^\s*class\s+(\w+)\s*\((?:[^)]*\b)?Eval\b/.exec(line);
      if (ev) {
        lenses.push(new vscode.CodeLens(r, { title: `✓ Run eval ${ev[1]}`, command: "agentflight.eval", arguments: [document.uri, ev[1]] }));
      }
    }
    return lenses;
  }
}

function fetchModels() {
  return new Promise((resolve) => {
    if (!S.info) { resolve([]); return; }
    const port = S.info.port;
    http.get({ host: "127.0.0.1", port, path: "/api/models", headers: { Host: `127.0.0.1:${port}` } }, (response) => {
      let data = "";
      response.setEncoding("utf8");
      response.on("data", (chunk) => { data += chunk; });
      response.on("end", () => { try { resolve(JSON.parse(data).models || []); } catch { resolve([]); } });
    }).on("error", () => resolve([]));
  });
}

const modelCompletion = {
  async provideCompletionItems(document, position) {
    const before = document.lineAt(position.line).text.slice(0, position.character);
    const match = /\bmodel\s*=\s*["']([^"']*)$/.exec(before);
    if (!match || !isAgentFlightFile(document)) return [];
    const models = await fetchModels();
    const range = new vscode.Range(position.line, position.character - match[1].length, position.line, position.character);
    return models.map((m, i) => {
      const item = new vscode.CompletionItem({ label: m.name, description: m.ready ? m.route : "not connected" }, vscode.CompletionItemKind.Value);
      item.range = range;
      item.sortText = String(i).padStart(3, "0");
      item.detail = m.ready ? `${m.group} · ${m.route}` : (m.reason || "");
      return item;
    });
  },
};

async function checkDocument(document) {
  if (!vscode.workspace.getConfiguration("agentflight").get("checkOnSave") || !isAgentFlightFile(document)) return;
  const python = S.python || (await pythonPath(document));
  childProcess.execFile(python, ["-m", "agentflight", "check", "--json", document.uri.fsPath],
    { timeout: 60000, windowsHide: true, env: { ...process.env, PYTHONIOENCODING: "utf-8" } }, (error, stdout) => {
      let data;
      try { data = JSON.parse(String(stdout)); } catch { return; }
      const items = Object.values(data)[0] || [];
      S.diagnostics.set(document.uri, items.map((x) => {
        const r = new vscode.Range(Math.max(0, x.line - 1), Math.max(0, (x.column || 1) - 1),
          Math.max(0, (x.end_line || x.line) - 1), Math.max(0, (x.end_column || 1000) - 1));
        const d = new vscode.Diagnostic(r, x.message, x.severity === "error" ? vscode.DiagnosticSeverity.Error : vscode.DiagnosticSeverity.Warning);
        d.source = "agentflight";
        return d;
      }));
    });
}

// ---------------------------------------------------------------- activation
function activate(context) {
  S.context = context;
  S.runningDecoration = vscode.window.createTextEditorDecorationType({
    isWholeLine: true,
    backgroundColor: "rgba(108, 203, 138, 0.10)",
    gutterIconPath: vscode.Uri.joinPath(context.extensionUri, "media", "running.svg"),
    gutterIconSize: "contain",
    after: { color: new vscode.ThemeColor("editorCodeLens.foreground"), fontStyle: "italic" },
  });
  S.approvalDecoration = vscode.window.createTextEditorDecorationType({
    isWholeLine: true,
    backgroundColor: "rgba(242, 180, 90, 0.12)",
    gutterIconPath: vscode.Uri.joinPath(context.extensionUri, "media", "approval.svg"),
    gutterIconSize: "contain",
    after: { color: new vscode.ThemeColor("editorWarning.foreground"), fontStyle: "italic" },
  });
  S.statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
  S.statusBar.command = "agentflight.openPanel";
  S.statusBar.tooltip = "Open the AgentFlight panel";
  S.diagnostics = vscode.languages.createDiagnosticCollection("agentflight");
  const selector = [{ language: "python" }];

  context.subscriptions.push(
    S.runningDecoration, S.approvalDecoration, S.statusBar, S.diagnostics,
    vscode.window.registerWebviewViewProvider("agentflight.panel", new PanelViewProvider(), { webviewOptions: { retainContextWhenHidden: true } }),
    vscode.commands.registerCommand("agentflight.openPanel", () => showPanel()),
    vscode.commands.registerCommand("agentflight.openPanelInTab", () => openPanelInTab()),
    vscode.commands.registerCommand("agentflight.connectAccounts", () => showPanel("?settings=accounts")),
    vscode.commands.registerCommand("agentflight.run", (uri) => runFile(uri instanceof vscode.Uri ? uri : undefined, "run")),
    vscode.commands.registerCommand("agentflight.eval", (uri, name) => runFile(uri instanceof vscode.Uri ? uri : undefined, "eval", typeof name === "string" ? name : undefined)),
    vscode.commands.registerCommand("agentflight.stopPanel", async () => {
      const python = S.python || (await pythonPath());
      childProcess.execFile(python, ["-m", "agentflight", "watch", "--stop"], { windowsHide: true }, () => {});
    }),
    vscode.commands.registerCommand("agentflight._state", () => ({
      info: S.info, eventCount: S.eventCount, recentEvents: [...S.recentEvents], running: S.running.size,
      approvals: S.approvals.size, approvalKeys: [...S.approvals.keys()], view: !!S.view, python: S.python, statusBar: S.statusBar.text,
    })),
    vscode.languages.registerCodeLensProvider(selector, new AgentFlightCodeLenses()),
    vscode.languages.registerCompletionItemProvider(selector, modelCompletion, '"', "'", "/", "-"),
    vscode.workspace.onDidSaveTextDocument(checkDocument),
    vscode.window.onDidChangeVisibleTextEditors(() => updateDecorations()),
  );
  updateDecorations();
  // A panel that is already running (`agentflight watch`, or one started by another editor) is picked up right away.
  runningPanel().then((info) => { if (info) { S.info = info; listenToEvents(); } });
}

function deactivate() {
  if (S.request) { try { S.request.destroy(); } catch { /* */ } }
  if (S.panelProcess) { try { S.panelProcess.kill(); } catch { /* */ } }
}

module.exports = { activate, deactivate };
