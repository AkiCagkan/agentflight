"""A fake `claude` program for the tests: imitates Claude Code's `-p` stream-json behaviour.

Environment variables:
  FAKE_CLAUDE_SIGNED_IN  "0" makes it look signed out
  FAKE_CLAUDE_SCENARIO   tool (default) | format (the first answer does not match the schema) | not_signed_in
  FAKE_CLAUDE_LOG        file to write the call arguments, part of the environment and the system prompt to
"""

import json
import os
import sys
import urllib.request

args = sys.argv[1:]


def arg(name):
    return args[args.index(name) + 1] if name in args else None


if args[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": os.environ.get("FAKE_CLAUDE_SIGNED_IN", "1") == "1", "authMethod": "claude.ai",
                      "email": "test@example.com", "subscriptionType": "max"}))
    sys.exit(0)
if args == ["--version"]:
    print("9.9.9 (Claude Code)")
    sys.exit(0)

sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
log = os.environ.get("FAKE_CLAUDE_LOG")
if log:
    with open(log, "w", encoding="utf-8") as f:
        names = ("CLAUDECODE", "ANTHROPIC_API_KEY", "MCP_TOOL_TIMEOUT", "CLAUDE_CODE_SESSION_ID")
        json.dump({"argv": args, "env": {k: os.environ.get(k) for k in names},
                   "system": open(arg("--system-prompt-file"), encoding="utf-8").read()}, f, ensure_ascii=False)
scenario = os.environ.get("FAKE_CLAUDE_SCENARIO", "tool")
mcp = None
if arg("--mcp-config"):
    with open(arg("--mcp-config"), encoding="utf-8") as f:
        mcp = json.load(f)["mcpServers"]["agentflight"]


def write(o):
    sys.stdout.write(json.dumps(o, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def mcp_call(method, params, request_id=1):
    body = {"jsonrpc": "2.0", "method": method, "params": params}
    if request_id is not None:
        body["id"] = request_id
    request = urllib.request.Request(mcp["url"], data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", **mcp["headers"]})
    with urllib.request.urlopen(request, timeout=60) as response:
        raw = response.read()
    return json.loads(raw)["result"] if raw else None


def stream_turn(blocks, text=""):
    write({"type": "stream_event", "event": {"type": "message_start", "message": {"usage": {"input_tokens": 100}}}})
    if text:
        write({"type": "stream_event", "event": {"type": "content_block_delta",
                                                 "delta": {"type": "text_delta", "text": text}}})
    write({"type": "stream_event", "event": {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
                                             "usage": {"output_tokens": 20}}})
    write({"type": "stream_event", "event": {"type": "message_stop"}})
    write({"type": "assistant", "message": {"content": ([{"type": "text", "text": text}] if text else []) + blocks}})


write({"type": "system", "subtype": "init", "session_id": "s1",
       "mcp_servers": [{"name": "agentflight", "status": "connected"}] if mcp else []})
if mcp:
    mcp_call("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}})
    mcp_call("notifications/initialized", {}, request_id=None)

turn = 0
for line in sys.stdin:
    if not line.strip():
        continue
    turn += 1
    if scenario == "not_signed_in":
        write({"type": "result", "subtype": "success", "is_error": True,
               "result": "Not logged in · Please run /login"})
        continue
    value = 7
    if mcp:
        tools = mcp_call("tools/list", {})["tools"]
        name = tools[0]["name"]
        stream_turn([{"type": "tool_use", "id": f"t{turn}", "name": f"mcp__agentflight__{name}",
                      "input": {"a": 2, "b": 3}}],
                    "Calculating with the tool.")
        result = mcp_call("tools/call", {"name": name, "arguments": {"a": 2, "b": 3}})
        write({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{turn}",
                                                        "content": result["content"], "is_error": result["isError"]}]}})
        value = int(result["content"][0]["text"])
    output = {"wrong": 1} if scenario == "format" and turn == 1 else {"value": value, "explanation": f"turn {turn}"}
    stream_turn([{"type": "tool_use", "id": f"s{turn}", "name": "StructuredOutput", "input": output}])
    write({"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(output),
           "structured_output": output, "num_turns": 2, "usage": {"input_tokens": 200, "output_tokens": 40}})
