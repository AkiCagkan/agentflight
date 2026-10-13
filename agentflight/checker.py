"""Static checker: finds mistakes before a program runs (`agentflight check`, editor diagnostics).

Checks:
  - syntax
  - the agent's instructions file exists, its model name is known and the model is reachable
  - @llm methods have a return type and the types they use are defined
  - names in tool lists are defined
  - an eval's target is a real agent method and the eval has inputs
  - argument counts of agent method calls and agent setting names
"""

from __future__ import annotations

import ast
import builtins
import difflib
from pathlib import Path

from .agents import SETTINGS
from .errors import UnknownModel
from .source import has_empty_body, has_llm_decorator, is_agent_class, is_eval_class, is_schema_class

TYPE_NAMES = {"str", "int", "float", "bool", "list", "dict", "tuple", "set", "None", "Any", "Optional", "Literal",
              "Union", "List", "Dict", "object"}
EVAL_INPUT_NAMES = {"inputs", "examples"}
AGENT_METHODS = ("clear_memory", "instructions_text", "instructions_file", "llm_methods")
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def _diagnostic(node, severity, message, line=None):
    return {
        "line": line or getattr(node, "lineno", 1),
        "column": (getattr(node, "col_offset", 0) or 0) + 1,
        "end_line": getattr(node, "end_lineno", None),
        "end_column": (getattr(node, "end_col_offset", None) or 0) + 1
        if getattr(node, "end_col_offset", None) is not None else None,
        "severity": severity,
        "message": message,
    }


def _string_constant(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


class _Checker:
    def __init__(self, tree: ast.Module, folder: Path, filename: str):
        self.tree = tree
        self.folder = folder
        self.filename = filename
        self.diagnostics: list[dict] = []
        self.agents: dict[str, ast.ClassDef] = {}
        self.schemas: dict[str, ast.ClassDef] = {}
        self.evals: dict[str, ast.ClassDef] = {}
        self.other_classes: set[str] = set()
        self.defined: set[str] = set(dir(builtins))
        self.imported: set[str] = set()
        self._provider_status: dict[str, tuple[bool, str]] = {}

    def add(self, *args, **kw):
        self.diagnostics.append(_diagnostic(*args, **kw))

    # --- collection
    def collect(self):
        for stmt in ast.walk(self.tree):
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                for alias in stmt.names:
                    name = (alias.asname or alias.name).split(".")[0]
                    self.imported.add(name)
                    self.defined.add(name)
        for stmt in self.tree.body:
            if isinstance(stmt, ast.ClassDef):
                self.defined.add(stmt.name)
                if is_agent_class(stmt):
                    self.agents[stmt.name] = stmt
                elif is_schema_class(stmt):
                    self.schemas[stmt.name] = stmt
                elif is_eval_class(stmt):
                    self.evals[stmt.name] = stmt
                else:
                    self.other_classes.add(stmt.name)
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.defined.add(stmt.name)
            elif isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                for target in targets:
                    for node in ast.walk(target):
                        if isinstance(node, ast.Name):
                            self.defined.add(node.id)
            elif isinstance(stmt, (ast.For, ast.With, ast.If, ast.Try, ast.While)):
                for node in ast.walk(stmt):
                    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                        self.defined.add(node.id)
                    elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                        self.defined.add(node.name)

    def llm_methods(self, class_name: str) -> dict[str, ast.FunctionDef]:
        cls = self.agents.get(class_name)
        if cls is None:
            return {}
        methods = {}
        for base in cls.bases:
            if isinstance(base, ast.Name) and base.id in self.agents and base.id != class_name:
                methods.update(self.llm_methods(base.id))
        for member in cls.body:
            if isinstance(member, ast.FunctionDef) and has_llm_decorator(member):
                methods[member.name] = member
        return methods

    def all_methods(self, class_name: str) -> set[str]:
        cls = self.agents.get(class_name)
        if cls is None:
            return set()
        names = {m.name for m in cls.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for base in cls.bases:
            if isinstance(base, ast.Name) and base.id in self.agents and base.id != class_name:
                names |= self.all_methods(base.id)
        return names

    # --- type expressions
    def check_type(self, expr, context: str):
        if expr is None:
            return
        if isinstance(expr, ast.Constant):
            if expr.value is None or isinstance(expr.value, (str, int, bool)):
                return
        if isinstance(expr, ast.Name):
            if expr.id in TYPE_NAMES or expr.id in self.schemas or expr.id in self.imported \
                    or expr.id in self.other_classes:
                return
            if expr.id in self.agents:
                self.add(expr, "error", f"{context}: '{expr.id}' is an agent; it cannot be used as a type")
                return
            suggestions = difflib.get_close_matches(expr.id, list(TYPE_NAMES | set(self.schemas)), n=1)
            hint = f" (did you mean '{suggestions[0]}'?)" if suggestions else ""
            self.add(expr, "error", f"{context}: unknown type '{expr.id}'{hint}")
            return
        if isinstance(expr, ast.Subscript):
            if isinstance(expr.value, ast.Name) and expr.value.id == "Literal":
                return
            self.check_type(expr.value, context)
            inner = expr.slice
            for item in (inner.elts if isinstance(inner, ast.Tuple) else [inner]):
                self.check_type(item, context)
            return
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.BitOr):
            self.check_type(expr.left, context)
            self.check_type(expr.right, context)
            return
        if isinstance(expr, ast.Attribute):
            return

    # --- agents
    def provider_status(self, model: str):
        from .providers import find_provider

        provider, _ = find_provider(model)
        if provider.name not in self._provider_status:
            self._provider_status[provider.name] = provider.is_ready()
        return self._provider_status[provider.name]

    def check_agent(self, cls: ast.ClassDef):
        doc = ast.get_docstring(cls, clean=True)
        for member in cls.body:
            if isinstance(member, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "instructions"
                                                      for t in member.targets):
                doc = _string_constant(member.value) or doc or "-"
        if not doc:
            if not any(isinstance(b, ast.Name) and b.id in self.agents for b in cls.bases):
                self.add(cls, "warning",
                         f"{cls.name}: no instructions; write them (or a .md file path) as the class docstring")
        else:
            first = doc.splitlines()[0].strip()
            if first.endswith((".md", ".txt")) and " " not in first:
                if not (self.folder / first).is_file():
                    self.add(cls.body[0], "error", f"{cls.name}: instructions file not found: {first}")
        for member in cls.body:
            if isinstance(member, ast.Assign) and len(member.targets) == 1 and isinstance(member.targets[0], ast.Name):
                name = member.targets[0].id
                if name == "model":
                    model = _string_constant(member.value)
                    if model:
                        try:
                            ready, reason = self.provider_status(model)
                            if not ready:
                                self.add(member.value, "warning",
                                         f"{cls.name}: model '{model}' is not available here: {reason}")
                        except UnknownModel as e:
                            self.add(member.value, "error", f"{cls.name}: {e}")
                elif name == "tools":
                    self.check_tools(cls, member.value)
                elif name == "budget":
                    if isinstance(member.value, ast.Constant) and isinstance(member.value.value, (int, float)) \
                            and member.value.value <= 0:
                        self.add(member.value, "error", f"{cls.name}: budget must be greater than zero")
                elif name == "effort":
                    value = _string_constant(member.value)
                    if value and value not in EFFORT_LEVELS:
                        self.add(member.value, "warning", f"{cls.name}: effort must be low/medium/high/xhigh/max")
                elif name not in SETTINGS and name != "instructions":
                    close = difflib.get_close_matches(name, SETTINGS, n=1, cutoff=0.75)
                    if close:
                        self.add(member.targets[0], "warning",
                                 f"{cls.name}: '{name}' is not an agent setting; did you mean '{close[0]}'?")
            elif isinstance(member, ast.FunctionDef) and has_empty_body(member) and not has_llm_decorator(member):
                self.add(member, "warning",
                         f"{cls.name}.{member.name}: empty body but no @llm; add @llm to bind it to the model")
            elif isinstance(member, ast.FunctionDef) and has_llm_decorator(member):
                context = f"{cls.name}.{member.name}"
                if member.returns is None:
                    self.add(member, "error",
                             f"{context}: an @llm method needs a return type (e.g. -> str or -> Summary)")
                else:
                    self.check_type(member.returns, context + " return type")
                params = member.args.args[1:] + member.args.kwonlyargs
                if not member.args.args or member.args.args[0].arg != "self":
                    self.add(member, "error", f"{context}: the first parameter must be 'self'")
                for p in params:
                    if p.annotation is None:
                        self.add(p, "warning", f"{context}: parameter '{p.arg}' has no type annotation")
                    else:
                        self.check_type(p.annotation, f"{context}({p.arg})")

    def check_tools(self, cls, expr):
        items = expr.elts if isinstance(expr, (ast.List, ast.Tuple)) else []
        for item in items:
            if isinstance(item, ast.Starred):
                item = item.value
            if isinstance(item, ast.Name) and item.id not in self.defined:
                self.add(item, "error", f"{cls.name}: no tool named '{item.id}' is defined")

    # --- schemas
    def check_schema(self, cls: ast.ClassDef):
        for member in cls.body:
            if isinstance(member, ast.AnnAssign):
                name = member.target.id if isinstance(member.target, ast.Name) else "?"
                self.check_type(member.annotation, f"{cls.name}.{name}")
            elif isinstance(member, ast.Assign):
                for target in member.targets:
                    if isinstance(target, ast.Name):
                        self.add(member, "warning",
                                 f"{cls.name}.{target.id}: schema fields need a type (e.g. {target.id}: str)")
            elif isinstance(member, ast.Expr) and isinstance(member.value, ast.Constant):
                continue

    # --- evals
    def check_eval(self, cls: ast.ClassDef):
        target = None
        for kw in cls.keywords:
            if kw.arg == "target":
                target = kw.value.body if isinstance(kw.value, ast.Lambda) else kw.value
        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
            agent_name, method = target.value.id, target.attr
            if agent_name in self.agents:
                if method not in self.all_methods(agent_name):
                    self.add(target, "error", f"eval {cls.name}: agent {agent_name} has no method named '{method}'")
            elif agent_name not in self.defined:
                self.add(target, "error", f"eval {cls.name}: '{agent_name}' is not defined")
        assigned = {t.id for m in cls.body if isinstance(m, ast.Assign) for t in m.targets if isinstance(t, ast.Name)}
        if not (EVAL_INPUT_NAMES & assigned):
            self.add(cls, "error", f"eval {cls.name}: define 'inputs' or 'examples'")
        for member in cls.body:
            if isinstance(member, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "threshold"
                                                      for t in member.targets):
                if isinstance(member.value, ast.Constant) and isinstance(member.value.value, (int, float)) \
                        and not 0 <= member.value.value <= 1:
                    self.add(member.value, "error", f"eval {cls.name}: threshold must be between 0 and 1 (e.g. 0.8)")

    # --- calls
    def check_calls(self):
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call):
                continue
            # Agent(...) constructor
            if isinstance(node.func, ast.Name) and node.func.id in self.agents:
                for kw in node.keywords:
                    if kw.arg and kw.arg not in SETTINGS:
                        self.add(kw.value, "error",
                                 f"{node.func.id}(...): unknown setting '{kw.arg}' (valid: {', '.join(SETTINGS)})")
                if node.args:
                    self.add(node, "error", f"{node.func.id}(...): agent settings must be given as name=value")
            # Agent(...).method(...)
            if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Call) \
                    and isinstance(node.func.value.func, ast.Name) and node.func.value.func.id in self.agents:
                agent_name, method = node.func.value.func.id, node.func.attr
                methods = self.llm_methods(agent_name)
                if method not in self.all_methods(agent_name) and method not in AGENT_METHODS:
                    self.add(node.func, "error", f"agent {agent_name} has no method named '{method}'")
                    continue
                if method in methods:
                    self.check_arguments(node, methods[method], f"{agent_name}.{method}")
            if isinstance(node.func, ast.Name) and node.func.id == "require_approval" and len(node.args) > 2:
                self.add(node, "error", f"{node.func.id}(value, message) takes at most two arguments")

    def check_arguments(self, call: ast.Call, fn: ast.FunctionDef, name: str):
        if any(isinstance(a, ast.Starred) for a in call.args) or any(k.arg is None for k in call.keywords):
            return
        params = [p.arg for p in fn.args.args[1:]]
        default_count = len(fn.args.defaults)
        required = params[: len(params) - default_count] if default_count else params
        given = len(call.args)
        keywords = {k.arg for k in call.keywords}
        if given > len(params):
            self.add(call, "error", f"{name}: takes at most {len(params)} arguments, {given} given")
            return
        missing = [p for p in required[given:] if p not in keywords]
        if missing:
            self.add(call, "error", f"{name}: missing argument: {', '.join(missing)}")
        for kw in keywords:
            if kw not in params + [p.arg for p in fn.args.kwonlyargs]:
                self.add(call, "error", f"{name}: unknown argument '{kw}'")

    def run(self):
        self.collect()
        for cls in self.agents.values():
            self.check_agent(cls)
        for cls in self.schemas.values():
            self.check_schema(cls)
        for cls in self.evals.values():
            self.check_eval(cls)
        self.check_calls()
        return sorted(self.diagnostics, key=lambda d: (d["line"], d["column"]))


def check_source(source: str, filename: str, folder: Path | None = None) -> list[dict]:
    folder = Path(folder) if folder else Path(filename).resolve().parent
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as e:
        return [{
            "line": e.lineno or 1, "column": e.offset or 1, "end_line": getattr(e, "end_lineno", None),
            "end_column": getattr(e, "end_offset", None), "severity": "error",
            "message": f"syntax error: {e.msg}",
        }]
    return _Checker(tree, folder, filename).run()


def check_file(path) -> list[dict]:
    path = Path(path)
    return check_source(path.read_text(encoding="utf-8"), str(path), path.resolve().parent)


__all__ = ["check_source", "check_file"]
