"""Request/response translation of the providers, tried against fake HTTP servers (no network or keys needed)."""

import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler

from agentflight.errors import AgentError
from agentflight.localhttp import LocalHTTPServer
from agentflight.providers import gemini as gemini_module
from agentflight.providers.base import Request
from agentflight.tools import mcp_server
from tests.helpers import TESTS

TOOLS = [{"name": "add", "description": "Adds two numbers", "schema": {
    "type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
    "required": ["a", "b"], "additionalProperties": False}}]
SCHEMA = {"type": "object", "properties": {"x": {"type": "integer", "minimum": 1}}, "required": ["x"],
          "additionalProperties": False}
STRICT = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"], "additionalProperties": False}


class FakeServer:
    """HTTP server that returns the given responses in order and records the requests it receives."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append({"path": self.path, "body": body,
                                       "headers": {k.lower(): v for k, v in self.headers.items()}})
                code, content_type, data = outer.responses.pop(0)
                if callable(data):
                    data = data(body)
                raw = data.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.http = LocalHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.http.server_address[1]}"
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def close(self):
        self.http.shutdown()
        self.http.server_close()


def sse(packets, event_names=False):
    lines = []
    for p in packets:
        if event_names:
            lines.append(f"event: {p['type']}")
        lines.append("data: " + (p if isinstance(p, str) else json.dumps(p, ensure_ascii=False)))
        lines.append("")
    return "\n".join(lines) + "\n"


def request(**extra):
    base = dict(model="m", system="System", messages=[{"role": "user", "content": "add 1 and 2"}],
                tools=TOOLS, schema=SCHEMA, strict_schema=STRICT, temperature=0.0, seed=7)
    base.update(extra)
    return Request(**base)


class OpenAITest(unittest.TestCase):
    def test_streaming_and_tool_call(self):
        from agentflight.providers.openai_api import OpenAIProvider

        chunks = [
            {"model": "gpt-x", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hel"}}]},
            {"choices": [{"index": 0, "delta": {"content": "lo"}}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "id": "call_1", "type": "function",
                 "function": {"name": "add", "arguments": "{\"a\":"}}]}}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": "1,\"b\":2}"}}]}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
            "[DONE]",
        ]
        server = FakeServer([
            (400, "application/json",
             json.dumps({"error": {"message": "Unsupported value: 'temperature' does not support 0"}})),
            (200, "text/event-stream", sse(chunks)),
        ])
        try:
            provider = OpenAIProvider(base_url=server.url, api_key="k")
            streamed = []
            response = provider.call(request(model="gpt-x"), streamed.append)
            self.assertEqual(response.text, "Hello")
            self.assertEqual("".join(streamed), "Hello")
            self.assertEqual((response.tool_calls[0].name, response.tool_calls[0].arguments), ("add", {"a": 1, "b": 2}))
            self.assertEqual((response.input_tokens, response.output_tokens), (10, 5))
            first, second = server.requests
            self.assertIn("temperature", first["body"])
            self.assertNotIn("temperature", second["body"], "temperature must be dropped when it is not supported")
            body = second["body"]
            self.assertEqual(body["messages"][0], {"role": "system", "content": "System"})
            self.assertEqual(body["tools"][0]["function"]["name"], "add")
            self.assertNotIn("response_format", body, "no schema mode with tools on a compatible server")
            official = OpenAIProvider(base_url="https://api.openai.com/v1", api_key="k")._body(request(model="gpt-x"))
            self.assertEqual(official["response_format"]["json_schema"]["schema"], STRICT)
            local = OpenAIProvider(base_url=server.url, api_key="k")._body(request(model="gpt-x", tools=[]))
            self.assertEqual(local["response_format"]["json_schema"]["schema"], STRICT)
            self.assertEqual(body["seed"], 7)
            self.assertEqual(second["headers"]["authorization"], "Bearer k")
        finally:
            server.close()

    def test_tool_result_message_translation(self):
        from agentflight.providers.openai_api import OpenAIProvider

        messages = [
            {"role": "user", "content": "add"},
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "c1", "name": "add", "arguments": {"a": 1, "b": 2}}]},
            {"role": "tool", "results": [{"id": "c1", "name": "add", "content": "3", "error": False}]},
        ]
        translated = OpenAIProvider._messages(request(messages=messages))
        self.assertEqual(translated[2]["tool_calls"][0]["function"]["arguments"], '{"a": 1, "b": 2}')
        self.assertEqual(translated[3], {"role": "tool", "tool_call_id": "c1", "content": "3"})


class OllamaTest(unittest.TestCase):
    def test_ndjson(self):
        from agentflight.providers.ollama import OllamaProvider

        lines = "\n".join(json.dumps(x) for x in [
            {"message": {"role": "assistant", "content": "Hel"}, "done": False},
            {"message": {"role": "assistant", "content": "lo",
                         "tool_calls": [{"function": {"name": "add", "arguments": {"a": 1, "b": 2}}}]}, "done": False},
            {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop",
             "prompt_eval_count": 12, "eval_count": 7},
        ]) + "\n"
        server = FakeServer([(200, "application/x-ndjson", lines)])
        old = os.environ.get("OLLAMA_HOST")
        os.environ["OLLAMA_HOST"] = server.url
        try:
            response = OllamaProvider().call(request(model="llama"), lambda p: None)
            self.assertEqual(response.text, "Hello")
            self.assertEqual(response.tool_calls[0].arguments, {"a": 1, "b": 2})
            self.assertEqual((response.input_tokens, response.output_tokens), (12, 7))
            body = server.requests[0]["body"]
            self.assertEqual(body["format"], STRICT)
            self.assertEqual(body["options"], {"temperature": 0.0, "seed": 7})
        finally:
            if old is None:
                os.environ.pop("OLLAMA_HOST", None)
            else:
                os.environ["OLLAMA_HOST"] = old
            server.close()


class GeminiTest(unittest.TestCase):
    def test_streaming_signature_and_sending_back(self):
        from agentflight.providers.gemini import GeminiProvider

        packets = [
            {"candidates": [{"content": {"role": "model", "parts": [{"text": "Hel"}]}}],
             "modelVersion": "gemini-x-001"},
            {"candidates": [{"content": {"role": "model", "parts": [{"text": "lo"}]}}]},
            {"candidates": [{"content": {"role": "model", "parts": [
                {"functionCall": {"name": "add", "args": {"a": 1, "b": 2}}, "thoughtSignature": "sig=="}]},
                "finishReason": "STOP"}],
             "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 6, "thoughtsTokenCount": 3}},
        ]
        answer = {"candidates": [{"content": {"role": "model", "parts": [{"text": "{\"x\": 3}"}]},
                                  "finishReason": "STOP"}]}
        server = FakeServer([(200, "text/event-stream", sse(packets)), (200, "text/event-stream", sse([answer]))])
        old_url, old_key = gemini_module.BASE_URL, os.environ.get("GEMINI_API_KEY")
        gemini_module.BASE_URL = server.url
        os.environ["GEMINI_API_KEY"] = "key"
        try:
            provider = GeminiProvider()
            response = provider.call(request(model="gemini-x"), lambda p: None)
            self.assertEqual(response.text, "Hello")
            self.assertEqual(response.tool_calls[0].arguments, {"a": 1, "b": 2})
            self.assertEqual((response.input_tokens, response.output_tokens), (11, 9))
            self.assertEqual(response.raw[0], {"text": "Hello"})
            self.assertEqual(response.raw[1]["thoughtSignature"], "sig==")
            first = server.requests[0]
            self.assertEqual(first["headers"]["x-goog-api-key"], "key")
            self.assertIn(":streamGenerateContent?alt=sse", first["path"])
            self.assertEqual(first["body"]["tools"][0]["functionDeclarations"][0]["parametersJsonSchema"],
                             TOOLS[0]["schema"])
            self.assertNotIn("responseMimeType", first["body"]["generationConfig"], "no JSON mode with tools")

            messages = [
                {"role": "user", "content": "add"},
                {"role": "assistant", "content": response.text, "tool_calls": [], "raw": response.raw,
                 "provider": "gemini"},
                {"role": "tool", "results": [{"id": "g1", "name": "add", "content": "3", "error": False}]},
            ]
            provider.call(request(model="gemini-x", messages=messages, tools=[]), lambda p: None)
            contents = server.requests[1]["body"]["contents"]
            self.assertEqual(contents[1]["parts"][1]["thoughtSignature"], "sig==",
                             "the signature must be sent back unchanged")
            self.assertEqual(contents[2]["parts"][0]["functionResponse"], {"name": "add", "response": {"result": "3"}})
            self.assertEqual(server.requests[1]["body"]["generationConfig"]["responseMimeType"], "application/json")
        finally:
            gemini_module.BASE_URL = old_url
            if old_key is None:
                os.environ.pop("GEMINI_API_KEY", None)
            else:
                os.environ["GEMINI_API_KEY"] = old_key
            server.close()


class AnthropicTest(unittest.TestCase):
    def setUp(self):
        try:
            import anthropic  # noqa: F401
        except ImportError:
            self.skipTest("the anthropic package is not installed")

    def _stream(self):
        return sse([
            {"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant",
                                                  "model": "claude-opus-5-5", "content": [], "stop_reason": None,
                                                  "stop_sequence": None,
                                                  "usage": {"input_tokens": 20, "output_tokens": 1}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hel"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "lo"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1,
             "content_block": {"type": "tool_use", "id": "toolu_1", "name": "add", "input": {}}},
            {"type": "content_block_delta", "index": 1,
             "delta": {"type": "input_json_delta", "partial_json": "{\"a\": 1, "}},
            {"type": "content_block_delta", "index": 1,
             "delta": {"type": "input_json_delta", "partial_json": "\"b\": 2}"}},
            {"type": "content_block_stop", "index": 1},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
             "usage": {"output_tokens": 15}},
            {"type": "message_stop"},
        ], event_names=True)

    def _environment(self, server):
        names = ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "AGENTFLIGHT_CLAUDE_FALLBACK")
        self._saved = {k: os.environ.get(k) for k in names}
        os.environ["ANTHROPIC_BASE_URL"] = server.url
        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        os.environ.pop("AGENTFLIGHT_CLAUDE_FALLBACK", None)

    def tearDown(self):
        for k, v in getattr(self, "_saved", {}).items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_sdk_streaming_tool_and_fallback_model(self):
        from agentflight.providers.anthropic_api import AnthropicProvider

        server = FakeServer([(200, "text/event-stream", self._stream())])
        self._environment(server)
        try:
            streamed = []
            response = AnthropicProvider().call(request(model="claude-opus-5-5", effort="high"), streamed.append)
            self.assertEqual(response.text, "Hello")
            self.assertEqual("".join(streamed), "Hello")
            self.assertEqual(response.tool_calls[0].id, "toolu_1")
            self.assertEqual(response.tool_calls[0].arguments, {"a": 1, "b": 2})
            self.assertEqual((response.input_tokens, response.output_tokens, response.stop_reason),
                             (20, 15, "tool_use"))
            self.assertEqual(response.raw[1]["type"], "tool_use")
            sent = server.requests[0]
            body = sent["body"]
            self.assertTrue(sent["path"].startswith("/v1/messages"))
            self.assertIn("server-side-fallback-2026-07-01", sent["headers"].get("anthropic-beta", ""))
            self.assertEqual(body["fallbacks"], "default")
            self.assertTrue(body["stream"])
            self.assertNotIn("temperature", body, "Opus 5.5 does not accept a temperature")
            self.assertTrue(body["tools"][0]["eager_input_streaming"])
            self.assertEqual(body["output_config"],
                             {"format": {"type": "json_schema", "schema": STRICT}, "effort": "high"})
            self.assertEqual(body["system"], "System")
        finally:
            server.close()

    def test_retries_without_fallback_and_translates_tool_results(self):
        from agentflight.providers.anthropic_api import AnthropicProvider

        error = json.dumps({"type": "error", "error": {"type": "invalid_request_error",
                                                       "message": "fallbacks is not enabled for this organization"}})
        server = FakeServer([(400, "application/json", error), (200, "text/event-stream", self._stream())])
        self._environment(server)
        try:
            messages = [
                {"role": "user", "content": "add"},
                {"role": "assistant", "content": "", "tool_calls": [], "provider": "anthropic",
                 "raw": [{"type": "thinking", "thinking": "", "signature": "sig"},
                         {"type": "tool_use", "id": "toolu_0", "name": "add", "input": {"a": 1, "b": 2}}]},
                {"role": "tool", "results": [{"id": "toolu_0", "name": "add", "content": "3", "error": False}]},
                {"role": "user", "content": "[Message from the user] keep it short"},
            ]
            AnthropicProvider().call(request(model="claude-sonnet-5-5", messages=messages), lambda p: None)
            second = server.requests[1]
            self.assertNotIn("fallbacks", second["body"])
            sent = second["body"]["messages"]
            self.assertEqual(sent[1]["content"][0]["signature"], "sig",
                             "the thinking block must be sent back unchanged")
            self.assertEqual(sent[2]["role"], "user")
            self.assertEqual(sent[2]["content"][0]["type"], "tool_result")
            self.assertEqual(sent[2]["content"][1], {"type": "text", "text": "[Message from the user] keep it short"})
            self.assertEqual(len(sent), 3, "consecutive user messages must be merged")
        finally:
            server.close()


class McpTest(unittest.TestCase):
    def test_stdio_server(self):
        tools = mcp_server([sys.executable, str(TESTS / "calc_mcp.py")])
        self.assertEqual({t.name for t in tools}, {"calculate", "convert_unit"})
        calculate = next(t for t in tools if t.name == "calculate")
        self.assertEqual(calculate.source, "mcp")
        self.assertEqual(calculate.run({"expression": "2 * (3 + 4)"}), "14")
        self.assertEqual(calculate(expression="10 / 4"), "2.5")
        with self.assertRaises(AgentError):
            calculate.run({"expression": "__import__('os')"})


if __name__ == "__main__":
    unittest.main()
