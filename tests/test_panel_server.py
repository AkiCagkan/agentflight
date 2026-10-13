"""Panel server: security checks, re-running a program of a connected project, live events and the approval flow."""

import http.client
import json
import os
import queue
import socket
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import urlencode

from agentflight import config
from agentflight.panel.server import create_server
from tests.helpers import cleanup, temp_project, wait_until

PROGRAM = '''
from agentflight import Agent, Schema, field, llm, require_approval


class Plan(Schema):
    steps: list[str] = field("steps", min=1, max=3)


class Planner(Agent):
    """You make short plans."""

    @llm
    def plan(self, goal: str) -> Plan: ...


plan = Planner().plan("The boiler has to be prepared for maintenance. First the fuel is cut off. Then it cools down.")
plan = require_approval(plan, "Is the plan OK?")
print("APPROVED", len(plan.steps))
'''


class PanelServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._old_delay = os.environ.get("AGENTFLIGHT_FAKE_DELAY")
        os.environ["AGENTFLIGHT_FAKE_DELAY"] = "0.01"
        cls.folder = temp_project({"plan.py": PROGRAM})
        cls.project = cls.folder.resolve()
        cls.http, cls.panel = create_server(port=17700)
        cls.port = cls.panel.port
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        # A program of the project connects by itself (like `python plan.py` with an open panel): from then on the
        # panel knows the project and may re-run its programs.
        cls.external = cls.connect_external(cls.project / "plan.py", cls.project)
        assert wait_until(lambda: str(cls.project) in cls.panel.known_projects), "the external program did not connect"

    @classmethod
    def tearDownClass(cls):
        cls.external.close()
        for process in list(cls.panel.processes.values()):
            process.stop()
        cls.http.shutdown()
        cls.http.server_close()
        if cls._old_delay is None:
            os.environ.pop("AGENTFLIGHT_FAKE_DELAY", None)
        else:
            os.environ["AGENTFLIGHT_FAKE_DELAY"] = cls._old_delay
        time.sleep(0.3)
        cleanup(cls.folder)

    @classmethod
    def connect_external(cls, file: Path, project: Path) -> socket.socket:
        """Performs the handshake of a program that found the panel through the discovery file."""
        connection = socket.create_connection(("127.0.0.1", cls.panel.runner_port), timeout=5)
        hello = {"hello": True, "token": f"{cls.panel.token}:", "run_id": "external-run",
                 "external": {"file": str(file), "kind": "program", "pid": 0, "python": None, "cwd": str(project),
                              "project": str(project), "model": "fake", "run_id": "external-run", "env_model": None,
                              "command": f"python {file.name}"}}
        connection.sendall((json.dumps(hello) + "\n").encode("utf-8"))
        return connection

    def request(self, method, path, data=None, token=True, host=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-AgentFlight-Token"] = self.panel.token if token is True else token
        if host:
            headers["Host"] = host
        connection.request(method, path, body=json.dumps(data) if data is not None else None, headers=headers)
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        connection.close()
        try:
            return response.status, json.loads(body)
        except json.JSONDecodeError:
            return response.status, body

    def test_page_contains_the_token(self):
        code, html = self.request("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn(self.panel.token, html)
        self.assertNotIn("{{TOKEN}}", html)

    def test_post_without_token_is_rejected(self):
        for token in (False, "wrong-token"):
            code, _ = self.request("POST", "/api/settings", {"keys": {"OPENAI_API_KEY": "sk-from-another-site"}},
                                   token=token)
            self.assertEqual(code, 403)
        self.assertNotIn("OPENAI_API_KEY", config.read().get("keys") or {})
        self.assertNotEqual(os.environ.get("OPENAI_API_KEY"), "sk-from-another-site")
        running = len(self.panel.processes)
        code, _ = self.request("POST", "/api/run", {"file": str(self.project / "plan.py"), "model": "fake"},
                               token=False)
        self.assertEqual(code, 403)
        self.assertEqual(len(self.panel.processes), running)

    def test_foreign_host_is_rejected(self):
        code, _ = self.request("GET", "/api/status", host="evil.example.com")
        self.assertEqual(code, 403)
        code, _ = self.request("POST", "/api/stop", {"sid": "x"}, host="evil.example.com")
        self.assertEqual(code, 403)

    def test_connected_project_is_known(self):
        code, status = self.request("GET", "/api/status")
        self.assertEqual(code, 200)
        self.assertIn(str(self.project), status["projects"])

    def test_programs_of_unknown_projects_cannot_be_run(self):
        other = temp_project({"plan.py": PROGRAM})
        try:
            running = len(self.panel.processes)
            code, r = self.request("POST", "/api/run", {"file": str(other / "plan.py"), "model": "fake"})
            self.assertEqual(code, 403, r)
            code, r = self.request("POST", "/api/eval", {"file": str(other / "plan.py")})
            self.assertEqual(code, 403, r)
            code, r = self.request("POST", "/api/run", {"file": str(self.project / ".." / other.name / "plan.py")})
            self.assertEqual(code, 403, r)
            self.assertEqual(len(self.panel.processes), running)
            code, _ = self.request("GET", "/api/runs?" + urlencode({"project": str(other)}))
            self.assertEqual(code, 403)
            code, _ = self.request("GET", "/api/run?" + urlencode({"project": str(other), "run_id": "x"}))
            self.assertEqual(code, 403)
        finally:
            cleanup(other)

    def test_static_path_traversal_is_refused(self):
        code, _ = self.request("GET", "/static/index.html")
        self.assertEqual(code, 200)
        for path in ("/static/../server.py", "/static/..\\server.py", "/static/../../__init__.py",
                     "/static/%2e%2e/server.py"):
            code, body = self.request("GET", path)
            self.assertEqual(code, 404, path)
            self.assertNotIn("PanelServer", str(body), path)

    def test_run_streams_live_events_and_answers_the_approval(self):
        events: queue.Queue = queue.Queue()
        stop = threading.Event()

        def listen():
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
            connection.request("GET", "/api/events")
            response = connection.getresponse()
            while not stop.is_set():
                line = response.fp.readline().decode("utf-8")
                if not line:
                    break
                if line.startswith("data: "):
                    events.put(json.loads(line[6:]))

        threading.Thread(target=listen, daemon=True).start()
        time.sleep(0.3)
        code, r = self.request("POST", "/api/run", {"file": str(self.project / "plan.py"), "model": "fake"})
        self.assertEqual(code, 200, r)
        sid = r["sid"]
        seen = []
        deadline = time.time() + 60
        approved = False
        while time.time() < deadline:
            try:
                event = events.get(timeout=1)
            except queue.Empty:
                continue
            if event.get("sid") != sid:
                continue
            seen.append(event)
            if event["type"] == "approval_requested" and not approved:
                approved = True
                code, r = self.request("POST", "/api/command", {"sid": sid, "command": "approve",
                                                                "approval_id": event["approval_id"],
                                                                "decision": "approve"})
                self.assertTrue(r["ok"], r)
            if event["type"] == "process_finished":
                break
        stop.set()
        types = [e["type"] for e in seen]
        for expected in ("process_started", "run_started", "call_started", "token", "call_finished",
                         "approval_requested", "approval_answered", "run_finished", "process_finished"):
            self.assertIn(expected, types)
        output = " ".join(e["text"] for e in seen if e["type"] == "output")
        self.assertIn("APPROVED", output)
        finished = next(e for e in seen if e["type"] == "process_finished")
        self.assertEqual(finished["exit_code"], 0, output)
        code, runs = self.request("GET", "/api/runs")
        self.assertEqual(code, 200)
        self.assertTrue(runs and runs[0]["status"] == "done", runs)
        self.assertEqual(runs[0]["project"], str(self.project))
        code, run = self.request("GET", "/api/run?" + urlencode({"project": runs[0]["project"],
                                                                "run_id": runs[0]["run_id"]}))
        self.assertEqual(code, 200, run)
        self.assertEqual(run["calls"][0]["agent"], "Planner")


if __name__ == "__main__":
    unittest.main()
