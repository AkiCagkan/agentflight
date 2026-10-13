"""A small MCP server (stdio, JSON-RPC 2.0) for the tests.

Tools:
  calculate(expression)               safe arithmetic
  convert_unit(value, source, target) conversion between a few engineering units
"""

import ast
import json
import operator
import sys

OPERATORS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
             ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos, ast.Mod: operator.mod}

CONVERSIONS = {
    ("kw", "kcal/h"): 859.845, ("kcal/h", "kw"): 1 / 859.845,
    ("bar", "psi"): 14.5038, ("psi", "bar"): 1 / 14.5038,
    ("bar", "kpa"): 100.0, ("kpa", "bar"): 0.01,
    ("ton/h", "kg/s"): 1000 / 3600, ("kg/s", "ton/h"): 3600 / 1000,
}


def _evaluate(node):
    if isinstance(node, ast.Expression):
        return _evaluate(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in OPERATORS:
        return OPERATORS[type(node.op)](_evaluate(node.left), _evaluate(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in OPERATORS:
        return OPERATORS[type(node.op)](_evaluate(node.operand))
    raise ValueError("only numbers and + - * / ** % are allowed")


def calculate(expression):
    result = _evaluate(ast.parse(expression, mode="eval"))
    return str(int(result) if isinstance(result, float) and result.is_integer() else result)


def convert_unit(value, source, target):
    s, t = source.strip().lower(), target.strip().lower()
    if (s, t) in CONVERSIONS:
        return f"{value * CONVERSIONS[(s, t)]:.4g} {target}"
    if (s, t) == ("c", "k"):
        return f"{value + 273.15:.4g} K"
    if (s, t) == ("k", "c"):
        return f"{value - 273.15:.4g} C"
    raise ValueError(f"unsupported conversion: {source} -> {target}")


TOOLS = [
    {"name": "calculate", "description": "Evaluates an arithmetic expression, e.g. '2 * (3 + 4)'.",
     "inputSchema": {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]}},
    {"name": "convert_unit", "description": "Converts units: kW<->kcal/h, bar<->psi, bar<->kPa, ton/h<->kg/s, C<->K.",
     "inputSchema": {"type": "object", "properties": {"value": {"type": "number"}, "source": {"type": "string"},
                                                     "target": {"type": "string"}},
                     "required": ["value", "source", "target"]}},
]


def reply(request_id, result=None, error=None):
    data = {"jsonrpc": "2.0", "id": request_id}
    if error:
        data["error"] = {"code": -32000, "message": error}
    else:
        data["result"] = result
    sys.stdout.write(json.dumps(data, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    sys.stdin.reconfigure(encoding="utf-8")   # MCP messages are UTF-8 (not the Windows default code page)
    sys.stdout.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, request_id = request.get("method"), request.get("id")
        if request_id is None:
            continue
        if method == "initialize":
            reply(request_id, {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                               "serverInfo": {"name": "test-calc", "version": "0.1"}})
        elif method == "tools/list":
            reply(request_id, {"tools": TOOLS})
        elif method == "tools/call":
            name = request["params"]["name"]
            arguments = request["params"].get("arguments", {})
            try:
                text = calculate(**arguments) if name == "calculate" else convert_unit(**arguments)
                reply(request_id, {"content": [{"type": "text", "text": text}]})
            except Exception as e:  # noqa: BLE001
                reply(request_id, {"content": [{"type": "text", "text": str(e)}], "isError": True})
        else:
            reply(request_id, error=f"unknown method: {method}")


if __name__ == "__main__":
    main()
