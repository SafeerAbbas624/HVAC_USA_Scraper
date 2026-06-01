"""
Local HTTP-to-SOCKS5 bridge proxy.

Starts a local HTTP CONNECT proxy on localhost that tunnels all traffic
through an authenticated SOCKS5 proxy. Chrome connects to this local proxy
without auth, solving Chrome's lack of SOCKS5 auth support.
"""
import socket
import select
import threading
import logging

import socks

logger = logging.getLogger("scraper")

_BUFFER_SIZE = 65536
_SELECT_TIMEOUT = 1.0


class Socks5Bridge:
    """Local HTTP proxy that forwards through a SOCKS5 proxy with auth."""

    def __init__(self, socks_host, socks_port, socks_user=None, socks_pass=None):
        self.socks_host = socks_host
        self.socks_port = int(socks_port)
        self.socks_user = socks_user
        self.socks_pass = socks_pass
        self._server = None
        self._thread = None
        self.port = None
        self._stop_event = threading.Event()
        # Tracks how many connections failed due to SOCKS5 auth rejection
        self.auth_failure_count = 0
        self._count_lock = threading.Lock()

    def start(self):
        """Start the local bridge proxy. Returns the local port."""
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self.port = self._server.getsockname()[1]
        self._server.listen(128)
        self._server.settimeout(2.0)
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        logger.info(
            f"SOCKS5 bridge started on 127.0.0.1:{self.port} "
            f"-> {self.socks_host}:{self.socks_port}"
        )
        return self.port

    def stop(self):
        """Stop the bridge proxy."""
        self._stop_event.set()
        if self._server:
            try:
                self._server.close()
            except Exception:
                pass

    def _accept_loop(self):
        while not self._stop_event.is_set():
            try:
                client, _ = self._server.accept()
                t = threading.Thread(
                    target=self._handle_client, args=(client,), daemon=True
                )
                t.start()
            except socket.timeout:
                continue
            except OSError:
                break

    def _handle_client(self, client_sock):
        remote_sock = None
        try:
            client_sock.settimeout(30)
            data = client_sock.recv(_BUFFER_SIZE)
            if not data:
                return

            first_line = data.split(b"\r\n")[0].decode("utf-8", errors="replace")
            parts = first_line.split(" ", 2)
            if len(parts) < 2:
                return

            method = parts[0].upper()
            target = parts[1]

            if method == "CONNECT":
                # HTTPS tunnel: CONNECT host:port HTTP/1.1
                host, port = self._parse_host_port(target, 443)
                remote_sock = self._connect_via_socks(host, port)
                if remote_sock is None:
                    client_sock.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                    return
                client_sock.sendall(
                    b"HTTP/1.1 200 Connection Established\r\n\r\n"
                )
                self._relay(client_sock, remote_sock)
            else:
                # Plain HTTP: GET http://host/path HTTP/1.1
                if target.startswith("http://"):
                    url_rest = target[7:]
                    slash_idx = url_rest.find("/")
                    if slash_idx == -1:
                        host_part = url_rest
                        path = "/"
                    else:
                        host_part = url_rest[:slash_idx]
                        path = url_rest[slash_idx:]
                    host, port = self._parse_host_port(host_part, 80)

                    remote_sock = self._connect_via_socks(host, port)
                    if remote_sock is None:
                        client_sock.sendall(
                            b"HTTP/1.1 502 Bad Gateway\r\n\r\n"
                        )
                        return

                    # Rewrite request line to use relative path
                    rewritten = first_line.replace(target, path).encode()
                    rest = data.split(b"\r\n", 1)[1] if b"\r\n" in data else b""
                    remote_sock.sendall(rewritten + b"\r\n" + rest)
                    self._relay(client_sock, remote_sock)
                else:
                    client_sock.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
        except Exception:
            pass
        finally:
            try:
                client_sock.close()
            except Exception:
                pass
            if remote_sock:
                try:
                    remote_sock.close()
                except Exception:
                    pass

    def _parse_host_port(self, host_str, default_port):
        """Parse 'host:port' or 'host' into (host, port)."""
        if ":" in host_str:
            h, p = host_str.rsplit(":", 1)
            try:
                return h, int(p)
            except ValueError:
                return host_str, default_port
        return host_str, default_port

    def _connect_via_socks(self, host, port):
        """Create a SOCKS5 connection to the target host:port."""
        try:
            s = socks.socksocket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(30)
            s.set_proxy(
                socks.SOCKS5,
                self.socks_host,
                self.socks_port,
                username=self.socks_user,
                password=self.socks_pass,
            )
            s.connect((host, port))
            return s
        except Exception as e:
            err_str = str(e).lower()
            if "authentication" in err_str or "auth" in err_str:
                with self._count_lock:
                    self.auth_failure_count += 1
            logger.debug(f"SOCKS5 connect to {host}:{port} failed: {e}")
            return None

    def _relay(self, sock_a, sock_b):
        """Relay data bidirectionally between two sockets."""
        sockets = [sock_a, sock_b]
        try:
            while True:
                readable, _, errored = select.select(
                    sockets, [], sockets, _SELECT_TIMEOUT
                )
                if errored:
                    break
                for s in readable:
                    data = s.recv(_BUFFER_SIZE)
                    if not data:
                        return
                    other = sock_b if s is sock_a else sock_a
                    other.sendall(data)
        except Exception:
            pass


# Global bridge instance (one per process)
_bridge = None
_bridge_lock = threading.Lock()


def get_or_start_bridge(proxy_dict):
    """
    If the proxy is SOCKS5 with auth, start a local bridge and return
    the local proxy string for Chrome (e.g. '127.0.0.1:PORT').
    If the proxy is not SOCKS5, return None (use normal proxy handling).
    """
    global _bridge
    scheme = proxy_dict.get("scheme", "")
    if not scheme.startswith("socks"):
        return None

    with _bridge_lock:
        if _bridge is not None:
            # Bridge already running — reuse it
            return f"127.0.0.1:{_bridge.port}"

        _bridge = Socks5Bridge(
            socks_host=proxy_dict["ip"],
            socks_port=proxy_dict["port"],
            socks_user=proxy_dict.get("username"),
            socks_pass=proxy_dict.get("password"),
        )
        port = _bridge.start()
        return f"127.0.0.1:{port}"


def stop_bridge():
    """Stop the global bridge if running."""
    global _bridge
    with _bridge_lock:
        if _bridge:
            _bridge.stop()
            _bridge = None
