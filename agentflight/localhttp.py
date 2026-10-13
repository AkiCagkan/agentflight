"""An HTTP server for 127.0.0.1 that starts instantly."""

from __future__ import annotations

import socketserver
from http.server import ThreadingHTTPServer


class LocalHTTPServer(ThreadingHTTPServer):
    """`HTTPServer.server_bind()` looks the host up with `socket.getfqdn()`, which can take seconds (reverse DNS,
    notably on macOS). The servers of AgentFlight only listen on 127.0.0.1, so the name is not needed."""

    daemon_threads = True

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port
