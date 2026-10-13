"""Sign in with a ChatGPT plan ("Sign in with ChatGPT", open-source / local app flow).

Flow (developers.openai.com/siwc/token-sharing-open-source):
  1. A persistent, opaque `ext_agent_host_id` (urn:uuid:...) is generated for this computer.
  2. A loopback callback listener is opened on 127.0.0.1 and OpenAI's sign-in page is opened in the system
     browser (PKCE S256, state, nonce). On the first sign-in the app registers itself with
     client_id=dynamic_agent_client; the returned `client_id` (oaiapp_...) is stored and reused afterwards.
  3. The code is exchanged for tokens, the ID token (RS256) is verified with OpenAI's JWKS keys and the
     plan usage permission (`chatgpt.tokens.use.direct`) is checked.
  4. Tokens are stored in ~/.agentflight/accounts/chatgpt.json (encrypted with DPAPI on Windows) and refreshed
     before they expire. Logging out revokes the refresh token.

There is no client secret (public client). Tokens are never written to logs, URLs or the panel.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import html
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from .. import config
from ..config import _decrypt, _encrypt
from ..errors import AgentError
from ..localhttp import LocalHTTPServer


def _auth_root() -> str:
    return os.environ.get("AGENTFLIGHT_CHATGPT_AUTH", "https://auth.openai.com").rstrip("/")


def api_root() -> str:
    return os.environ.get("AGENTFLIGHT_CHATGPT_API", "https://api.openai.com/v1").rstrip("/")


APP_NAME = "AgentFlight"
REGISTRATION_CLIENT = "dynamic_agent_client"
SCOPE = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
RESOURCE = "https://api.openai.com/v1"
CALLBACK_PATH = "/auth/callback"
DEFAULT_PORT = 1455
USAGE_URL = "https://chatgpt.com/settings/usage"
LOGIN_TIMEOUT = 600  # seconds
REFRESH_MARGIN = 120  # the access token is refreshed this many seconds before it expires
UNUSABLE_REFRESH_ERRORS = {"invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
                           "refresh_token_invalidated", "refresh_token_reused"}


class ChatGPTError(AgentError):
    pass


# ---------------------------------------------------------------------------
# storage file

_LOCK = threading.RLock()


def _folder() -> Path:
    folder = config.settings_dir() / "accounts"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _file() -> Path:
    return _folder() / "chatgpt.json"


@contextlib.contextmanager
def _process_lock():
    """Serialises token refreshes across concurrently running AgentFlight processes (panel + programs)."""
    with _LOCK:
        f = open(_folder() / "chatgpt.lock", "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                while True:
                    try:
                        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                        break
                    except OSError:
                        continue
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX)
            yield
        finally:
            try:
                if os.name == "nt":
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f, fcntl.LOCK_UN)
            except OSError:
                pass
            f.close()


def _read() -> dict:
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    data.setdefault("records", {})
    data.setdefault("active", None)
    return data


def _write(data: dict) -> None:
    path = _file()
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def host_id() -> str:
    """This computer's persistent, opaque agent host id (urn:uuid:...)."""
    with _LOCK:
        data = _read()
        if not data.get("host_id"):
            data["host_id"] = f"urn:uuid:{uuid.uuid4()}"
            _write(data)
        return data["host_id"]


_SECRET_FIELDS = ("access_token", "refresh_token", "id_token")


def _decrypt_record(record: dict) -> dict:
    r = dict(record)
    for name in _SECRET_FIELDS:
        if r.get(name):
            r[name] = _decrypt(r[name])
    return r


def _encrypt_record(record: dict) -> dict:
    r = dict(record)
    for name in _SECRET_FIELDS:
        if r.get(name):
            r[name] = _encrypt(r[name])
    return r


# ---------------------------------------------------------------------------
# HTTP

def _request(url: str, *, form: dict | None = None, token: str | None = None, timeout: float = 30):
    headers = {"Accept": "application/json", "User-Agent": f"{APP_NAME}"}
    body = None
    if form is not None:
        body = urllib.parse.urlencode(form).encode("ascii")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            content = json.loads(raw)
        except json.JSONDecodeError:
            content = {"detail": raw.decode("utf-8", "replace")[:300]}
        return e.code, content


_DISCOVERY: dict = {}


def discovery() -> dict:
    """OpenID configuration (issuer, jwks_uri, revocation_endpoint...)."""
    root = _auth_root()
    if _DISCOVERY.get("root") != root:
        status_code, data = _request(f"{root}/.well-known/openid-configuration")
        if status_code != 200:
            raise ChatGPTError(f"could not load the OpenAI sign-in configuration (HTTP {status_code})")
        _DISCOVERY.clear()
        _DISCOVERY.update(data, root=root)
    return _DISCOVERY


# ---------------------------------------------------------------------------
# ID token verification (RS256, no dependencies)

def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


_SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def _verify_rs256(n: int, e: int, message: bytes, signature: bytes) -> bool:
    k = (n.bit_length() + 7) // 8
    if len(signature) != k:
        return False
    m = pow(int.from_bytes(signature, "big"), e, n).to_bytes(k, "big")
    t = _SHA256_PREFIX + hashlib.sha256(message).digest()
    expected = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return hmac.compare_digest(m, expected)


def verify_id_token(id_token: str, client_id: str, nonce: str | None) -> dict:
    try:
        head, body, signature = id_token.split(".")
        header = json.loads(_b64url_decode(head))
        claims = json.loads(_b64url_decode(body))
    except (ValueError, json.JSONDecodeError) as e:
        raise ChatGPTError("could not decode the ID token") from e
    if header.get("alg") != "RS256":
        raise ChatGPTError("unsupported ID token algorithm: " + str(header.get("alg")))
    configuration = discovery()
    status_code, jwks = _request(configuration["jwks_uri"])
    if status_code != 200:
        raise ChatGPTError("could not load OpenAI signing keys")
    keys = [k for k in jwks.get("keys", []) if k.get("kty") == "RSA"
            and (not header.get("kid") or k.get("kid") == header.get("kid"))]
    message = f"{head}.{body}".encode("ascii")
    if not any(_verify_rs256(int.from_bytes(_b64url_decode(k["n"]), "big"),
                             int.from_bytes(_b64url_decode(k["e"]), "big"),
                             message, _b64url_decode(signature)) for k in keys):
        raise ChatGPTError("the ID token signature could not be verified")
    if claims.get("iss") != configuration.get("issuer", _auth_root()):
        raise ChatGPTError("the ID token was not issued by the expected server")
    audience = claims.get("aud")
    if client_id not in (audience if isinstance(audience, list) else [audience]):
        raise ChatGPTError("the ID token was not issued for this app registration")
    if float(claims.get("exp", 0)) < time.time() - 60:
        raise ChatGPTError("the ID token has expired")
    if nonce is not None and claims.get("nonce") != nonce:
        raise ChatGPTError("the ID token nonce does not match")
    if not claims.get("sub"):
        raise ChatGPTError("the ID token has no account identity")
    return claims


# ---------------------------------------------------------------------------
# sign-in

class _CallbackServer(LocalHTTPServer):
    """Callback listener. The port is not shared: on Windows SO_REUSEADDR can hand the same port to two
    processes/sockets, so a stale listener that was never closed could swallow the redirect."""
    allow_reuse_address = False
    allow_reuse_port = False
    daemon_threads = True

    def server_bind(self):
        import socket
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def handle_error(self, request, client_address):
        pass


class _Login:
    """A single browser sign-in in progress."""

    def __init__(self, reauth_key: str | None, on_done=None):
        self.status = "pending"
        self.message = ""
        self.account: dict | None = None
        self.on_done = on_done
        self.state = _b64url_encode(secrets.token_bytes(24))
        self.nonce = _b64url_encode(secrets.token_bytes(24))
        self.verifier = _b64url_encode(secrets.token_bytes(48))
        self.reauth_key = reauth_key  # record key when signing in again
        self._closed = False
        self._close_lock = threading.Lock()
        previous = _read()["records"].get(reauth_key) if reauth_key else None
        self.previous = _decrypt_record(previous) if previous else None
        self.client_id = self.previous["client_id"] if self.previous else REGISTRATION_CLIENT
        self._server = self._open_listener()
        self.port = self._server.server_address[1]
        self.redirect_uri = f"http://127.0.0.1:{self.port}{CALLBACK_PATH}"
        self.started_at = time.time()
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        timer = threading.Timer(LOGIN_TIMEOUT, self._timeout)
        timer.daemon = True  # must not keep the process alive
        timer.start()

    def _open_listener(self) -> LocalHTTPServer:
        login = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                parts = urllib.parse.urlsplit(self.path)
                if parts.path != CALLBACK_PATH:
                    self.send_response(404)
                    self.end_headers()
                    return
                query = dict(urllib.parse.parse_qsl(parts.query))
                login._handle_callback(query)
                success = login.status == "done"
                title = "Signed in" if success else "Sign-in failed"
                text = ("Your ChatGPT account is connected to AgentFlight. "
                        "You can close this tab and go back to the app." if success else login.message)
                page = (f"<!doctype html><meta charset=utf-8><title>{title}</title>"
                        "<body style='font-family:Segoe UI,system-ui,sans-serif;background:#16181d;color:#e6e6e6;"
                        "display:grid;place-items:center;height:90vh'><div style='max-width:460px'>"
                        f"<h2>{html.escape(title)}</h2><p>{html.escape(text)}</p></div></body>").encode()
                self.send_response(200 if success else 400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(page)
                threading.Thread(target=login.close, daemon=True).start()

        for port in (DEFAULT_PORT, 0):
            try:
                return _CallbackServer(("127.0.0.1", port), Handler)
            except OSError:
                continue
        raise ChatGPTError("could not open a local port for sign-in")

    def url(self) -> str:
        params = {"client_id": self.client_id}
        if not self.previous:
            params["agent_name_hint"] = APP_NAME
        params["ext_agent_host_id"] = host_id()
        if self.previous:
            if self.previous.get("id_token"):
                params["id_token_hint"] = self.previous["id_token"]
            if self.previous.get("email"):
                params["login_hint"] = self.previous["email"]
        params.update({
            "response_type": "code", "redirect_uri": self.redirect_uri, "scope": SCOPE, "resource": RESOURCE,
            "state": self.state, "nonce": self.nonce, "code_challenge_method": "S256",
            "code_challenge": _b64url_encode(hashlib.sha256(self.verifier.encode("ascii")).digest()),
        })
        return f"{_auth_root()}/api/accounts/authorize?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)

    def _finish(self, status: str, message: str = "", account: dict | None = None):
        if self.status != "pending":
            return
        self.status, self.message, self.account = status, message, account
        if self.on_done:
            try:
                self.on_done(self)
            except Exception:  # noqa: BLE001
                pass

    def _timeout(self):
        self._finish("error", "Sign-in timed out; try again.")
        self.close()

    def close(self):
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:  # noqa: BLE001
            pass

    def _handle_callback(self, query: dict):
        if self.status != "pending":
            return
        if not secrets.compare_digest(str(query.get("state", "")), self.state):
            self._finish("error", "Sign-in could not be verified (state mismatch). Try again.")
            return
        if query.get("error"):
            if query["error"] == "access_denied":
                self._finish("rejected", "Sign-in cancelled; ChatGPT plan usage was not allowed.")
            else:
                self._finish("error", "OpenAI sign-in returned an error: "
                             + str(query.get("error_description") or query["error"]))
            return
        returned_client = query.get("client_id")
        if self.client_id == REGISTRATION_CLIENT:
            if not returned_client or returned_client == REGISTRATION_CLIENT:
                self._finish("error", "App registration did not complete (no client_id returned). Try again.")
                return
            client_id = returned_client
        else:
            if returned_client and returned_client != self.client_id:
                self._finish("error", "The returned app registration does not match this account; sign-in rejected.")
                return
            client_id = self.client_id
        if not query.get("code"):
            self._finish("error", "No authorization code was returned.")
            return
        try:
            account = self._exchange_code(client_id, query["code"])
        except ChatGPTError as e:
            self._finish("error", str(e))
            return
        except Exception as e:  # noqa: BLE001
            self._finish("error", "Sign-in failed: " + str(e))
            return
        self._finish("done", "", account)

    def _exchange_code(self, client_id: str, code: str) -> dict:
        status_code, response = _request(f"{_auth_root()}/api/accounts/oauth/token", form={
            "grant_type": "authorization_code", "client_id": client_id, "code": code,
            "code_verifier": self.verifier, "redirect_uri": self.redirect_uri, "resource": RESOURCE,
        })
        if status_code != 200:
            error = response.get("error") or response.get("detail") or f"HTTP {status_code}"
            raise ChatGPTError("Could not exchange the code for tokens: " + str(error))
        claims = verify_id_token(response["id_token"], client_id, self.nonce)
        if self.previous and self.previous.get("subject") and self.previous["subject"] != claims["sub"]:
            raise ChatGPTError("You signed in with a different ChatGPT account than the selected one; "
                               "nothing was changed.")
        scopes = sorted(str(response.get("scope", "")).split())
        now = time.time()
        record = {
            "email": claims.get("email", ""), "name": claims.get("name", ""), "issuer": claims.get("iss"),
            "subject": claims["sub"], "client_id": client_id, "ext_agent_host_id": host_id(),
            "id_token": response["id_token"], "access_token": response["access_token"],
            "refresh_token": response.get("refresh_token", ""), "token_type": response.get("token_type", "Bearer"),
            "expires_in": int(response.get("expires_in", 3600)),
            "expires_at": now + int(response.get("expires_in", 3600)),
            "scopes": scopes, "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        }
        key = f"{client_id}|{claims['sub']}"
        with _process_lock():
            data = _read()
            old = data["records"].get(key, {})
            record["models"] = old.get("models", [])
            record["welcomed"] = old.get("welcomed", False)
            data["records"][key] = _encrypt_record(record)
            data["active"] = key
            _write(data)
        try:
            refresh_models(key)
        except ChatGPTError:
            pass
        return account_summary(key)


_CURRENT: _Login | None = None


def start_login(reauth_key: str | None = None, browser: bool = True, on_done=None) -> dict:
    """Starts the ChatGPT sign-in in the browser. The returned URL can be opened by hand if no browser opens."""
    global _CURRENT
    with _LOCK:
        if _CURRENT is not None:
            _CURRENT._finish("error", "A new sign-in was started.")
            _CURRENT.close()
        if reauth_key is None:
            # Signing in to the same account again reuses its stored client_id (no new registration).
            data = _read()
            if data.get("active") in data["records"]:
                reauth_key = data["active"]
        _CURRENT = _Login(reauth_key, on_done)
        url = _CURRENT.url()
    if browser:
        webbrowser.open(url)
    return {"status": "pending", "url": url, "port": _CURRENT.port}


def add_account(browser: bool = True, on_done=None) -> dict:
    """New registration for another ChatGPT account or workspace (dynamic_agent_client)."""
    global _CURRENT
    with _LOCK:
        if _CURRENT is not None:
            _CURRENT._finish("error", "A new sign-in was started.")
            _CURRENT.close()
        _CURRENT = _Login(None, on_done)
        url = _CURRENT.url()
    if browser:
        webbrowser.open(url)
    return {"status": "pending", "url": url, "port": _CURRENT.port}


def login_status() -> dict | None:
    login = _CURRENT
    if login is None:
        return None
    return {"status": login.status, "message": login.message, "account": login.account}


def wait_for_login(timeout: float = LOGIN_TIMEOUT) -> dict | None:
    """For the CLI: waits until the sign-in in progress finishes."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = login_status()
        if s is None or s["status"] != "pending":
            return s
        time.sleep(0.3)
    return login_status()


# ---------------------------------------------------------------------------
# tokens

def _refresh_token(key: str) -> dict:
    """Called inside the locked section; returns the updated (decrypted) record."""
    data = _read()
    record = _decrypt_record(data["records"][key])
    if not record.get("refresh_token"):
        raise ChatGPTError("The ChatGPT session is closed; sign in again in Settings › Accounts.")
    status_code, response = _request(f"{_auth_root()}/api/accounts/oauth/token", form={
        "grant_type": "refresh_token", "client_id": record["client_id"], "refresh_token": record["refresh_token"],
        "resource": RESOURCE,
    })
    if status_code != 200:
        code = str(response.get("error") or "")
        if code in UNUSABLE_REFRESH_ERRORS or status_code in (400, 401):
            for name in ("access_token", "refresh_token"):
                record[name] = ""
            data["records"][key] = _encrypt_record(record)
            _write(data)
            raise ChatGPTError("The ChatGPT session expired or the app was disconnected; "
                               "sign in again in Settings › Accounts.")
        if code == "invalid_client":
            raise ChatGPTError("The ChatGPT app registration is invalid (invalid_client); "
                               "remove the account and add it again.")
        raise ChatGPTError(f"could not refresh the ChatGPT token (HTTP {status_code}); try again shortly.")
    now = time.time()
    record.update({
        "access_token": response["access_token"],
        "refresh_token": response.get("refresh_token", record["refresh_token"]),
        "expires_in": int(response.get("expires_in", 3600)), "expires_at": now + int(response.get("expires_in", 3600)),
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
    })
    if response.get("scope"):
        record["scopes"] = sorted(str(response["scope"]).split())
    if response.get("id_token"):
        record["id_token"] = response["id_token"]
    data["records"][key] = _encrypt_record(record)
    _write(data)
    return record


def access_token(key: str | None = None, force_refresh: bool = False) -> tuple[str, dict]:
    """(access token, decrypted record). Refreshes the token when needed."""
    with _process_lock():
        data = _read()
        key = key or data.get("active")
        if not key or key not in data["records"]:
            raise ChatGPTError("No ChatGPT account connected; Settings › Accounts › Continue with ChatGPT.")
        record = _decrypt_record(data["records"][key])
        if PLAN_SCOPE not in record.get("scopes", []):
            raise ChatGPTError("ChatGPT plan usage was not allowed; enable it in Settings › Accounts.")
        if (force_refresh or not record.get("access_token")
                or float(record.get("expires_at", 0)) - time.time() < REFRESH_MARGIN):
            record = _refresh_token(key)
        return record["access_token"], record


def refresh_models(key: str | None = None) -> list[dict]:
    token, record = access_token(key)
    status_code, response = _request(f"{api_root()}/models", token=token)
    if status_code != 200:
        raise ChatGPTError(f"could not load the ChatGPT model list (HTTP {status_code})")
    models = [{"slug": m["slug"], "name": m.get("display_name") or m["slug"]}
              for m in response.get("models", []) if m.get("visibility") == "list" and m.get("slug")]
    key = f"{record['client_id']}|{record['subject']}"
    with _process_lock():
        data = _read()
        if key in data["records"]:
            data["records"][key]["models"] = models
            _write(data)
    return models


def logout(key: str | None = None) -> dict:
    """Revokes the refresh token and deletes the tokens; the account/registration mapping is kept for the
    next sign-in."""
    with _process_lock():
        data = _read()
        key = key or data.get("active")
        if not key or key not in data["records"]:
            return {"ok": True, "revocation_confirmed": True}
        record = _decrypt_record(data["records"][key])
        confirmed = True
        if record.get("refresh_token"):
            confirmed = False
            try:
                endpoint = discovery().get("revocation_endpoint")
                if endpoint:
                    for _ in range(2):
                        status_code, _r = _request(endpoint, form={
                            "token": record["refresh_token"], "token_type_hint": "refresh_token",
                            "client_id": record["client_id"]}, timeout=10)
                        if status_code == 200:
                            confirmed = True
                            break
                        if status_code < 500:
                            break
                        time.sleep(1)
            except Exception:  # noqa: BLE001 - the local logout still happens
                pass
        for name in ("access_token", "refresh_token", "id_token"):
            record[name] = ""
        data["records"][key] = _encrypt_record(record)
        if data.get("active") == key:
            signed_in = [k for k, r in data["records"].items() if k != key and r.get("refresh_token")]
            data["active"] = signed_in[0] if signed_in else key
        _write(data)
    return {"ok": True, "revocation_confirmed": confirmed}


def select_account(key: str) -> None:
    with _process_lock():
        data = _read()
        if key not in data["records"]:
            raise ChatGPTError("account not found")
        data["active"] = key
        _write(data)


def mark_welcomed(key: str | None = None) -> None:
    with _process_lock():
        data = _read()
        key = key or data.get("active")
        if key in data["records"]:
            data["records"][key]["welcomed"] = True
            _write(data)


# ---------------------------------------------------------------------------
# status

def account_summary(key: str) -> dict:
    r = _read()["records"].get(key) or {}
    return {
        "key": key, "email": r.get("email", ""), "name": r.get("name", ""),
        "signed_in": bool(r.get("refresh_token") or r.get("access_token")),
        "plan_usage": PLAN_SCOPE in (r.get("scopes") or []),
        "models": r.get("models", []), "welcomed": bool(r.get("welcomed")),
    }


def is_connected() -> bool:
    data = _read()
    r = data["records"].get(data.get("active") or "")
    return bool(r and (r.get("refresh_token") or r.get("access_token")) and PLAN_SCOPE in (r.get("scopes") or []))


def active_models() -> list[dict]:
    data = _read()
    r = data["records"].get(data.get("active") or "")
    return list((r or {}).get("models") or [])


def status() -> dict:
    data = _read()
    accounts = [{**account_summary(k), "active": k == data.get("active")} for k in data["records"]]
    active = next((a for a in accounts if a["active"]), None)
    return {
        "connected": is_connected(), "active": active, "accounts": accounts, "usage_url": USAGE_URL,
        "login": login_status(),
    }
