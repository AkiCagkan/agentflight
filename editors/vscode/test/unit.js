"use strict";
/* Extension logic tests without a VS Code window: extension.js runs against test/fake_vscode.js and a
 * real AgentFlight panel server. Run with Node, or with VS Code's own Node runtime:
 *   set ELECTRON_RUN_AS_NODE=1 && Code.exe test/unit.js
 * (tests/vscode_unit_test.py does this). Env: AGENTFLIGHT_PYTHON (python with agentflight), AGENTFLIGHT_PROJECT (copy of examples/). */

const Module = require("module");
const path = require("path");
const fs = require("fs");
const childProcess = require("child_process");
const assert = require("assert");

const originalResolve = Module._resolveFilename;
Module._resolveFilename = function (request, ...rest) {
  if (request === "vscode") return path.join(__dirname, "fake_vscode.js");
  return originalResolve.call(this, request, ...rest);
};
const vscode = require("vscode");
const { recorded, FakeDocument, FakeEditor, fakeWebview } = vscode;
const extension = require("../extension.js");

const PYTHON = process.env.AGENTFLIGHT_PYTHON;
const PROJECT = process.env.AGENTFLIGHT_PROJECT;
const APPROVAL_LINE = 76;  // the require_approval(...) line of examples/02_research.py
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function waitFor(fn, timeout, name) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    const v = await fn();
    if (v) return v;
    await sleep(200);
  }
  throw new Error(`timed out: ${name}`);
}
const state = () => vscode.commands.executeCommand("agentflight._state");
const passed = (name) => console.log(`ok   ${name}`);
const exampleDocument = (name) => new FakeDocument(path.join(PROJECT, name), fs.readFileSync(path.join(PROJECT, name), "utf8"));

async function main() {
  assert.ok(PYTHON && PROJECT, "AGENTFLIGHT_PYTHON and AGENTFLIGHT_PROJECT must be set");
  recorded.settings = { pythonPath: PYTHON, panelPort: 7795, codeLens: true, runningLines: true, checkOnSave: true };
  extension.activate({ subscriptions: [], extensionUri: vscode.Uri.file(path.join(__dirname, "..")) });
  for (const k of ["agentflight.openPanel", "agentflight.run", "agentflight.eval", "agentflight.connectAccounts", "agentflight.openPanelInTab", "agentflight.stopPanel"]) {
    assert.ok(recorded.commands.has(k), k);
  }
  passed("activate registers the commands");

  // CodeLens
  const lensProvider = recorded.codeLensProviders[0];
  const hello = exampleDocument("00_hello.py");
  const lenses = lensProvider.provideCodeLenses(hello);
  assert.ok(lenses.some((l) => l.command.command === "agentflight.run"), "run lens");
  const summary = exampleDocument("01_summary.py");
  const evalLens = lensProvider.provideCodeLenses(summary).find((l) => l.command.command === "agentflight.eval");
  assert.deepStrictEqual(evalLens.command.arguments[1], "SummaryRules");
  const foreign = new FakeDocument("x.py", "class Agent(Base):\n    pass\n");
  assert.strictEqual(lensProvider.provideCodeLenses(foreign).length, 0, "no lenses in files that do not use agentflight");
  passed("code lenses");

  // Panel: the extension starts `agentflight watch` with the configured Python and shows it in a webview
  const viewProvider = recorded.views.get("agentflight.panel");
  const webview = fakeWebview();
  await viewProvider.resolveWebviewView({ webview, onDidDispose() {} });
  assert.ok(/<iframe id="panel" src="http:\/\/127\.0\.0\.1:\d+\//.test(webview.html), webview.html.slice(0, 300));
  const s0 = await waitFor(async () => { const s = await state(); return s.info && s.eventCount > 0 ? s : null; }, 30000, "event stream");
  passed(`panel started on port ${s0.info.port} and the event stream is connected`);

  // Run command: types the right command into the "AgentFlight" terminal (PowerShell call operator, quoting)
  await vscode.commands.executeCommand("agentflight.run", hello.uri);
  const terminal = recorded.terminals.find((t) => t.name === "AgentFlight");
  const line = terminal.sent[0];
  assert.ok(line.startsWith("& "), line);
  assert.ok(line.includes("-m agentflight run") && line.includes("00_hello.py"), line);
  vscode.env.shell = "/bin/bash";
  await vscode.commands.executeCommand("agentflight.eval", summary.uri, "SummaryRules");
  const line2 = terminal.sent[1];
  assert.ok(!line2.startsWith("& ") && line2.includes("-m agentflight eval") && line2.endsWith("--name SummaryRules"), line2);
  passed("run / eval commands");

  // Live events: run 02_research.py as if from a terminal; the approval shows in the editor and the status bar.
  // With the fake model the researchers call the wikipedia_lookup tool, which needs the network; without it the
  // tool returns an error text and the program still reaches the approval step.
  const research = path.join(PROJECT, "02_research.py");
  const editor = new FakeEditor(exampleDocument("02_research.py"));
  vscode.window.visibleTextEditors = [editor];
  const program = childProcess.spawn(PYTHON, ["02_research.py"], {
    cwd: PROJECT, env: { ...process.env, AGENTFLIGHT_MODEL: "fake", AGENTFLIGHT_FAKE_DELAY: "0.05", PYTHONIOENCODING: "utf-8" },
  });
  let programOutput = "";
  program.stdout.on("data", (d) => { programOutput += d; });
  program.stderr.on("data", (d) => { programOutput += d; });
  let exitCode = null;
  const exited = new Promise((r) => program.on("exit", (code) => { exitCode = code; r(code); }));
  let runningSeen = false;
  const sApproval = await waitFor(async () => {
    const s = await state();
    if (s.running > 0) runningSeen = true;
    if (s.approvals > 0) return s;
    if (exitCode !== null) throw new Error(`02_research.py exited (${exitCode}) before asking for approval:\n${programOutput}`);
    return null;
  }, 90000, "approval request");
  assert.ok(runningSeen, "running calls were tracked");
  assert.ok(/approval/.test(sApproval.statusBar), sApproval.statusBar);
  const approvalDecoration = [...recorded.decorations.entries()].find(([k, v]) => k.endsWith("02_research.py") && v.length
    && /waiting for approval/.test(v[0].renderOptions.after.contentText));
  assert.ok(approvalDecoration, "approval line decoration");
  assert.strictEqual(approvalDecoration[1][0].range.start.line, APPROVAL_LINE - 1, "approval decoration line");
  assert.ok(recorded.notifications.some((m) => /Publish|⏸/.test(m)), `notification: ${recorded.notifications.join(" | ")}`);
  const approvalKey = sApproval.approvalKeys[0];
  const sep = approvalKey.indexOf(":");
  const sid = approvalKey.slice(0, sep);
  const approvalId = approvalKey.slice(sep + 1);
  await postToPanel(s0.info, "/api/command", { sid, command: "approve", approval_id: approvalId, decision: "approve" });
  const code = await Promise.race([exited, sleep(60000).then(() => "timeout")]);
  assert.strictEqual(code, 0, `program exit code\n${programOutput}`);
  await waitFor(async () => { const s = await state(); return s.approvals === 0 && s.running === 0 ? s : null; }, 20000, "cleared");
  passed("live running lines, approval decoration, status bar and notification");

  // Model names in model = "
  const completion = recorded.completionProviders[0];
  const document = new FakeDocument("m.py", 'from agentflight import Agent\nclass A(Agent):\n    model = "');
  const suggestions = await completion.provideCompletionItems(document, new vscode.Position(2, 13));
  assert.ok(suggestions.some((o) => o.label.label === "fake"), "fake model suggested");
  passed(`model completion (${suggestions.length} models)`);

  // Check on save
  const broken = path.join(PROJECT, "broken.py");
  fs.writeFileSync(broken, 'from agentflight import Agent, llm\n\n\nclass A(Agent):\n    """x"""\n\n    @llm\n    def f(self, x: str) -> Sumary: ...\n', "utf8");
  const brokenDocument = new FakeDocument(broken, fs.readFileSync(broken, "utf8"));
  for (const f of recorded.saveListeners) await f(brokenDocument);
  const problems = await waitFor(() => recorded.diagnostics.get(broken), 60000, "diagnostics");
  assert.ok(problems.some((x) => /Sumary/.test(x.message) && x.severity === 0), JSON.stringify(problems));
  passed("check on save shows problems");

  // "Line 76" in the panel opens the file at that line
  await webview.listener({ agentflight: "open", file: research, line: APPROVAL_LINE });
  const opened = recorded.opened[recorded.opened.length - 1];
  assert.ok(opened.path.endsWith("02_research.py"));
  assert.strictEqual(vscode.window.activeTextEditor.selection.start.line, APPROVAL_LINE - 1);
  passed("files open from the panel at the right line");

  await vscode.commands.executeCommand("agentflight.stopPanel");
  extension.deactivate();
  console.log("ALL OK");
}

function postToPanel(info, urlPath, body) {
  const http = require("http");
  return new Promise((resolve, reject) => {
    const data = Buffer.from(JSON.stringify(body));
    const r = http.request({ host: "127.0.0.1", port: info.port, path: urlPath, method: "POST", headers: {
      Host: `127.0.0.1:${info.port}`, "Content-Type": "application/json", "X-AgentFlight-Token": info.token, "Content-Length": data.length,
    } }, (response) => { response.resume(); response.on("end", resolve); });
    r.on("error", reject);
    r.end(data);
  });
}

main().then(() => process.exit(0), (e) => { console.error("FAIL", e && e.stack || e); process.exit(1); });
