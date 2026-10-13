"""AST helpers for AgentFlight programs: recognise agent, schema and eval classes and strip program code."""

from __future__ import annotations

import ast

_FLOW_FUNCTIONS = ("require_approval", "parallel", "parallel_map", "parallel_for_each")


def _is_base(node: ast.expr, name: str) -> bool:
    """`Name` (after `from agentflight import Name`) or `agentflight.Name`."""
    if isinstance(node, ast.Name):
        return node.id == name
    return (
        isinstance(node, ast.Attribute)
        and node.attr == name
        and isinstance(node.value, ast.Name)
        and node.value.id == "agentflight"
    )


def has_llm_decorator(fn) -> bool:
    for decorator in getattr(fn, "decorator_list", []):
        if isinstance(decorator, ast.Call):
            decorator = decorator.func
        if (isinstance(decorator, ast.Name) and decorator.id == "llm") or (
                isinstance(decorator, ast.Attribute) and decorator.attr == "llm"):
            return True
    return False


def is_agent_class(node: ast.ClassDef) -> bool:
    return any(_is_base(base, "Agent") for base in node.bases)


def is_schema_class(node: ast.ClassDef) -> bool:
    return any(_is_base(base, "Schema") for base in node.bases)


def is_eval_class(node: ast.ClassDef) -> bool:
    return any(_is_base(base, "Eval") for base in node.bases)


def has_empty_body(fn: ast.FunctionDef) -> bool:
    """True if the body is only `...` (optionally after a docstring)."""
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    return (
        len(body) == 1
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and body[0].value.value is Ellipsis
    )


def _contains_call(node, predicate) -> bool:
    return any(isinstance(n, ast.Call) and predicate(n) for n in ast.walk(node))


def definitions_only(tree: ast.Module) -> ast.Module:
    """Drops the program code and keeps only the definitions (agents, schemas, tools, evals, imports...)."""
    agent_methods: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.ClassDef) and is_agent_class(stmt):
            agent_methods |= {m.name for m in stmt.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def is_agent_call(call: ast.Call):
        f = call.func
        if isinstance(f, ast.Attribute) and f.attr in agent_methods:
            return True
        if isinstance(f, ast.Name) and f.id in _FLOW_FUNCTIONS:
            return True
        return isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "agentflight" \
            and f.attr in _FLOW_FUNCTIONS

    def assigned_names(stmt) -> set[str]:
        return {n.id for n in ast.walk(stmt) if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del))}

    def read_names(stmt) -> set[str]:
        return {n.id for n in ast.walk(stmt) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}

    kept = []
    dropped: set[str] = set()  # names set by dropped program code; assignments that read them are dropped too
    for stmt in tree.body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            kept.append(stmt)
        elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            kept.append(stmt)
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and not _contains_call(stmt, is_agent_call) \
                and not (read_names(stmt) & dropped):
            kept.append(stmt)
        elif isinstance(stmt, (ast.Try,)) and all(isinstance(s, (ast.Import, ast.ImportFrom)) for s in stmt.body):
            kept.append(stmt)
        else:
            dropped |= assigned_names(stmt)
    tree.body = kept
    return tree
