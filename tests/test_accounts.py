"""Accounts: ChatGPT sign-in (against a fake OpenAI server), the ChatGPT plan provider, the Claude Code bridge
(with a fake `claude` program) and model routing. No network, real account or Claude Code needed."""

import base64
import hashlib
import http.client
import json
import os
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

from agentflight import Agent, Schema, config, llm, providers, tool
from agentflight.accounts import chatgpt, claude_code
from agentflight.errors import ProviderError
from agentflight.localhttp import LocalHTTPServer
from agentflight.runtime import Run, set_current_run
from tests.helpers import TESTS, EventCollector, cleanup, temp_project

try:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
except ImportError:  # pragma: no cover
    rsa = None

FAKE_CLAUDE = str(TESTS / "fake_claude.py")


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def sse(events) -> str:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events)


class FakeOpenAI:
    """Local server that imitates OpenAI sign-in (OIDC) and the Responses API."""

    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.codes: dict[str, dict] = {}
        self.refresh_tokens = {}      # refresh_token -> client_id
        self.access_tokens = set()    # valid access tokens
        self.revocations = []
        self.responses = []           # (code, body) queue for /v1/responses
        self.requests = []
        self.scope = chatgpt.SCOPE
        self.counter = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, content_type="application/json"):
                raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _authorized(self):
                return self.headers.get("Authorization", "").removeprefix("Bearer ") in outer.access_tokens

            def do_GET(self):
                if self.path == "/.well-known/openid-configuration":
                    return self._send(200, {"issuer": outer.url, "jwks_uri": outer.url + "/jwks",
                                            "revocation_endpoint": outer.url + "/oauth/revoke"})
                if self.path == "/jwks":
                    public = outer.key.public_key().public_numbers()
                    return self._send(200, {"keys": [{"kty": "RSA", "kid": "k1", "alg": "RS256",
                                                      "n": b64(public.n.to_bytes(256, "big")),
                                                      "e": b64(public.e.to_bytes(3, "big"))}]})
                if self.path == "/v1/models":
                    if not self._authorized():
                        return self._send(401, {"error": {"message": "unauthorized"}})
                    return self._send(200, {"models": [
                        {"slug": "gpt-test", "display_name": "GPT Test", "visibility": "list"},
                        {"slug": "gpt-hidden", "display_name": "Hidden", "visibility": "hide"}]})
                self._send(404, {})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode()
                if self.path == "/api/accounts/oauth/token":
                    form = dict(urllib.parse.parse_qsl(raw))
                    outer.requests.append(("token", form))
                    return self._send(*outer.issue_tokens(form))
                if self.path == "/oauth/revoke":
                    outer.revocations.append(dict(urllib.parse.parse_qsl(raw)))
                    return self._send(200, "", "text/plain")
                if self.path == "/v1/responses":
                    outer.requests.append(("responses", json.loads(raw), self.headers.get("Authorization")))
                    code, body = outer.responses.pop(0)
                    if code == 401:
                        return self._send(401, {"error": {"message": "invalid token", "code": None}})
                    if code != 200:
                        return self._send(code, body)
                    return self._send(200, body, "text/event-stream")
                self._send(404, {})

        self.http = LocalHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.http.server_address[1]}"
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def id_token(self, client_id, nonce, sub="user-1", email="alice@example.com", broken=False):
        head = b64(json.dumps({"alg": "RS256", "kid": "k1", "typ": "JWT"}).encode())
        body = b64(json.dumps({"iss": self.url, "aud": client_id, "sub": sub, "email": email, "nonce": nonce,
                               "exp": int(time.time()) + 3600, "iat": int(time.time())}).encode())
        signature = self.key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
        if broken:
            signature = bytes([signature[0] ^ 1]) + signature[1:]
        return f"{head}.{body}.{b64(signature)}"

    def issue_tokens(self, form):
        self.counter += 1
        if form.get("resource") != chatgpt.RESOURCE:
            return 400, {"error": "invalid_target"}
        if form["grant_type"] == "authorization_code":
            code = self.codes.pop(form.get("code"), None)
            if code is None or code["client_id"] != form["client_id"] or code["redirect_uri"] != form["redirect_uri"]:
                return 400, {"error": "invalid_grant"}
            if b64(hashlib.sha256(form["code_verifier"].encode()).digest()) != code["challenge"]:
                return 400, {"error": "invalid_grant"}
            client_id, scope = form["client_id"], code["scope"]
            id_token = self.id_token(client_id, code["nonce"], sub=code.get("sub", "user-1"),
                                     broken=code.get("broken", False))
        elif form["grant_type"] == "refresh_token":
            if self.refresh_tokens.pop(form.get("refresh_token"), None) != form["client_id"]:
                return 400, {"error": "invalid_grant"}
            client_id, scope, id_token = form["client_id"], self.scope, None
        else:
            return 400, {"error": "unsupported_grant_type"}
        access, refresh = f"at-{self.counter}", f"rt-{self.counter}"
        self.access_tokens.add(access)
        self.refresh_tokens[refresh] = client_id
        response = {"access_token": access, "refresh_token": refresh, "token_type": "Bearer", "expires_in": 3600,
                    "scope": scope}
        if id_token:
            response["id_token"] = id_token
        return 200, response

    def browser(self, url, *, client_id="oaiapp_test1", scope=None, error=None, state=None, sub="user-1",
                broken=False):
        """Imitates the user approving in the browser: redirects to the callback address."""
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        code = f"code-{len(self.codes)}-{time.time_ns()}"
        self.codes[code] = {"client_id": client_id, "redirect_uri": q["redirect_uri"], "challenge": q["code_challenge"],
                            "nonce": q["nonce"], "scope": scope or self.scope, "sub": sub, "broken": broken}
        callback = {"state": state or q["state"]}
        if error:
            callback["error"] = error
        else:
            callback.update(code=code, scope=scope or self.scope)
            if q["client_id"] == chatgpt.REGISTRATION_CLIENT:
                callback["client_id"] = client_id
        try:
            urllib.request.urlopen(q["redirect_uri"] + "?" + urllib.parse.urlencode(callback), timeout=20).read()
        except urllib.error.HTTPError:
            pass
        return q

    def close(self):
        self.http.shutdown()
        self.http.server_close()


def reset_chatgpt():
    try:
        (chatgpt._folder() / "chatgpt.json").unlink()
    except FileNotFoundError:
        pass


def use_fake_openai(server: FakeOpenAI) -> None:
    os.environ["AGENTFLIGHT_CHATGPT_AUTH"] = server.url
    os.environ["AGENTFLIGHT_CHATGPT_API"] = server.url + "/v1"
    chatgpt._DISCOVERY.clear()
    reset_chatgpt()


def stop_fake_openai(server: FakeOpenAI) -> None:
    server.close()
    os.environ.pop("AGENTFLIGHT_CHATGPT_AUTH", None)
    os.environ.pop("AGENTFLIGHT_CHATGPT_API", None)
    chatgpt._DISCOVERY.clear()
    reset_chatgpt()


@unittest.skipIf(rsa is None, "cryptography is not installed")
class ChatGPTLoginTest(unittest.TestCase):
    def setUp(self):
        self.server = FakeOpenAI()
        use_fake_openai(self.server)

    def tearDown(self):
        stop_fake_openai(self.server)

    def login(self, **kw):
        info = chatgpt.start_login(browser=False)
        q = self.server.browser(info["url"], **kw)
        return q, chatgpt.wait_for_login(10)

    def test_first_login_registration_tokens_and_models(self):
        q, result = self.login()
        self.assertEqual(result["status"], "done", result)
        # dynamic registration on the first sign-in
        self.assertEqual(q["client_id"], "dynamic_agent_client")
        self.assertEqual(q["agent_name_hint"], "AgentFlight")
        self.assertTrue(q["ext_agent_host_id"].startswith("urn:uuid:"))
        self.assertEqual((q["response_type"], q["code_challenge_method"], q["resource"]),
                         ("code", "S256", "https://api.openai.com/v1"))
        self.assertIn("chatgpt.tokens.use.direct", q["scope"].split())
        self.assertTrue(q["redirect_uri"].startswith("http://127.0.0.1:"))
        self.assertTrue(q["redirect_uri"].endswith("/auth/callback"))
        self.assertTrue(chatgpt.is_connected())
        self.assertEqual([m["slug"] for m in chatgpt.active_models()], ["gpt-test"])  # hidden models are not listed
        # tokens are not stored as plain text in the file (DPAPI on Windows)
        raw = (chatgpt._folder() / "chatgpt.json").read_text(encoding="utf-8")
        if os.name == "nt":
            self.assertNotIn("at-1", raw)
            self.assertNotIn("rt-1", raw)
        status = chatgpt.status()
        self.assertEqual(status["active"]["email"], "alice@example.com")
        self.assertTrue(status["active"]["key"].startswith("oaiapp_test1|"))

        # signing in to the same account again: stored client_id + hints, no agent_name_hint
        q2, result2 = self.login()
        self.assertEqual(result2["status"], "done", result2)
        self.assertEqual(q2["client_id"], "oaiapp_test1")
        self.assertNotIn("agent_name_hint", q2)
        self.assertIn("id_token_hint", q2)
        self.assertEqual(q2["login_hint"], "alice@example.com")
        self.assertEqual(q2["ext_agent_host_id"], q["ext_agent_host_id"])

    def test_token_refresh_and_logout(self):
        self.login()
        data = chatgpt._read()
        key = data["active"]
        data["records"][key]["expires_at"] = time.time() - 10
        chatgpt._write(data)
        token, _ = chatgpt.access_token()
        self.assertNotEqual(token, "at-1")
        refreshes = [r[1] for r in self.server.requests if r[0] == "token" and r[1]["grant_type"] == "refresh_token"]
        self.assertEqual(len(refreshes), 1)
        self.assertEqual(refreshes[0]["client_id"], "oaiapp_test1")
        result = chatgpt.logout()
        self.assertTrue(result["revocation_confirmed"])
        self.assertEqual(self.server.revocations[0]["token_type_hint"], "refresh_token")
        self.assertFalse(chatgpt.is_connected())
        self.assertIn(key, chatgpt._read()["records"])  # the registration mapping stays for the next sign-in

    def test_bad_logins(self):
        _, result = self.login(error="access_denied")
        self.assertEqual(result["status"], "rejected")
        _, result = self.login(state="other")
        self.assertEqual(result["status"], "error")
        self.assertIn("state", result["message"])
        _, result = self.login(broken=True)
        self.assertEqual(result["status"], "error")
        self.assertIn("signature", result["message"])
        self.assertFalse(chatgpt.is_connected())

    def test_without_plan_scope_not_connected(self):
        _, result = self.login(scope="openid profile email offline_access")
        self.assertEqual(result["status"], "done")
        self.assertFalse(result["account"]["plan_usage"])
        self.assertFalse(chatgpt.is_connected())
        with self.assertRaises(chatgpt.ChatGPTError):
            chatgpt.access_token()

    def test_relogin_with_another_account_is_rejected(self):
        self.login()
        _, result = self.login(sub="other-user")
        self.assertEqual(result["status"], "error")
        self.assertIn("different ChatGPT account", result["message"])


class Answer(Schema):
    value: int
    explanation: str


@tool
def add(a: int, b: int) -> int:
    """Adds two numbers."""
    return a + b


class Calculator(Agent):
    """You do calculations."""
    tools = [add]
    temperature = 0

    @llm
    def calculate(self, question: str) -> Answer: ...


class AgentTestCase(unittest.TestCase):
    def setUp(self):
        self.folder = temp_project({})
        self.session = Run(model=None, approval="auto", default_model="fake", project=self.folder)
        self.events = EventCollector()
        self.session.bus.add(self.events)
        set_current_run(self.session)
        providers._UNAVAILABLE.clear()

    def tearDown(self):
        set_current_run(None)
        cleanup(self.folder)


@unittest.skipIf(rsa is None, "cryptography is not installed")
class ChatGPTProviderTest(AgentTestCase):
    def setUp(self):
        super().setUp()
        self.server = FakeOpenAI()
        use_fake_openai(self.server)
        info = chatgpt.start_login(browser=False)
        self.server.browser(info["url"])
        self.assertEqual(chatgpt.wait_for_login(10)["status"], "done")

    def tearDown(self):
        stop_fake_openai(self.server)
        super().tearDown()

    def test_tool_loop_and_request_format(self):
        call = {"type": "function_call", "id": "fc_1", "call_id": "c1", "namespace": "agentflight", "name": "add",
                "arguments": '{"a": 2, "b": 3}', "status": "completed"}
        reasoning = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "encrypted"}
        message = {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                   "content": [{"type": "output_text", "text": '{"value": 5, "explanation": "added"}'}]}
        self.server.responses = [
            (401, None),  # a token treated as expired: refreshed once and retried
            (200, sse([{"type": "response.output_item.done", "item": reasoning},
                       {"type": "response.output_item.done", "item": call},
                       {"type": "response.completed", "response": {
                           "output": [reasoning, call], "usage": {"input_tokens": 50, "output_tokens": 10}}}])),
            (200, sse([{"type": "response.output_text.delta", "delta": '{"value": 5, '},
                       {"type": "response.output_text.delta", "delta": '"explanation": "added"}'},
                       {"type": "response.completed", "response": {
                           "output": [message], "usage": {"input_tokens": 80, "output_tokens": 12}}}])),
        ]
        result = Calculator(model="chatgpt/gpt-test").calculate("add 2 and 3")
        self.assertEqual((result.value, result.explanation), (5, "added"))
        requests = [r for r in self.server.requests if r[0] == "responses"]
        self.assertEqual(len(requests), 3)
        body = requests[1][1]
        self.assertEqual((body["model"], body["store"], body["stream"]), ("gpt-test", False, True))
        self.assertIn("instructions", body)
        self.assertNotIn("temperature", body)
        self.assertEqual(body["include"], ["reasoning.encrypted_content"])
        self.assertEqual(body["tools"][0]["type"], "namespace")
        self.assertEqual(body["tools"][0]["tools"][0]["name"], "add")
        self.assertEqual(body["text"]["format"]["type"], "json_schema")
        self.assertTrue(all(item.get("role") != "system" for item in body["input"]))
        self.assertNotEqual(requests[0][2], requests[1][2])  # a new token after the 401
        second_input = requests[2][1]["input"]
        self.assertEqual([item["type"] for item in second_input],
                         ["message", "reasoning", "function_call", "function_call_output"])
        self.assertEqual(second_input[1]["encrypted_content"], "encrypted")
        self.assertNotIn("id", second_input[2])
        self.assertEqual(second_input[3], {"type": "function_call_output", "call_id": "c1", "output": "5"})
        finished = self.events.of_type("call_finished")[-1]
        self.assertTrue(finished["subscription"])
        self.assertEqual(finished["cost"], 0.0)  # plan usage is not counted as an API fee
        self.assertEqual(self.events.of_type("tool_finished")[0]["result"], "5")

    def test_usage_limit_is_not_retried(self):
        self.server.responses = [(200, sse([{"type": "response.failed", "response": {"error": {
            "code": "subscription_sharing_usage_limit_exceeded", "message": "limit"}}}]))]
        with self.assertRaises(ProviderError) as ctx:
            Calculator(model="chatgpt/gpt-test", tools=[]).calculate("x")
        self.assertIn("chatgpt.com/settings/usage", str(ctx.exception))
        self.assertEqual(len([r for r in self.server.requests if r[0] == "responses"]), 1)

    def test_routing(self):
        os.environ.pop("OPENAI_API_KEY", None)
        provider, model = providers.find_provider("gpt-test")
        self.assertEqual((provider.name, model), ("chatgpt", "gpt-test"))
        self.assertIn("chatgpt/gpt-test", providers.auto_candidates())
        os.environ["OPENAI_API_KEY"] = "sk-test"
        try:
            self.assertEqual(providers.find_provider("gpt-test")[0].name, "chatgpt")  # default: account first
            config.set_preference("account_priority", {"openai": "key"})
            self.assertEqual(providers.find_provider("gpt-test")[0].name, "openai")
            self.assertEqual(providers.find_provider("chatgpt/gpt-test")[0].name, "chatgpt")
        finally:
            os.environ.pop("OPENAI_API_KEY", None)
            config.set_preference("account_priority", {"openai": "account"})


class ClaudeCodeBridgeTest(AgentTestCase):
    def setUp(self):
        super().setUp()
        self.log = self.folder / "claude_call.json"
        self._claude_env = os.environ.get("AGENTFLIGHT_CLAUDE")
        os.environ["AGENTFLIGHT_CLAUDE"] = FAKE_CLAUDE
        os.environ["FAKE_CLAUDE_LOG"] = str(self.log)
        os.environ["CLAUDECODE"] = "1"                    # the nested-session marker must not reach the subprocess
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"   # must not reach the subprocess on the subscription route
        claude_code._CACHE.clear()

    def tearDown(self):
        for name in ("FAKE_CLAUDE_LOG", "FAKE_CLAUDE_SCENARIO", "FAKE_CLAUDE_SIGNED_IN", "CLAUDECODE",
                     "ANTHROPIC_API_KEY"):
            os.environ.pop(name, None)
        if self._claude_env is None:
            os.environ.pop("AGENTFLIGHT_CLAUDE", None)
        else:
            os.environ["AGENTFLIGHT_CLAUDE"] = self._claude_env
        claude_code._CACHE.clear()
        super().tearDown()

    def test_status(self):
        s = claude_code.status(refresh=True)
        self.assertEqual((s["installed"], s["signed_in"], s["email"], s["subscription"], s["version"]),
                         (True, True, "test@example.com", "max", "9.9.9"))
        os.environ["FAKE_CLAUDE_SIGNED_IN"] = "0"
        self.assertFalse(claude_code.status(refresh=True)["signed_in"])

    def test_tool_runs_in_agentflight_through_the_mcp_bridge(self):
        result = Calculator(model="claude-code/claude-haiku-5-5").calculate("add 2 and 3")
        self.assertEqual((result.value, result.explanation), (5, "turn 1"))
        call = json.loads(self.log.read_text(encoding="utf-8"))
        argv = call["argv"]
        for expected in (["--model", "claude-haiku-5-5"], ["--tools", ""], ["--setting-sources", "project"],
                         ["--output-format", "stream-json"], ["--input-format", "stream-json"]):
            i = argv.index(expected[0])
            self.assertEqual(argv[i:i + 2], expected)
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn("--no-session-persistence", argv)
        self.assertIn("mcp__agentflight", argv[argv.index("--allowedTools") + 1])
        schema = json.loads(argv[argv.index("--json-schema") + 1])
        self.assertEqual(set(schema["required"]), {"value", "explanation"})
        self.assertIsNone(call["env"]["CLAUDECODE"])
        self.assertIsNone(call["env"]["ANTHROPIC_API_KEY"])
        self.assertIn("You do calculations.", call["system"])
        # the tool ran in AgentFlight and showed up in the live view
        self.assertEqual(self.events.of_type("tool_started")[0]["name"], "add")
        self.assertEqual(self.events.of_type("tool_finished")[0]["result"], "5")
        self.assertEqual(self.events.of_type("tool_started")[0]["id"], self.events.of_type("call_started")[0]["id"])
        self.assertGreaterEqual(len(self.events.of_type("turn_finished")), 2)
        self.assertIn("Calculating", "".join(e["text"] for e in self.events.of_type("token")))
        finished = self.events.of_type("call_finished")[-1]
        self.assertTrue(finished["subscription"])
        self.assertEqual(finished["cost"], 0.0)
        roles = [m["role"] for m in finished["messages"]]
        self.assertEqual(roles[:3], ["user", "assistant", "tool"])

    def test_format_error_is_fixed_in_the_same_session(self):
        os.environ["FAKE_CLAUDE_SCENARIO"] = "format"
        result = Calculator(model="claude-code/claude-haiku-5-5").calculate("x")
        self.assertEqual(result.explanation, "turn 2")
        self.assertEqual(len(self.events.of_type("format_error")), 1)

    def test_clear_error_when_not_signed_in(self):
        os.environ["FAKE_CLAUDE_SCENARIO"] = "not_signed_in"
        with self.assertRaises(ProviderError) as ctx:
            Calculator(model="claude-code/claude-haiku-5-5").calculate("x")
        self.assertIn("Settings › Accounts › Claude › Connect", str(ctx.exception))
        self.assertEqual(ctx.exception.status, 401)

    def test_routing(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        self.assertEqual(providers.find_provider("claude-opus-5-5")[0].name, "claude_code")
        self.assertEqual(providers.auto_candidates()[0], "claude-code/claude-opus-5-5")
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
        try:
            self.assertEqual(providers.find_provider("claude-opus-5-5")[0].name, "claude_code")
            config.set_preference("account_priority", {"anthropic": "key"})
            self.assertEqual(providers.find_provider("claude-opus-5-5")[0].name, "anthropic")
            self.assertEqual(providers.find_provider("claude-code/claude-opus-5-5")[0].name, "claude_code")
        finally:
            config.set_preference("account_priority", {"anthropic": "account"})
        os.environ["AGENTFLIGHT_CLAUDE"] = self._claude_env or "missing"
        claude_code._CACHE.clear()
        self.assertEqual(providers.find_provider("claude-opus-5-5")[0].name, "anthropic")


class McpBridgeTest(unittest.TestCase):
    def test_token_host_and_name_mapping(self):
        from agentflight.mcp_bridge import ToolServer, mcp_name
        from agentflight.tools import to_tools

        def über_city(city: str) -> str:
            """Returns the city name."""
            return city.upper()

        calls = []
        server = ToolServer(to_tools([über_city]), lambda name, args: (calls.append((name, args)) or "OK", False))
        try:
            name = mcp_name("über_city", set())
            self.assertEqual(name, "uber_city")
            self.assertRegex(server.definitions[0]["name"], r"^[a-zA-Z0-9_-]{1,64}$")
            self.assertEqual(mcp_name("façade_ünit", set()), "facade_unit")
            self.assertEqual(mcp_name("weather report!", set()), "weather_report_")
            self.assertEqual(mcp_name("add", {"add"}), "add_2")

            def post(body, token=server.token, host=None):
                connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
                headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
                if host:
                    headers["Host"] = host
                connection.request("POST", "/mcp", body=json.dumps(body), headers=headers)
                response = connection.getresponse()
                raw = response.read()
                connection.close()
                return response.status, (json.loads(raw) if raw else None)

            self.assertEqual(post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token="wrong")[0], 401)
            self.assertEqual(post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, host="evil.example.com")[0], 401)
            code, r = post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            self.assertEqual(code, 200)
            self.assertEqual(r["result"]["tools"][0]["name"], name)
            code, r = post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                            "params": {"name": name, "arguments": {"city": "x"}}})
            self.assertEqual(r["result"]["content"][0]["text"], "OK")
            self.assertFalse(r["result"]["isError"])
            self.assertEqual(calls, [("über_city", {"city": "x"})])
            code, r = post({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "nope"}})
            self.assertTrue(r["result"]["isError"])
            self.assertEqual(len(calls), 1)
            self.assertEqual(post({"jsonrpc": "2.0", "method": "notifications/initialized"})[0], 202)
            self.assertEqual(post({"jsonrpc": "2.0", "id": 4, "method": "server/discover"})[1]["error"]["code"], -32601)
            self.assertEqual(server.tool_name(f"mcp__agentflight__{name}"), "über_city")
            self.assertIsNone(server.tool_name("mcp__other__uber_city"))
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()
