"""Runs AgentFlight programs and their evals (shared entry point of the CLI and the panel)."""

from __future__ import annotations

import ast
import builtins
import json
import os
import sys
import time
import traceback
import types
from pathlib import Path

from . import pricing
from .console import ConsoleWriter, gray, red, yellow
from .errors import AgentError, ApprovalRejected, SetupError
from .project import default_model, find_root, read_config, total_budget
from .recording import Recorder, ReplayStore, new_run_id, runs_dir
from .runtime import Run, is_user_file, set_current_run
from .source import definitions_only


def _percent(rate: float) -> str:
    return f"{rate * 100:.0f}%"


def prepare_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if os.name == "nt":
        try:  # ANSI colours
            import ctypes
            k = ctypes.windll.kernel32
            k.SetConsoleMode(k.GetStdHandle(-11), 7)
            k.SetConsoleMode(k.GetStdHandle(-12), 7)
        except Exception:  # noqa: BLE001 - colors are optional
            pass


def _user_frames(exc: BaseException):
    """Frames of the traceback that belong to the user's own files."""
    return [f for f in traceback.extract_tb(exc.__traceback__) if is_user_file(f.filename)]


def error_report(exc: BaseException) -> dict:
    """Summarises an error in user code with the lines of the user's own files."""
    if isinstance(exc, SyntaxError):
        return {"error_type": "SyntaxError", "message": exc.msg, "file": exc.filename, "line": exc.lineno,
                "column": exc.offset, "stack": []}
    frames = _user_frames(exc)
    last = frames[-1] if frames else None
    return {
        "error_type": type(exc).__name__,
        "message": str(exc),
        "file": last.filename if last else None,
        "line": last.lineno if last else None,
        "stack": [{"file": f.filename, "line": f.lineno, "code": f.line, "function": f.name} for f in frames],
    }


def print_error(report: dict, stream=None) -> None:
    stream = stream or sys.stderr
    for frame in report.get("stack", []):
        print(f"  {Path(frame['file']).name}:{frame['line']}  {frame.get('code') or ''}", file=stream)
    where = f" ({Path(report['file']).name}:{report['line']})" if report.get("file") else ""
    print(red(f"{report['error_type']}{where}: {report['message']}"), file=stream)


def _print_diagnostics(diagnostics, file, stream):
    for d in diagnostics:
        color = red if d["severity"] == "error" else yellow
        print(color(f"{Path(file).name}:{d['line']}:{d['column']}: {d['severity']}: {d['message']}"), file=stream)


def _find_panel():
    """Info of an open panel, if any (disabled with AGENTFLIGHT_PANEL=0)."""
    if os.environ.get("AGENTFLIGHT_PANEL", "").strip() in ("0", "false", "no"):
        return None
    try:
        from .panel.discovery import panel_info
        return panel_info()
    except Exception:  # noqa: BLE001 - without a panel the program runs normally
        return None


def _add_to_path(folder) -> None:
    folder = str(Path(folder).resolve())
    if folder not in sys.path:
        sys.path.insert(0, folder)


class _Session:
    """Run + listeners + recording set up for one run.

    record: True / False / "panel" (record only if a panel is connected)."""

    def __init__(self, file: Path, *, model, approval, budget, record, replay, step, kind, extra_meta=None,
                 command=None):
        self.file = Path(file).resolve()
        self.project = find_root(self.file)
        try:
            self.config = read_config(self.project)
            replay_store = ReplayStore.from_run(self.project, replay, step) if replay else None
        except (ValueError, OSError) as e:
            raise SetupError(str(e)) from None
        pricing.add_prices(self.config.get("prices") or {})
        self.run_id = new_run_id()
        socket_address = os.environ.get("AGENTFLIGHT_PANEL_SOCKET")
        panel = None if socket_address else _find_panel()
        if record == "panel":
            record = panel is not None
        self.recorder = None
        self.mirrors = []
        if record:
            self.recorder = Recorder(runs_dir(self.project) / self.run_id, {
                "run_id": self.run_id, "kind": kind, "file": str(self.file), "file_name": self.file.name,
                "started_at": time.strftime("%Y-%m-%d %H:%M:%S"), "model": model, "replay": replay, "step": step,
                "python": sys.executable, "cwd": os.getcwd(), "env_model": os.environ.get("AGENTFLIGHT_MODEL") or None,
                **(extra_meta or {}),
            })
        self.run = Run(
            run_id=self.run_id, project=self.project, model=model,
            approval="panel" if ((socket_address or panel) and approval == "ask") else approval,
            total_budget=budget if budget is not None else total_budget(self.config),
            default_model=default_model(self.config), recorder=self.recorder, replay=replay_store,
        )
        self.panel = None
        if socket_address:
            from .panel.client import PanelConnection
            token = os.environ.get("AGENTFLIGHT_PANEL_TOKEN", "")
            self.panel = PanelConnection(socket_address, token, self.run.control,
                                         self.run_id)
            self.run.bus.add(self.panel)
            self.run.control.panel_connected = True
        else:
            self.run.bus.add(ConsoleWriter(sys.stderr))
            if panel is not None:
                self._connect_panel(panel, kind, model, command)
        set_current_run(self.run)
        _add_to_path(self.file.parent)
        _add_to_path(self.project)

    def _connect_panel(self, panel: dict, kind: str, model, command) -> None:
        from .panel.client import OutputMirror, PanelConnection
        external = {
            "file": str(self.file), "kind": kind, "pid": os.getpid(), "python": sys.executable, "cwd": os.getcwd(),
            "project": str(self.project), "model": model, "run_id": self.run_id,
            "env_model": os.environ.get("AGENTFLIGHT_MODEL") or None,
            "command": command or f"python {self.file.name}",
        }
        try:
            self.panel = PanelConnection(f"127.0.0.1:{panel['runner_port']}", f"{panel['token']}:", self.run.control,
                                         self.run_id, external=external)
        except OSError:
            self.panel = None
            return
        self.run.bus.add(self.panel)
        self.run.control.panel_connected = True
        # Program output also goes to the panel's "Output" tab (the terminal output does not change).
        for name in ("stdout", "stderr"):
            original = getattr(sys, name)
            if original is not None and not isinstance(original, OutputMirror):
                mirror = OutputMirror(original, self.panel, name)
                setattr(sys, name, mirror)
                self.mirrors.append((name, original, mirror))
        print(gray(f"↗ connected to the AgentFlight panel: {panel['url']}"),
              file=self.mirrors[1][1] if len(self.mirrors) > 1 else sys.stderr)

    def finish(self, status: str, extra: dict | None = None) -> dict:
        run = self.run
        summary = {
            "status": status, "duration_ms": run.bus.elapsed_ms, "total_cost": round(run.total_cost, 6),
            "total_tokens": run.total_input_tokens + run.total_output_tokens, "input_tokens": run.total_input_tokens,
            "output_tokens": run.total_output_tokens, "call_count": run.call_count, "run_id": self.run_id,
            "recorded": self.recorder is not None, "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"), **(extra or {}),
        }
        for name, original, mirror in self.mirrors:
            try:
                mirror.flush()
                mirror.drain()
            except Exception:  # noqa: BLE001
                pass
            if getattr(sys, name) is mirror:
                setattr(sys, name, original)
        self.mirrors = []
        run.bus.publish("run_finished", **summary)
        if self.recorder is not None:
            self.recorder.close(summary)
        if self.panel is not None:
            self.panel.close()
        return summary


def _load_module(file: Path, name: str, only_definitions: bool = False):
    """Compiles a `.py` file into a fresh module; `only_definitions` drops the program code (for evals)."""
    source = file.read_text(encoding="utf-8")
    module = types.ModuleType(name)
    module.__file__ = str(file)
    module.__builtins__ = builtins
    tree = ast.parse(source, filename=str(file))
    if only_definitions:
        tree = definitions_only(tree)
    code = compile(tree, str(file), "exec")
    sys.modules[name] = module
    exec(code, module.__dict__)  # noqa: S102 - the user's program
    return module


def run_program(file, *, model=None, approval="ask", budget=None, record=True, replay=None, step=None,
                args=None, check=True) -> int:
    from .checker import check_file

    command = " ".join(["agentflight run", Path(file).name] + list(args or []))
    session = _Session(file, model=model, approval=approval, budget=budget, record=record, replay=replay, step=step,
                       kind="program", command=command)
    run = session.run
    run.bus.publish("run_started", run_id=session.run_id, file=str(session.file), file_name=session.file.name,
                    model=model or run.default_model or "auto", replay=replay, step=step, kind="program",
                    project=str(session.project))
    if check:
        diagnostics = check_file(session.file)
        if diagnostics:
            run.bus.publish("diagnostics", file=str(session.file), diagnostics=diagnostics)
        errors = [d for d in diagnostics if d["severity"] == "error"]
        if session.panel is None:
            _print_diagnostics(diagnostics, session.file, sys.stderr)
        if errors:
            session.finish("check_failed", {"error": {"error_type": "CheckError", "message": errors[0]["message"],
                                                      "file": str(session.file), "line": errors[0]["line"]}})
            return 2

    old_argv = sys.argv
    sys.argv = [str(session.file)] + list(args or [])
    old_main = sys.modules.get("__main__")
    old_cwd = os.getcwd()
    os.chdir(session.file.parent)  # the program runs in its own folder (for relative file paths)
    status, code, extra = "done", 0, {}
    try:
        _load_module(session.file, "__main__", only_definitions=False)
    except KeyboardInterrupt:
        status, code = "cancelled", 130
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        status = "done" if code == 0 else "error"
    except BaseException as e:  # noqa: BLE001 - every error of the user's program is reported
        report = error_report(e)
        status, code, extra = ("rejected" if isinstance(e, ApprovalRejected) else "error"), 1, {"error": report}
        run.bus.publish("program_error", **report)
        if session.panel is None:
            print_error(report)
        else:
            print(f"{report['error_type']}: {report['message']}", file=sys.stderr)
            if not isinstance(e, (AgentError, SyntaxError)) and not report["stack"]:
                traceback.print_exc()
    finally:
        sys.argv = old_argv
        os.chdir(old_cwd)
        if old_main is not None:
            sys.modules["__main__"] = old_main
        sys.stdout.flush()
        session.finish(status, extra)
    return code


def run_evals(file, *, name=None, model=None, record=True, json_path=None, quiet=False,
              write_report=True) -> tuple[int, list[dict]]:
    from .evals import registered_evals, run_eval

    session = _Session(file, model=model, approval="auto", budget=None, record=record, replay=None, step=None,
                       kind="eval", command=f"agentflight eval {Path(file).name}")
    run = session.run
    run.bus.publish("run_started", run_id=session.run_id, file=str(session.file), file_name=session.file.name,
                    model=model or run.default_model or "auto", kind="eval", project=str(session.project))
    module_name = "__agentflight_eval__"
    results: list[dict] = []
    try:
        _load_module(session.file, module_name, only_definitions=True)
        evals = [e for e in registered_evals(module_name) if name is None or e.__name__ == name]
        if not evals:
            print(yellow("This file defines no evals." if name is None else f"No eval named '{name}'."),
                  file=sys.stderr)
        for e in evals:
            results.append(run_eval(e))
    except BaseException as e:  # noqa: BLE001
        report = error_report(e)
        run.bus.publish("program_error", **report)
        print_error(report)
        session.finish("error", {"error": report})
        return 1, results
    finally:
        sys.modules.pop(module_name, None)

    all_passed = all(r["passed"] for r in results) if results else False
    summary = session.finish("done", {"evals": [{k: v for k, v in r.items() if k != "samples"} for r in results]})
    report = {"file": str(session.file), "time": time.strftime("%Y-%m-%d %H:%M:%S"), "run_id": session.run_id,
              "model": model or run.default_model or "auto", "cost": summary["total_cost"], "evals": results}
    if write_report:
        folder = session.project / ".agentflight" / "evals"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{session.run_id}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                                       encoding="utf-8")
        (folder / "latest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if json_path:
        Path(json_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if not quiet and session.panel is None:
        print_eval_report(results)
    return (0 if all_passed else 1), results


def print_eval_report(results: list[dict], stream=None) -> None:
    from .console import bold, green

    stream = stream or sys.stdout
    if not results:
        return
    print("\n" + bold("EVAL RESULTS"), file=stream)
    for r in results:
        if r.get("error"):
            print(f"  {red('ERROR')}  {r['name']:<24} {r['error']}", file=stream)
            continue
        mark = green("PASS ") if r["passed"] else red("FAIL ")
        print(f"  {mark}  {r['name']:<24} {r['passed_count']}/{r['sample_count']} = " + _percent(r["rate"])
              + gray(f"  (threshold {_percent(r['threshold'])} · {r['target']} · ${r['cost']:.4f} · "
                     f"{r['duration_ms'] / 1000:.1f} s)"),
              file=stream)
        for s in r["samples"]:
            if not s["passed"]:
                print(gray(f"        ✗ #{s['index'] + 1} {str(s.get('input'))[:50]!r}: {s.get('note', '')}"),
                      file=stream)


# ---------------------------------------------------------------------------
# `python main.py`: programs started without `agentflight run`

_AUTO_SESSION: _Session | None = None


def auto_session():
    """Set up on the first agent call of a program started directly (`python main.py`): applies the project's
    agentflight.toml, connects to an open panel and records the run there. The run is closed when the program ends
    (or on an uncaught error)."""
    global _AUTO_SESSION
    import atexit

    script = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if script is None or script.suffix.lower() != ".py" or not script.is_file():
        return None
    if Path(__file__).resolve().parent in script.resolve().parents:
        return None
    session = _Session(script, model=None, approval="ask", budget=None, record="panel", replay=None, step=None,
                       kind="program", command=" ".join(["python", script.name] + sys.argv[1:]))
    _AUTO_SESSION = session
    run = session.run
    run.bus.publish("run_started", run_id=session.run_id, file=str(session.file), file_name=session.file.name,
                    model=run.default_model or "auto", kind="program", project=str(session.project))
    state = {"finished": False, "error": None}
    old_hook = sys.excepthook

    def error_hook(exc_type, exc, tb):
        try:
            report = error_report(exc)
            run.bus.publish("program_error", **report)
            state["error"] = report
        except Exception:  # noqa: BLE001
            pass
        old_hook(exc_type, exc, tb)

    def close():
        if state["finished"]:
            return
        state["finished"] = True
        try:
            sys.stdout.flush()
        except Exception:  # noqa: BLE001
            pass
        if state["error"]:
            status = "rejected" if state["error"].get("error_type") == "ApprovalRejected" else "error"
            session.finish(status, {"error": state["error"]})
        else:
            session.finish("done")

    sys.excepthook = error_hook
    atexit.register(close)
    return run
