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

from . import model, provision
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

# A file in asterisk/custom replaces the add-on's own. These are the lines the
# web UI needs in each one, with a hint for where they go.
OVERRIDE_NEEDS = {
    "pjsip.conf": [("#include pjsip_ui.conf", r"#include\s+\"?pjsip_ui\.conf")],
    "extensions.conf": [
        ("include => ui-apps  (in [default])", r"include\s*=>\s*ui-apps"),
        ("include => ui-outbound  (in [default])", r"include\s*=>\s*ui-outbound"),
        ("include => local-extensions  (in [default], after the two above)",
         r"include\s*=>\s*local-extensions"),
        ("a [local-extensions] context that rings phones", r"^\s*\[local-extensions\]"),
        ("#include extensions_ui.conf", r"#include\s+\"?extensions_ui\.conf"),
    ],
    "ari.conf": [("#include ari_ui.conf", r"#include\s+\"?ari_ui\.conf")],
}


class Settings:
    def __init__(self):
        env = os.environ.get
        self.port = int(env("PBX_UI_PORT", "8087"))
        self.provision_port = int(env("PBX_UI_PROVISION_PORT", "8097"))
        self.config_path = env("PBX_UI_CONFIG", "/config/asterisk/ui/pbx.json")
        self.asterisk_dir = env("PBX_UI_ASTERISK_DIR", "/etc/asterisk")
        self.custom_dir = env("PBX_UI_CUSTOM_DIR", "/config/asterisk/custom")
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
        self.seen = provision.Seen()
        self.provision_error = None  # set if the phone setup port couldn't be opened

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
        for command in ("pjsip reload", "dialplan reload", "module reload res_ari.so"):
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
        status["apps"] = self.connected_apps() if status["available"] else []
        self._status_cache = (time.monotonic(), status)
        return status

    def connected_apps(self):
        """Stasis apps that are connected over ARI right now."""
        ok, output = self.cli("ari show apps")
        if not ok or "====" not in output:
            return []
        listing = output.split("====", 1)[1].lstrip("=").strip()
        return [line.strip() for line in listing.splitlines() if line.strip()]

    def overrides(self):
        """Custom files that replace the add-on's own, and what they're missing."""
        found = []
        for name, needs in OVERRIDE_NEEDS.items():
            path = os.path.join(self.settings.custom_dir, name)
            try:
                with open(path) as fh:
                    text = fh.read()
            except OSError:
                continue
            missing = [label for label, rx in needs if not re.search(rx, text, re.MULTILINE)]
            found.append({"file": name, "missing": missing})
        return found

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

    def server_address(self, config=None):
        """The address phones should use: the one set in the UI, else the detected one."""
        if config is None:
            try:
                config = self.store.load()
            except (ValueError, model.ValidationError):
                config = model.default_config()
        return config["network"]["sip_address"] or self.detected_address()

    def detected_address(self):
        """The Home Assistant host's LAN address (the add-on uses the host's network)."""
        if self._server_address is None:
            self._server_address = self._supervisor_address() or self._route_address()
        return self._server_address

    @staticmethod
    def _route_address():
        """The local address the host would use to reach the internet. Nothing is sent."""
        for target in ("192.0.2.1", "1.1.1.1"):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                    sock.connect((target, 9))
                    address = sock.getsockname()[0]
            except OSError:
                continue
            if address and not address.startswith("127."):
                return address
        return ""

    def _supervisor_address(self):
        if not self.settings.supervisor_token:
            return ""
        try:
            req = urllib.request.Request(
                "http://supervisor/network/info",
                headers={"Authorization": f"Bearer {self.settings.supervisor_token}"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.load(resp)["data"]
        except (OSError, ValueError, KeyError) as err:
            log.debug("Supervisor didn't give the host address: %s", err)
            return ""
        for iface in data.get("interfaces", []):
            addrs = (iface.get("ipv4") or {}).get("address") or []
            if iface.get("primary") and addrs:
                return addrs[0].split("/")[0]
        return ""

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
            "server": {
                "address": self.server_address(config),
                "detected": self.detected_address(),
                "custom": config["network"]["sip_address"],
                "sip_port": 5060,
                "tls_port": 5061,
            },
            "presets": model.PRESETS,
            "ari_url": model.ARI_URL,
            "overrides": self.overrides(),
            "integrations_dir": self.store.integrations_dir,
            "provisioning": self.provisioning_overview(config),
        }

    def provisioning_overview(self, config):
        prov = config["provisioning"]
        known = {e["provision"]["mac"] for e in config["extensions"] if e["provision"]}
        waiting, fetched = self.seen.snapshot(known)
        return {
            "enabled": prov["enabled"],
            "port": self.settings.provision_port,
            "path": f"/p/{prov['token']}/" if prov["token"] else "",
            "public_url": prov["public_url"],
            "error": self.provision_error,
            "waiting": waiting,
            "fetched": fetched,
            "vendors": provision.VENDORS,
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
            macs = {
                e["provision"]["mac"] for e in config["extensions"]
                if e["provision"] and e is not existing
            }
            ext = model.validate_extension(
                data, existing, taken, config["outbound"], config["apps"], macs
            )
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
                data, {t["id"] for t in config["trunks"]}, all_numbers, config["apps"]
            )
            return self._save(config)

    def save_network(self, data):
        with self.store.lock:
            config = self.store.load()
            config["network"] = model.validate_network(data)
            self.store.save(config)
            return {"ok": True}

    def save_provisioning(self, data):
        with self.store.lock:
            config = self.store.load()
            prov = config["provisioning"]
            enabled = data.get("enabled", prov["enabled"])
            if not isinstance(enabled, bool):
                raise model.ValidationError("Invalid value.", "enabled")
            prov["enabled"] = enabled
            if "public_url" in data:
                prov["public_url"] = model.clean_public_url(data["public_url"])
            if data.get("reset") or not prov["token"]:
                prov["token"] = model.generate_password(24)
            self.store.save(config)  # no Asterisk reload needed
            return {"ok": True}

    def dismiss_waiting(self, mac):
        if not self.seen.dismiss(provision.normalize_mac(mac)):
            raise NotFound("That phone isn't waiting.")
        return {"ok": True}

    def resync(self, number):
        config = self.store.load()
        ext = next((e for e in config["extensions"] if e["number"] == number), None)
        if ext is None or not ext["provision"]:
            raise NotFound("That phone isn't set up automatically.")
        state = self.status()["endpoints"].get(number)
        if state in (None, "Unavailable", "Invalid", "Unknown"):
            raise model.ValidationError(
                f"{ext['name']} isn't online, so it can't be asked. Restart the phone "
                "instead: it downloads its settings when it starts."
            )
        notify = provision.CHECK_SYNC[ext["provision"]["vendor"]]
        ok, output = self.cli(f"pjsip send notify {notify} endpoint {number}")
        if not ok or "unable" in output.lower() or "not found" in output.lower():
            raise model.ValidationError(
                "Couldn't reach the phone. It must be online (signed in) for this; "
                "otherwise restart it to pick up its settings."
            )
        return {"ok": True}

    def provision_file(self, token, filename, user_agent, client, host_header):
        """(status, content_type, body) for a phone asking for its settings."""
        try:
            config = self.store.load()
        except (ValueError, model.ValidationError):
            return 503, "text/plain", "Settings unavailable\n"
        prov = config["provisioning"]
        if not (prov["token"] and token == prov["token"]):
            return 404, "text/plain", "Not found\n"
        if not prov["enabled"]:
            return 404, "text/plain", (
                "Automatic setup is turned off. Turn it on in the Asterisk add-on's web UI "
                "(Phones tab).\n"
            )
        if not filename:  # someone checking the address in a browser
            return 200, "text/plain; charset=utf-8", (
                "Asterisk add-on: automatic phone setup is working.\n\n"
                "This is the address to enter in a desk phone's provisioning or auto-provision "
                "setting. Phones add their own file name to it (for example "
                "805ec0123456.cfg), so there's nothing else to see here in a browser.\n"
            )
        parsed = provision.parse_request(filename, user_agent)
        if parsed is None:
            return 404, "text/plain", "Not found\n"
        mac, vendor, kind = parsed
        ext = next(
            (e for e in config["extensions"] if e["provision"] and e["provision"]["mac"] == mac),
            None,
        )
        if ext is None:
            log.info("Phone %s (%s) asked for settings; waiting to be set up",
                     provision.format_mac(mac), vendor)
            self.seen.waiting(mac, vendor, user_agent, client)
            return 404, "text/plain", "Not set up yet\n"
        # The address set in the UI, else the detected LAN address. The name the
        # phone used to fetch this file is a last resort: it may be a proxy's.
        host = self.server_address(config)
        if not host:
            host = re.sub(r":\d+$", "", (host_header or "").strip()).strip("[]")
        if not model.HOST_RE.match(host or ""):
            host = "127.0.0.1"
        if kind != "polycom-master":
            self.seen.fetched(mac)
            log.info("Sent settings to %s (%s, number %s)",
                     provision.format_mac(mac), vendor, ext["number"])
        content_type, body = provision.render(kind, ext, host, mac=mac)
        return 200, content_type, body

    def save_ari(self, data):
        with self.store.lock:
            config = self.store.load()
            enabled = data.get("enabled")
            if not isinstance(enabled, bool):
                raise model.ValidationError("Invalid value.", "enabled")
            config["ari"]["enabled"] = enabled
            return self._save(config)

    def save_app(self, app_id, data):
        with self.store.lock:
            config = self.store.load()
            existing = None
            if app_id is not None:
                existing = next((a for a in config["apps"] if a["id"] == app_id), None)
                if existing is None:
                    raise NotFound("That app doesn't exist.")
            others = [a for a in config["apps"] if a is not existing]
            numbers = self.taken_numbers(config)
            app = model.validate_app(data, existing, others, numbers, config["outbound"])
            if existing is None:
                config["apps"].append(app)
                config["ari"]["enabled"] = True  # adding an app is asking for ARI
            else:
                config["apps"][config["apps"].index(existing)] = app
            result = self._save(config)
            result["app"] = app
            return result

    def delete_app(self, app_id):
        with self.store.lock:
            config = self.store.load()
            before = len(config["apps"])
            config["apps"] = [a for a in config["apps"] if a["id"] != app_id]
            if len(config["apps"]) == before:
                raise NotFound("That app doesn't exist.")
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
    ("PUT", r"/api/ari", lambda b, m, d: b.save_ari(d)),
    ("PUT", r"/api/provisioning", lambda b, m, d: b.save_provisioning(d)),
    ("PUT", r"/api/network", lambda b, m, d: b.save_network(d)),
    ("DELETE", r"/api/provisioning/waiting/([0-9a-f]{12})", lambda b, m, d: b.dismiss_waiting(m[1])),
    ("POST", r"/api/extensions/(\d+)/resync", lambda b, m, d: b.resync(m[1])),
    ("POST", r"/api/apps", lambda b, m, d: b.save_app(None, d)),
    ("PUT", r"/api/apps/([a-z0-9-]+)", lambda b, m, d: b.save_app(m[1], d)),
    ("DELETE", r"/api/apps/([a-z0-9-]+)", lambda b, m, d: b.delete_app(m[1])),
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


class ProvisionHandler(http.server.BaseHTTPRequestHandler):
    """Serves phones' settings files on the LAN (phones can't sign in to Home Assistant)."""

    server_version = "pbx-ui-provisioning"
    backend = None  # set in serve()
    PATH_RE = re.compile(r"^/p/([A-Za-z0-9]{16,64})/([A-Za-z0-9._-]{0,80})$")

    def log_message(self, fmt, *args):
        log.debug("provisioning %s - %s", self.client_address[0], fmt % args)

    def do_GET(self):
        match = self.PATH_RE.match(self.path.split("?", 1)[0])
        if not match:
            status, ctype, body = 404, "text/plain", "Not found\n"
        else:
            try:
                status, ctype, body = self.backend.provision_file(
                    match[1], match[2], self.headers.get("User-Agent", ""),
                    self.client_address[0], self.headers.get("Host", ""),
                )
            except Exception:  # noqa: BLE001
                log.exception("Provisioning request failed")
                status, ctype, body = 500, "text/plain", "Error\n"
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    do_HEAD = do_GET


class Server(http.server.ThreadingHTTPServer):
    """Never shares its port: with SO_REUSEPORT (Python's default in newer versions)
    Linux would let another app listen on the same port and split connections
    between us, so phones could silently get the other app's answers."""

    allow_reuse_port = False
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 0)
            except OSError:
                pass
        super().server_bind()


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
    backend = Backend(settings)
    Handler.backend = backend
    ProvisionHandler.backend = backend
    if settings.provision_port:
        try:
            prov_server = Server(("0.0.0.0", settings.provision_port), ProvisionHandler)
        except OSError as err:
            log.error("Automatic phone setup can't use port %s: %s", settings.provision_port, err)
            backend.provision_error = (
                f"Port {settings.provision_port} is already used by another app on this "
                "Home Assistant, so phones can't download their settings. Change "
                "“Phone Setup Port” in this add-on's Configuration (for example to 8197) "
                "and restart the add-on."
            )
        else:
            threading.Thread(target=prov_server.serve_forever, name="provisioning", daemon=True).start()
            log.info("Automatic phone setup listening on port %s", settings.provision_port)
    server = Server(("0.0.0.0", settings.port), Handler)
    log.info("Web UI listening on port %s", settings.port)
    threading.current_thread().name = "pbx-ui"
    server.serve_forever()
