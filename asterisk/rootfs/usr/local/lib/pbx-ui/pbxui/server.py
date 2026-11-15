"""Web UI backend for the Asterisk add-on.

Serves the UI and a small JSON API behind Home Assistant ingress. Any request
that isn't for the UI or API (for example the SIP-over-WebSocket endpoint
``/ws`` used by the Home Assistant SIP card) is passed straight through to
Asterisk's own HTTP server so existing setups keep working.
"""

import http.client
import http.server
import json
import logging
import os
import re
import select
import socket
import subprocess
import threading
import time
import urllib.request

from . import model
from .ami import Ami, AmiError

log = logging.getLogger("pbx-ui")

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
MAX_BODY = 64 * 1024
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}
# Home Assistant's Supervisor is the only client allowed when running as an
# add-on: the UI is meant to be reached through ingress, which handles login.
INGRESS_CLIENTS = {"172.30.32.2", "127.0.0.1", "::1"}
PERSON_RE = re.compile(r'^callerid="(.*)" <(\d+)>\s*$', re.MULTILINE)


class Settings:
    def __init__(self):
        env = os.environ.get
        self.port = int(env("PBX_UI_PORT", "8087"))
        self.config_path = env("PBX_UI_CONFIG", "/config/asterisk/ui/pbx.json")
        self.asterisk_dir = env("PBX_UI_ASTERISK_DIR", "/etc/asterisk")
        self.asterisk_http = ("127.0.0.1", int(env("PBX_UI_ASTERISK_HTTP_PORT", "8088")))
        self.ami_port = int(env("PBX_UI_AMI_PORT", "5038"))
        self.ami_user = env("PBX_UI_AMI_USER", "pbx-ui")
        self.ami_secret_file = env("PBX_UI_AMI_SECRET_FILE", "/run/pbx-ui/ami-secret")
        self.supervisor_token = env("SUPERVISOR_TOKEN", "")
        self.restrict_clients = bool(self.supervisor_token) and not env("PBX_UI_ALLOW_ANY")

    def ami_secret(self):
        try:
            with open(self.ami_secret_file) as fh:
                return fh.read().strip()
        except OSError:
            return ""


class Backend:
    def __init__(self, settings):
        self.settings = settings
        self.store = model.Store(settings.config_path, settings.asterisk_dir)
        self._server_address = None
        self._status_cache = (0.0, None)

    # -- Asterisk access ---------------------------------------------------

    def ami(self):
        s = self.settings
        return Ami("127.0.0.1", s.ami_port, s.ami_user, s.ami_secret())

    def cli(self, command):
        """Run an Asterisk CLI command via the control socket."""
        try:
            result = subprocess.run(
                ["asterisk", "-rx", command],
                capture_output=True, text=True, timeout=20, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as err:
            return False, str(err)
        output = (result.stdout + result.stderr).strip()
        output = re.sub(r"\n?Asterisk ending \(\d+\)\.$", "", output).strip()
        return result.returncode == 0, output

    def apply(self):
        """Reload Asterisk so saved changes take effect."""
        problems = []
        for command in ("pjsip reload", "dialplan reload"):
            ok, output = self.cli(command)
            if not ok:
                problems.append(output or f"'{command}' failed")
        self._status_cache = (0.0, None)
        return problems

    def status(self):
        cached_at, cached = self._status_cache
        if cached is not None and time.monotonic() - cached_at < 2:
            return cached
        status = {"available": False, "endpoints": {}, "lines": {}, "calls": 0}
        try:
            with self.ami() as ami:
                resp, _ = ami.action("CoreSettings")
                status["version"] = resp.get("AsteriskVersion")
                _, events = ami.action("PJSIPShowEndpoints")
                for ev in events:
                    if ev.get("Event") == "EndpointList":
                        status["endpoints"][ev.get("ObjectName")] = ev.get("DeviceState")
                _, events = ami.action("PJSIPShowRegistrationsOutbound")
                for ev in events:
                    if ev.get("Event") == "OutboundRegistrationDetail":
                        status["lines"][ev.get("ObjectName")] = ev.get("Status")
                _, events = ami.action("CoreShowChannels")
                status["calls"] = len({
                    ev.get("Linkedid") for ev in events
                    if ev.get("Event") == "CoreShowChannel"
                })
                status["available"] = True
        except (AmiError, OSError) as err:
            status["error"] = str(err)
        self._status_cache = (time.monotonic(), status)
        return status

    # -- Things the UI doesn't manage ---------------------------------------

    def people(self):
        """Extensions auto-created for Home Assistant people (auto_add)."""
        path = os.path.join(self.settings.asterisk_dir, "pjsip_default.conf")
        try:
            with open(path) as fh:
                content = fh.read()
        except OSError:
            return {}
        return {number: name for name, number in PERSON_RE.findall(content)}

    def other_extensions(self, config, status):
        mine = {e["number"] for e in config["extensions"]}
        trunks = {t["id"] for t in config["trunks"]}
        people = self.people()
        others = [
            {"number": n, "name": name, "source": "person"}
            for n, name in people.items() if n not in mine
        ]
        for endpoint in status.get("endpoints", {}):
            if endpoint not in mine and endpoint not in trunks and endpoint not in people:
                others.append({"number": endpoint, "name": "", "source": "file"})
        return sorted(others, key=lambda o: (len(o["number"]), o["number"]))

    def taken_numbers(self, config, exclude=None):
        status = self.status()
        taken = {e["number"] for e in config["extensions"]}
        taken |= {o["number"] for o in self.other_extensions(config, status)}
        taken.discard(exclude)
        return taken

    def server_address(self):
        """The Home Assistant host's LAN address, for phone setup details."""
        if self._server_address is None and self.settings.supervisor_token:
            try:
                req = urllib.request.Request(
                    "http://supervisor/network/info",
                    headers={"Authorization": f"Bearer {self.settings.supervisor_token}"},
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.load(resp)["data"]
                for iface in data.get("interfaces", []):
                    addrs = (iface.get("ipv4") or {}).get("address") or []
                    if iface.get("primary") and addrs:
                        self._server_address = addrs[0].split("/")[0]
                        break
            except (OSError, ValueError, KeyError) as err:
                log.warning("Couldn't look up host address: %s", err)
                self._server_address = ""
        return self._server_address or ""

    # -- API -----------------------------------------------------------------

    def overview(self):
        try:
            config = self.store.load()
            config_error = None
        except (ValueError, model.ValidationError) as err:
            config = model.default_config()
            config_error = f"{self.settings.config_path} couldn't be read: {err}"
        status = self.status()
        others = self.other_extensions(config, status)
        taken = {e["number"] for e in config["extensions"]} | {o["number"] for o in others}
        return {
            "config": config,
            "config_error": config_error,
            "status": status,
            "others": others,
            "next_number": model.next_free_number(taken),
            "kinds": model.KINDS,
            "long_number_digits": model.LONG_NUMBER_MIN_DIGITS,
            "server": {"address": self.server_address(), "sip_port": 5060, "tls_port": 5061},
        }

    def _save(self, config):
        self.store.save(config)
        problems = self.apply()
        result = {"ok": True}
        if problems:
            result["warning"] = (
                "Saved, but Asterisk couldn't reload: " + "; ".join(problems)
            )
        return result

    def save_extension(self, number, data):
        with self.store.lock:
            config = self.store.load()
            existing = None
            if number is not None:
                existing = next((e for e in config["extensions"] if e["number"] == number), None)
                if existing is None:
                    raise NotFound(f"No phone with number {number}.")
            taken = self.taken_numbers(config, exclude=number)
            if existing is None and not str(data.get("number") or "").strip():
                data = dict(data, number=model.next_free_number(taken))
            ext = model.validate_extension(data, existing, taken, config["outbound"])
            if existing is None:
                config["extensions"].append(ext)
            else:
                config["extensions"][config["extensions"].index(existing)] = ext
                if ext["number"] != number:
                    for trunk in config["trunks"]:
                        trunk["ring"] = [ext["number"] if r == number else r for r in trunk["ring"]]
            config["extensions"].sort(key=lambda e: (len(e["number"]), e["number"]))
            result = self._save(config)
            result["extension"] = ext
            return result

    def delete_extension(self, number):
        with self.store.lock:
            config = self.store.load()
            before = len(config["extensions"])
            config["extensions"] = [e for e in config["extensions"] if e["number"] != number]
            if len(config["extensions"]) == before:
                raise NotFound(f"No phone with number {number}.")
            for trunk in config["trunks"]:
                trunk["ring"] = [r for r in trunk["ring"] if r != number]
            return self._save(config)

    def save_trunk(self, trunk_id, data):
        with self.store.lock:
            config = self.store.load()
            existing = None
            if trunk_id is not None:
                existing = next((t for t in config["trunks"] if t["id"] == trunk_id), None)
                if existing is None:
                    raise NotFound("That outside line doesn't exist.")
            known = {e["number"] for e in config["extensions"]}
            known |= {o["number"] for o in self.other_extensions(config, self.status())}
            trunk = model.validate_trunk(
                data, existing, known, {t["id"] for t in config["trunks"]}
            )
            if existing is None:
                config["trunks"].append(trunk)
                if not config["outbound"].get("trunk"):
                    config["outbound"]["trunk"] = trunk["id"]
            else:
                config["trunks"][config["trunks"].index(existing)] = trunk
            result = self._save(config)
            result["trunk"] = trunk
            return result

    def delete_trunk(self, trunk_id):
        with self.store.lock:
            config = self.store.load()
            before = len(config["trunks"])
            config["trunks"] = [t for t in config["trunks"] if t["id"] != trunk_id]
            if len(config["trunks"]) == before:
                raise NotFound("That outside line doesn't exist.")
            if config["outbound"].get("trunk") == trunk_id:
                remaining = config["trunks"]
                config["outbound"]["trunk"] = remaining[0]["id"] if remaining else None
            return self._save(config)

    def save_outbound(self, data):
        with self.store.lock:
            config = self.store.load()
            all_numbers = {e["number"] for e in config["extensions"]}
            all_numbers |= {o["number"] for o in self.other_extensions(config, self.status())}
            config["outbound"] = model.validate_outbound(
                data, {t["id"] for t in config["trunks"]}, all_numbers
            )
            return self._save(config)

    def ring(self, number):
        if not re.match(r"^[0-9]{1,6}$", number):
            raise model.ValidationError("Invalid number.")
        try:
            with self.ami() as ami:
                resp, _ = ami.action(
                    "Originate",
                    Channel=f"Local/{number}@local-extensions",
                    Application="Playback",
                    Data="hello-world",
                    CallerID='"Test call" <0>',
                    Timeout=30000,
                    Async="true",
                )
        except AmiError as err:
            raise model.ValidationError(str(err)) from err
        if resp.get("Response") != "Success":
            raise model.ValidationError(resp.get("Message", "Couldn't place the test call."))
        return {"ok": True}

    def command(self, data):
        command = str(data.get("command", "")).strip()
        if not command or "\n" in command or "\r" in command:
            raise model.ValidationError("Enter a command.", "command")
        ok, output = self.cli(command)
        return {"ok": ok, "output": output}


class NotFound(Exception):
    pass


ROUTES = [
    ("GET", r"/api/overview", lambda b, m, d: b.overview()),
    ("POST", r"/api/extensions", lambda b, m, d: b.save_extension(None, d)),
    ("PUT", r"/api/extensions/(\d+)", lambda b, m, d: b.save_extension(m[1], d)),
    ("DELETE", r"/api/extensions/(\d+)", lambda b, m, d: b.delete_extension(m[1])),
    ("POST", r"/api/extensions/(\d+)/ring", lambda b, m, d: b.ring(m[1])),
    ("POST", r"/api/trunks", lambda b, m, d: b.save_trunk(None, d)),
    ("PUT", r"/api/trunks/([a-z0-9-]+)", lambda b, m, d: b.save_trunk(m[1], d)),
    ("DELETE", r"/api/trunks/([a-z0-9-]+)", lambda b, m, d: b.delete_trunk(m[1])),
    ("PUT", r"/api/outbound", lambda b, m, d: b.save_outbound(d)),
    ("POST", r"/api/command", lambda b, m, d: b.command(d)),
    ("POST", r"/api/reload", lambda b, m, d: {"ok": True, "problems": b.apply()}),
]
STATIC_FILES = {"/": ("index.html", "text/html; charset=utf-8")}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "pbx-ui"
    backend = None  # set in serve()

    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.client_address[0], fmt % args)

    def _allowed(self):
        if not self.backend.settings.restrict_clients:
            return True
        return self.client_address[0] in INGRESS_CLIENTS

    def _dispatch(self):
        if not self._allowed():
            self.send_error(403, "Use the add-on through Home Assistant")
            return
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/"):
            self._api(path)
        elif self.command == "GET" and path in STATIC_FILES:
            self._static(*STATIC_FILES[path])
        else:
            self._proxy()

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_OPTIONS = _dispatch

    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _api(self, path):
        for method, pattern, handler in ROUTES:
            match = re.fullmatch(pattern, path)
            if match and method == self.command:
                break
        else:
            self._json(404, {"error": "Not found"})
            return
        try:
            data = {}
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise model.ValidationError("Request too large.")
            if length:
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise model.ValidationError("Expected a JSON object.")
            self._json(200, handler(self.backend, match, data))
        except model.ValidationError as err:
            self._json(400, {"error": err.message, "field": err.field})
        except NotFound as err:
            self._json(404, {"error": str(err)})
        except ValueError as err:
            self._json(400, {"error": f"Invalid request: {err}"})
        except Exception:  # noqa: BLE001 - report, don't kill the thread
            log.exception("API error on %s %s", self.command, path)
            self._json(500, {"error": "Something went wrong. Check the add-on log."})

    def _static(self, name, content_type):
        try:
            with open(os.path.join(STATIC_DIR, name), "rb") as fh:
                body = fh.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    # -- Pass-through to Asterisk's HTTP server -------------------------------

    def _proxy(self):
        if self.headers.get("Upgrade", "").lower() == "websocket":
            self._proxy_websocket()
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        conn = http.client.HTTPConnection(*self.backend.settings.asterisk_http, timeout=30)
        try:
            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()
            payload = resp.read()
        except OSError as err:
            self.send_error(502, f"Asterisk isn't responding: {err}")
            return
        finally:
            conn.close()
        self.send_response(resp.status, resp.reason)
        for key, value in resp.getheaders():
            if key.lower() not in HOP_BY_HOP and key.lower() != "content-length":
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _proxy_websocket(self):
        """Hand the raw connection to Asterisk once the upgrade request is sent."""
        try:
            upstream = socket.create_connection(self.backend.settings.asterisk_http, 10)
        except OSError as err:
            self.send_error(502, f"Asterisk isn't responding: {err}")
            return
        request = [f"{self.command} {self.path} HTTP/1.1"]
        request += [f"{k}: {v}" for k, v in self.headers.items()]
        upstream.sendall(("\r\n".join(request) + "\r\n\r\n").encode("latin-1"))
        client = self.connection
        client.settimeout(None)
        upstream.settimeout(None)
        sockets = [client, upstream]
        try:
            while True:
                readable, _, _ = select.select(sockets, [], [], 300)
                if not readable:
                    continue
                for sock in readable:
                    data = sock.recv(65536)
                    if not data:
                        return
                    (upstream if sock is client else client).sendall(data)
        except OSError:
            pass
        finally:
            upstream.close()
            self.close_connection = True


def generate(settings):
    """Write the Asterisk files from pbx.json (used at add-on start)."""
    store = model.Store(settings.config_path, settings.asterisk_dir)
    try:
        config = store.load()
    except (ValueError, model.ValidationError) as err:
        log.error("Ignoring %s, it couldn't be read: %s", settings.config_path, err)
        config = model.default_config()
    store.write_asterisk_files(config)


def serve(settings):
    Handler.backend = Backend(settings)
    server = http.server.ThreadingHTTPServer(("0.0.0.0", settings.port), Handler)
    server.daemon_threads = True
    log.info("Web UI listening on port %s", settings.port)
    threading.current_thread().name = "pbx-ui"
    server.serve_forever()
