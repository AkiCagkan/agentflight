# AgentFlight for VS Code and Cursor

Watch, pause, approve and replay LLM agents written with the AgentFlight Python
library, without leaving the editor.

- **Agents panel** (bottom panel, next to the terminal): every agent call of every running program, live —
  tool calls, the streamed answer, format errors and fixes, cost or "subscription". Pause an agent, send it a
  message, kill it, approve `require_approval(...)` steps, re-run a recorded run from any step, browse the run
  history and eval results, connect your ChatGPT / Claude accounts.
- **Run with AgentFlight** (▶ in the editor title, context menu, and a CodeLens above every agent): runs the file in
  the terminal; the run shows up in the panel.
- **Run eval** CodeLens above every `class ...(Eval, target=...)`.
- **Running lines**: the lines whose agent calls are running are highlighted; a line waiting for approval is
  marked and a notification appears.
- **Model names**: inside `model = "` you get the models of your connected accounts and API keys.
- **Check on save**: `agentflight check` runs when you save, problems show in the Problems view.
- **Snippets**: `agent`, `schema`, `eval`, `tool`, `parallel`, `from agentflight`.

Programs started anywhere (`python main.py` in any terminal) show up in the same panel.

## Requirements

Python with the `agentflight` package (`pip install -e .` from the AgentFlight repository; `pip install agentflight` once it is
on PyPI). The extension uses the interpreter selected in the
Python extension, or the `agentflight.pythonPath` setting.

## Commands

| Command | |
|---|---|
| AgentFlight: Open Panel | Show the agents panel (starts `agentflight watch` if needed) |
| AgentFlight: Run File with AgentFlight | `python -m agentflight run FILE` in the "AgentFlight" terminal |
| AgentFlight: Run Evals in File | `python -m agentflight eval FILE` in the "AgentFlight" terminal |
| AgentFlight: Connect Accounts (ChatGPT, Claude) | Open the panel's accounts dialog |
| AgentFlight: Open Panel in an Editor Tab | The panel as an editor tab instead of the bottom panel |
| AgentFlight: Stop the Panel Server | `python -m agentflight watch --stop` |

## Settings

| Setting | Default | |
|---|---|---|
| `agentflight.pythonPath` | (Python extension) | Interpreter that has the `agentflight` package |
| `agentflight.panelPort` | 7790 | Port of the panel server (`agentflight watch`) |
| `agentflight.codeLens` | true | Run / Eval links above agents and evals |
| `agentflight.runningLines` | true | Highlight running lines |
| `agentflight.checkOnSave` | true | Run `agentflight check` on save |
