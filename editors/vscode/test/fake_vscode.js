"use strict";
/* A small stand-in for the `vscode` module: enough of the API for extension.js, recording what the
 * extension does so test/unit.js can check it without opening a VS Code window. */

const recorded = {
  commands: new Map(), codeLensProviders: [], completionProviders: [], views: new Map(), terminals: [], notifications: [],
  errors: [], diagnostics: new Map(), opened: [], saveListeners: [], settings: {}, decorations: new Map(),
};

class Position { constructor(line, character) { this.line = line; this.character = character; } }
class Range {
  constructor(a, b, c, d) {
    if (a instanceof Position) { this.start = a; this.end = b; } else { this.start = new Position(a, b); this.end = new Position(c, d); }
  }
}
class Selection extends Range {}
class CodeLens { constructor(range, command) { this.range = range; this.command = command; } }
class CompletionItem { constructor(label, kind) { this.label = label; this.kind = kind; } }
class Diagnostic { constructor(range, message, severity) { this.range = range; this.message = message; this.severity = severity; } }
class ThemeColor { constructor(id) { this.id = id; } }

class Uri {
  constructor(fsPath, text) { this.fsPath = fsPath; this.scheme = "file"; this._text = text; }
  toString() { return this._text; }
  static file(p) { return new Uri(p, `file:///${p.replace(/\\/g, "/")}`); }
  static parse(s) { return new Uri(s, s); }
  static joinPath(base, ...parts) { return new Uri([base.fsPath, ...parts].join("/"), ""); }
}

class FakeDocument {
  constructor(filePath, text, languageId = "python") {
    this.uri = Uri.file(filePath);
    this.languageId = languageId;
    this._text = text;
    this.isDirty = false;
  }
  get lineCount() { return this._text.split("\n").length; }
  lineAt(i) { return { text: this._text.split("\n")[i] || "" }; }
  getText(r) {
    if (!r) return this._text;
    return this._text.split("\n").slice(r.start.line, r.end.line).join("\n");
  }
  save() { return Promise.resolve(true); }
}

class FakeEditor {
  constructor(document) { this.document = document; this.selection = null; }
  setDecorations(type, ranges) { recorded.decorations.set(`${type.name}:${this.document.uri.fsPath}`, ranges); }
  revealRange() {}
}

let decorationCounter = 0;
const window = {
  visibleTextEditors: [],
  activeTextEditor: undefined,
  terminals: [],
  createTextEditorDecorationType: (options) => ({ name: `decoration${++decorationCounter}`, options, dispose() {} }),
  createStatusBarItem: () => ({ text: "", show() {}, hide() {}, dispose() {} }),
  showInformationMessage: (m) => { recorded.notifications.push(m); return Promise.resolve(undefined); },
  showWarningMessage: (m) => { recorded.notifications.push(m); return Promise.resolve(undefined); },
  showErrorMessage: (m) => { recorded.errors.push(m); return Promise.resolve(undefined); },
  registerWebviewViewProvider: (id, provider) => { recorded.views.set(id, provider); return { dispose() {} }; },
  createWebviewPanel: () => {
    const p = { webview: fakeWebview(), iconPath: null, reveal() {}, onDidDispose() {} };
    return p;
  },
  createTerminal: ({ name }) => {
    const t = { name, sent: [], show() {}, sendText(m) { this.sent.push(m); } };
    recorded.terminals.push(t);
    window.terminals.push(t);
    return t;
  },
  showTextDocument: async (document, options) => {
    const e = new FakeEditor(document);
    recorded.opened.push({ path: document.uri.fsPath, options });
    window.activeTextEditor = e;
    return e;
  },
  onDidChangeVisibleTextEditors: () => ({ dispose() {} }),
};

function fakeWebview() {
  const w = { html: "", options: null, listener: null, messages: [] };
  w.onDidReceiveMessage = (f) => { w.listener = f; return { dispose() {} }; };
  w.postMessage = (m) => { w.messages.push(m); return Promise.resolve(true); };
  return w;
}

const workspace = {
  getConfiguration: () => ({ get: (k) => recorded.settings[k] }),
  onDidSaveTextDocument: (f) => { recorded.saveListeners.push(f); return { dispose() {} }; },
  openTextDocument: async (u) => {
    const fs = require("fs");
    const filePath = u.fsPath || u;
    return new FakeDocument(filePath, fs.existsSync(filePath) ? fs.readFileSync(filePath, "utf8") : "");
  },
  workspaceFolders: [],
};

const commands = {
  registerCommand: (name, f) => { recorded.commands.set(name, f); return { dispose() {} }; },
  executeCommand: async (name, ...args) => { const f = recorded.commands.get(name); return f ? f(...args) : undefined; },
};

const languages = {
  registerCodeLensProvider: (_selector, p) => { recorded.codeLensProviders.push(p); return { dispose() {} }; },
  registerCompletionItemProvider: (_selector, p) => { recorded.completionProviders.push(p); return { dispose() {} }; },
  createDiagnosticCollection: () => ({ set: (u, list) => recorded.diagnostics.set(u.fsPath, list), dispose() {} }),
};

module.exports = {
  recorded, FakeDocument, FakeEditor, fakeWebview,
  Position, Range, Selection, CodeLens, CompletionItem, Diagnostic, ThemeColor, Uri, window, workspace, commands, languages,
  extensions: { getExtension: () => undefined },
  env: { language: "en", shell: "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe", asExternalUri: async (u) => u },
  StatusBarAlignment: { Left: 1, Right: 2 },
  ViewColumn: { One: 1, Beside: -2 },
  TextEditorRevealType: { InCenter: 2 },
  CompletionItemKind: { Value: 11 },
  DiagnosticSeverity: { Error: 0, Warning: 1 },
};
