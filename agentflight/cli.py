"""The `agentflight` command line tool."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .console import gray, green, red, yellow
from .errors import SetupError
from .project import create_project
from .runner import prepare_console, run_evals, run_program

DESCRIPTION = """AgentFlight: a library for writing LLM agents in plain Python, watching them run and testing them.

Common commands:
  agentflight watch                           open the panel: see every agent call live, pause, approve, replay
  agentflight run main.py                     run a program (it shows up in the panel if the panel is open)
  agentflight check main.py                   check a program without running it
  agentflight eval main.py                    run the evals in a file
  agentflight login chatgpt | claude          connect your own account (ChatGPT plan / Claude subscription)
  agentflight run main.py --replay <run> --step 3
                                       steps 1-2 come from the record, live from step 3
  agentflight run main.py -- a b              pass arguments to the program (sys.argv)"""


def _login(account: str, new: bool = False) -> int:
    if account == "chatgpt":
        from .accounts import chatgpt
        info = chatgpt.add_account() if new else chatgpt.start_login()
        print(gray("ChatGPT sign-in opened in your browser. If it did not open, visit:"))
        print(info["url"])
        result = chatgpt.wait_for_login() or {}
        if result.get("status") == "done":
            acc = result["account"]
            print(green(f"✓ ChatGPT connected: {acc['email']}") + gray(f"  ({len(acc['models'])} models)"))
            if not acc["plan_usage"]:
                print(yellow("ChatGPT plan usage was not allowed; GPT agents will not run with this account."))
            else:
                print(gray("GPT agents now use your ChatGPT plan. Usage: " + chatgpt.USAGE_URL))
            return 0
        print(red(result.get("message") or "sign-in did not complete"), file=sys.stderr)
        return 1
    import subprocess

    from .accounts import claude_code
    exe = claude_code.executable()
    if not exe:
        print(red("Claude Code is not installed."), file=sys.stderr)
        print(gray("To install it (Anthropic's official command):  " + claude_code.install_command()))
        return 1
    print(gray("Opening Claude Code's own sign-in (sign in with your Claude account)…"))
    code = subprocess.call(claude_code.command(exe, "auth", "login"), env=claude_code.env())
    status = claude_code.status(refresh=True)
    if status["signed_in"]:
        print(green(f"✓ Claude connected: {status['email']}")
              + gray(f"  ({status['subscription'] or status['auth_method']})"))
        return 0
    return code or 1


def _file_problem(file: str) -> str | None:
    path = Path(file)
    if not path.is_file():
        return f"file not found: {file}"
    if path.suffix.lower() != ".py":
        return f"not a Python file: {file}"
    return None


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agentflight", description=DESCRIPTION,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", metavar="command")

    r = sub.add_parser("run", help="run a program")
    r.add_argument("file", metavar="FILE")
    r.add_argument("--model", help="override the model of every agent (e.g. fake)")
    r.add_argument("--approve", choices=["ask", "auto"], default="ask",
                   help="approval requests: ask, or approve automatically")
    r.add_argument("--budget", type=float, help="maximum spend for this run (USD)")
    r.add_argument("--no-record", action="store_true", help="do not record the run under .agentflight/runs")
    r.add_argument("--replay", metavar="RUN", help="reuse the calls of this recorded run instead of calling the model")
    r.add_argument("--step", type=int, help="with --replay: run live from this step on")
    r.add_argument("--no-check", action="store_true", help="skip the static check before running")

    c = sub.add_parser("check", help="static check without running")
    c.add_argument("files", nargs="+", metavar="FILE")
    c.add_argument("--json", action="store_true")

    e = sub.add_parser("eval", help="run the evals in a file")
    e.add_argument("file", metavar="FILE")
    e.add_argument("--name", help="only this eval")
    e.add_argument("--model")
    e.add_argument("--json", help="write the report to this file")

    w = sub.add_parser("watch", help="open the watch panel")
    w.add_argument("--port", type=int, default=7790)
    w.add_argument("--no-browser", action="store_true", help="do not open a browser")
    w.add_argument("--stop", action="store_true", help="stop a running panel")

    n = sub.add_parser("new", help="create a new project")
    n.add_argument("folder", metavar="DIR")

    li = sub.add_parser("login", help="connect your account: chatgpt or claude")
    li.add_argument("account", choices=["chatgpt", "claude"])
    li.add_argument("--new", action="store_true", help="chatgpt: add another account or workspace")
    lo = sub.add_parser("logout", help="disconnect an account")
    lo.add_argument("account", choices=["chatgpt", "claude"])
    sub.add_parser("accounts", help="connected accounts")
    sub.add_parser("models", help="available models and how they are reached")

    rs = sub.add_parser("runs", help="recorded runs")
    rs.add_argument("--project", default=".")
    sub.add_parser("version", help="version")
    return p


def main(argv=None) -> int:
    prepare_console()
    from . import config
    config.apply_to_env()
    argv = list(sys.argv[1:] if argv is None else argv)
    program_args: list[str] = []
    if "--" in argv:
        split = argv.index("--")
        argv, program_args = argv[:split], argv[split + 1:]

    p = _parser()
    ns = p.parse_args(argv)
    command = ns.command
    if command is None:
        p.print_help()
        return 0

    if command in ("run", "eval"):
        problem = _file_problem(ns.file)
        if command == "run" and ns.step is not None and not ns.replay:
            problem = problem or "--step needs --replay RUN"
        if problem:
            print(red(problem), file=sys.stderr)
            return 2
        try:
            if command == "run":
                return run_program(ns.file, model=ns.model, approval=ns.approve, budget=ns.budget,
                                   record=not ns.no_record, replay=ns.replay, step=ns.step, args=program_args,
                                   check=not ns.no_check)
            code, _ = run_evals(ns.file, name=ns.name, model=ns.model, json_path=ns.json)
            return code
        except SetupError as e:
            print(red(f"error: {e}"), file=sys.stderr)
            return 2

    if command == "check":
        from .checker import check_file

        has_errors = False
        everything = {}
        for file in ns.files:
            problem = _file_problem(file)
            if problem:
                print(red(problem), file=sys.stderr)
                has_errors = True
                continue
            diagnostics = check_file(file)
            everything[file] = diagnostics
            has_errors |= any(d["severity"] == "error" for d in diagnostics)
            if not ns.json:
                if not diagnostics:
                    print(green(f"✓ {file}: no problems"))
                for d in diagnostics:
                    color = red if d["severity"] == "error" else yellow
                    print(color(f"{file}:{d['line']}:{d['column']}: {d['severity']}: {d['message']}"))
        if ns.json:
            print(json.dumps(everything, ensure_ascii=False, indent=2))
        return 1 if has_errors else 0

    if command == "watch":
        from .panel.discovery import start_panel, stop_panel

        if ns.stop:
            return 0 if stop_panel() else 1
        return start_panel(port=ns.port, browser=not ns.no_browser)

    if command == "new":
        folder = Path(ns.folder)
        written = create_project(folder)
        if not written:
            print(yellow(f"nothing to do: {folder.resolve()} already has every project file"))
            return 0
        print(green(f"✓ project created: {folder.resolve()}") + gray(f"  ({', '.join(written)})"))
        print(gray(f"  run:    agentflight run {folder / 'main.py'}\n  panel:  agentflight watch"))
        return 0

    if command == "runs":
        from .console import plural, status_text
        from .project import find_root
        from .recording import list_runs

        for item in list_runs(find_root(Path(ns.project))):
            status = item.get("status", "?")
            color = green if status == "done" else (yellow if status == "running" else red)
            print(f"{item['run_id']}  {color(status_text(status)):<10} {item.get('file_name', '')}"
                  + gray(f"  {plural(item.get('call_count', 0), 'call')} · ${item.get('total_cost', 0):.4f}"))
        return 0

    if command == "models":
        from .providers import auto_model, available_models

        print(gray("automatic choice: " + auto_model()))
        for m in available_models():
            state = green(m["route"] or "ready") if m["ready"] else gray("unavailable: " + m.get("reason", ""))
            print(f"  {m['name']:<28} {m['group']:<7} {state}")
        return 0

    if command == "login":
        return _login(ns.account, new=ns.new)

    if command == "logout":
        if ns.account == "chatgpt":
            from .accounts import chatgpt
            result = chatgpt.logout()
            print(green("✓ signed out of ChatGPT") + ("" if result["revocation_confirmed"] else yellow(
                " (remote revocation could not be confirmed; you can disconnect the app in ChatGPT settings)")))
        else:
            from .accounts import claude_code
            print(green("✓ signed out of Claude Code") if claude_code.logout() else red("Claude Code was not found"))
        return 0

    if command == "accounts":
        from .accounts import chatgpt, claude_code
        gpt = chatgpt.status()
        if gpt["connected"]:
            active = gpt["active"]
            print(green("✓ ChatGPT ") + f"{active['email']}  "
                  + gray(f"{len(active['models'])} models · ChatGPT plan"))
        else:
            print(gray("○ ChatGPT not connected   → agentflight login chatgpt"))
        claude = claude_code.status(refresh=True)
        if claude["signed_in"]:
            print(green("✓ Claude  ") + f"{claude['email']}  "
                  + gray(f"{claude['subscription'] or claude['auth_method']} · Claude Code {claude['version']}"))
        elif claude["installed"]:
            print(gray("○ Claude not connected    → agentflight login claude"))
        else:
            print(gray("○ Claude Code is not installed → " + claude_code.install_command()))
        return 0

    if command == "version":
        print(f"agentflight {__version__}")
        return 0
    return 0
