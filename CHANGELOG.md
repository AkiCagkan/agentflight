# Changelog

## 0.4.0 (unreleased)

The project is renamed from **Ajan** to **AgentFlight**, the codebase is English only, and the scope is
narrowed to the library, the watch panel and the editor extensions.

- **Breaking:** the package, the command and the folders are renamed: `pip install agentflight`,
  `agentflight run`, `agentflight.toml`, `.agentflight/runs/`, `~/.agentflight/`, `AGENTFLIGHT_*` environment
  variables. Records and settings written by 0.3 are not read.
- **Breaking:** the Turkish aliases of the API (`Ajan`, `Tip`, `alan`, `arac`, `onay_iste`, `girdiler`, `esik`,
  Turkish CLI commands and `ajan.toml` tables) are removed. Use the English names (`Agent`, `Schema`, `field`,
  `tool`, `require_approval`, `inputs`, `threshold`, `[project]` ...).
- **Breaking:** the `.ajn` shorthand syntax and its compiler are removed; programs are plain Python.
- Removed the web IDE, the Windows desktop app and the agent package library.
- The panel UI, the VS Code / Cursor extension and the JetBrains plugin are English only.
- Fixed crashes found by edge-case testing: a closed stdin during a terminal approval, an unreadable
  `agentflight.toml` or run record, an unknown replay id, `--step` without `--replay`, running a file that is not
  Python, and one broken eval hiding the results of the others.
- Edge-case test suite, ruff, and CI on Linux, Windows and macOS (Python 3.11-3.13).

## 0.3.0

- English API (`Agent`, `llm`, `Schema`, `field`, `tool`, `Eval`, `parallel`, `require_approval`).
- Pydantic models as return and tool parameter types; `async` `@llm` methods.
- The watch panel: one panel for every program on the machine; programs connect by themselves.
- VS Code / Cursor extension and JetBrains plugin.

## 0.2.0

- Python-first usage; sign in with a ChatGPT plan or a Claude subscription (through Claude Code).

## 0.1.0

- First version: agents, typed answers with automatic format fixes, tools and MCP, live control, recording and
  replay, evals with a model judge.
