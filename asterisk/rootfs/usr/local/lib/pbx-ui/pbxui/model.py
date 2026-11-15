"""Phone system model for the web UI.

The UI stores everything in one small JSON file (``pbx.json``) and turns it
into two Asterisk config files:

* ``pjsip_ui.conf``      - phones (endpoints) and outside lines (trunks)
* ``extensions_ui.conf`` - how calls to/from outside lines and apps are routed
* ``ari_ui.conf``        - ARI on/off and the logins apps use to control calls

For each app it also writes a small JSON "handoff" file that the app (for
example Kiosk Intercom Gateway) can read to connect without copy and paste.

Every value written to those files is validated against a strict allowlist
first, so nothing typed into the UI can break out of an Asterisk config line.
"""

import json
import os
import re
import secrets
import string
import tempfile
import threading

from . import provision

SCHEMA_VERSION = 1

KINDS = {
    "phone": "Desk phone or phone app",
    "browser": "Home Assistant / web browser",
    "doorbell": "Doorbell or intercom",
}
TRANSPORTS = ("udp", "tcp", "tls")
OUTBOUND_MODES = ("direct", "prefix")

# Internal numbers are 2-4 digits, so in "direct" mode anything with 5+ digits
# (or starting with +) is unambiguously an outside number.
EXTENSION_RE = re.compile(r"^[0-9]{2,4}$")
LONG_NUMBER_MIN_DIGITS = 5
FIRST_UI_EXTENSION = 200  # 100+ is used by the add-on's auto_add for HA people

# Numbers that already mean something in the stock dialplan (call parking).
RESERVED_NUMBERS = {"444", "555"} | {str(n) for n in range(701, 721)}

NAME_FORBIDDEN_RE = re.compile(r'[\x00-\x1f\x7f"<>;\[\]\\]')
PASSWORD_RE = re.compile(r"^[A-Za-z0-9!#$%&()*+,\-./:=?@^_{}~]{8,64}$")
HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9._+*-]{1,64}$")
CALLER_NUMBER_RE = re.compile(r"^\+?[0-9]{2,20}$")
PREFIX_RE = re.compile(r"^[0-9]{1,2}$")
RING_TARGET_RE = re.compile(r"^[0-9]{1,6}$")
TRUNK_ID_RE = re.compile(r"^trunk-[a-z0-9-]{1,40}$")
APP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
ARI_USER_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
STASIS_APP_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
ROUTE_PREFIX_RE = re.compile(r"^[0-9]{1,3}$")

PROVISION_TOKEN_RE = re.compile(r"^[A-Za-z0-9]{16,64}$")
PUBLIC_URL_RE = re.compile(
    r"^(https?)://([A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?)(?::([0-9]{1,5}))?$"
)

ARI_URL = "http://127.0.0.1:8088"
HANDOFF_VERSION = 1

# One-click setups for apps we know about. Values are what the app expects.
PRESETS = {
    "kiosk-intercom-gateway": {
        "name": "Kiosk Intercom Gateway",
        "username": "kiosk-gateway",
        "stasis_app": "kiosk-intercom",
        "route": {"prefix": "8", "length": 3},
        "read_only": False,
        "blurb": "Calls between Kiosk Satellite kiosks and your phones. "
                 "Numbers 800-899 ring kiosks (800 announces to all of them).",
        "url": "https://github.com/jfarcher/kiosk-intercom-gateway",
    },
}


class ValidationError(Exception):
    """A user-facing problem with submitted data."""

    def __init__(self, message, field=None):
        super().__init__(message)
        self.message = message
        self.field = field


# ---------------------------------------------------------------------------
# Defaults and helpers
# ---------------------------------------------------------------------------


def default_config():
    return {
        "version": SCHEMA_VERSION,
        "extensions": [],
        "trunks": [],
        "outbound": {"mode": "direct", "prefix": "9", "trunk": None},
        "ari": {"enabled": False},
        "apps": [],
        "provisioning": {"enabled": False, "token": "", "public_url": ""},
        "network": {"sip_address": ""},
    }


def generate_password(length=16):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _text(data, key, default=""):
    value = data.get(key, default)
    if value is None:
        return default
    if not isinstance(value, (str, int)):
        raise ValidationError("Invalid value.", key)
    return str(value).strip()


def _bool(data, key, default):
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ValidationError("Invalid value.", key)
    return value


def clean_name(value, field="name", label="Name"):
    if not value:
        raise ValidationError(f"{label} is required.", field)
    if len(value) > 60:
        raise ValidationError(f"{label} must be 60 characters or fewer.", field)
    if NAME_FORBIDDEN_RE.search(value):
        raise ValidationError(
            f"{label} can't contain quotes, < >, [ ], \\ or ;.", field
        )
    return value


def clean_password(value, field="password"):
    if not PASSWORD_RE.match(value):
        raise ValidationError(
            "Password must be 8-64 characters: letters, numbers and "
            "!#$%&()*+,-./:=?@^_{}~ (no spaces, quotes or ;).",
            field,
        )
    return value


def parse_server(value):
    """Accept what people paste: 'sip:host', 'host:5080', 'host'."""
    value = value.strip()
    value = re.sub(r"^sips?:", "", value, flags=re.IGNORECASE)
    value = value.split(";", 1)[0].strip("/")
    port = None
    match = re.match(r"^(.*):([0-9]{1,5})$", value)
    if match:
        value, port = match.group(1), int(match.group(2))
    return value, port


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_extension(data, existing=None, taken=(), outbound=None, apps=(), taken_macs=()):
    """Return a clean extension dict built from user input.

    ``existing`` is the stored extension when editing (keeps its password if
    none was supplied). ``taken`` are numbers already used by anything else.
    """
    existing = existing or {}

    number = _text(data, "number", existing.get("number", ""))
    if not EXTENSION_RE.match(number):
        raise ValidationError("Number must be 2 to 4 digits.", "number")
    if number in RESERVED_NUMBERS:
        raise ValidationError(
            f"{number} is reserved for call parking. Pick another number.", "number"
        )
    if number in taken:
        raise ValidationError(f"{number} is already in use.", "number")
    for app in apps:
        if route_matches(app.get("route"), number):
            raise ValidationError(
                f"{route_label(app['route'])} go to {app['name']}. Pick another number.",
                "number",
            )
    if outbound and outbound.get("mode") == "prefix":
        prefix = outbound.get("prefix") or ""
        if prefix and number.startswith(prefix):
            raise ValidationError(
                f"Numbers can't start with {prefix} because that's what you "
                "dial to reach an outside line.",
                "number",
            )

    name = clean_name(_text(data, "name", existing.get("name", "")))

    kind = _text(data, "kind", existing.get("kind", "phone"))
    if kind not in KINDS:
        raise ValidationError("Choose what kind of device this is.", "kind")

    password = _text(data, "password", "") or existing.get("password", "")
    password = clean_password(password or generate_password())

    video = _bool(data, "video", existing.get("video", kind != "phone"))

    prov = clean_provision(data.get("provision", existing.get("provision")), taken_macs)

    return {
        "number": number,
        "name": name,
        "kind": kind,
        "password": password,
        "video": video,
        "provision": prov,
    }


def clean_provision(value, taken_macs=()):
    """Automatic setup for a desk phone: its MAC address and brand, or None."""
    if not value:
        return None
    if not isinstance(value, dict):
        raise ValidationError("Invalid automatic setup.", "mac")
    mac = provision.normalize_mac(value.get("mac"))
    if not mac:
        raise ValidationError(
            "MAC address should be 12 letters and digits, like 80:5E:C0:12:34:56.", "mac"
        )
    if mac in taken_macs:
        raise ValidationError("Another phone is already set up with that MAC address.", "mac")
    vendor = str(value.get("vendor") or "") or provision.vendor_from("", mac)
    if vendor not in provision.VENDORS:
        raise ValidationError("Choose the phone's brand.", "vendor")
    return {"mac": mac, "vendor": vendor}


def slugify(name, taken):
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24] or "line"
    candidate = f"trunk-{base}"
    n = 2
    while candidate in taken:
        candidate = f"trunk-{base}-{n}"
        n += 1
    return candidate


def validate_trunk(data, existing=None, known_numbers=(), taken_ids=(), outbound=None):
    existing = existing or {}

    name = clean_name(_text(data, "name", existing.get("name", "")))

    host, port = parse_server(_text(data, "server", existing.get("server", "")))
    if not host:
        raise ValidationError("Enter your provider's server address.", "server")
    if not HOST_RE.match(host):
        raise ValidationError(
            "Server should look like sip.example.com or an IP address.", "server"
        )
    explicit_port = data.get("port", existing.get("port"))
    if explicit_port not in (None, ""):
        try:
            port = int(explicit_port)
        except (TypeError, ValueError):
            raise ValidationError("Port must be a number.", "port") from None
    if port is not None and not 1 <= port <= 65535:
        raise ValidationError("Port must be between 1 and 65535.", "port")

    username = _text(data, "username", existing.get("username", ""))
    if not USERNAME_RE.match(username):
        raise ValidationError(
            "Username can only contain letters, numbers and . _ + * -", "username"
        )

    auth_username = _text(data, "auth_username", existing.get("auth_username", ""))
    if auth_username and not USERNAME_RE.match(auth_username):
        raise ValidationError(
            "Login name can only contain letters, numbers and . _ + * -",
            "auth_username",
        )

    password = _text(data, "password", "") or existing.get("password", "")
    if not password:
        raise ValidationError("Enter the password from your provider.", "password")
    if not re.match(r"^[^\s;\"\\]{1,128}$", password):
        raise ValidationError(
            "Password can't contain spaces, quotes, \\ or ;.", "password"
        )

    caller_id = _text(data, "caller_id", existing.get("caller_id", ""))
    caller_id = caller_id.replace(" ", "")
    if caller_id and not CALLER_NUMBER_RE.match(caller_id):
        raise ValidationError(
            "Phone number should be digits only, optionally starting with +.",
            "caller_id",
        )

    transport = _text(data, "transport", existing.get("transport", "udp")).lower()
    if transport not in TRANSPORTS:
        raise ValidationError("Choose UDP, TCP or TLS.", "transport")

    register = _bool(data, "register", existing.get("register", True))

    ring = data.get("ring", existing.get("ring", []))
    if not isinstance(ring, list):
        raise ValidationError("Invalid list of phones to ring.", "ring")
    clean_ring = []
    for target in ring:
        target = str(target).strip()
        if not RING_TARGET_RE.match(target):
            raise ValidationError("Invalid phone number in ring list.", "ring")
        if known_numbers and target not in known_numbers:
            raise ValidationError(f"There's no phone with number {target}.", "ring")
        if target not in clean_ring:
            clean_ring.append(target)

    ring_other = clean_ring_other(
        data.get("ring_other", existing.get("ring_other", [])), clean_ring, outbound
    )

    trunk_id = existing.get("id") or slugify(name, taken_ids)

    return {
        "id": trunk_id,
        "name": name,
        "server": host,
        "port": port,
        "transport": transport,
        "username": username,
        "auth_username": auth_username,
        "password": password,
        "caller_id": caller_id,
        "register": register,
        "ring": clean_ring,
        "ring_other": ring_other,
    }


def clean_ring_other(value, phones=(), outbound=None):
    """Extra numbers an outside line rings, such as a kiosk (801) handled by an app.

    They're dialled through [default], so they must look like internal numbers:
    anything that could match a dial-out rule would turn an incoming call into
    an outgoing one.
    """
    if isinstance(value, str):
        value = re.split(r"[\s,;]+", value)
    if not isinstance(value, list):
        raise ValidationError("Invalid list of numbers.", "ring_other")
    prefix = outbound.get("prefix") if outbound and outbound.get("mode") == "prefix" else ""
    clean = []
    for number in value:
        number = str(number).strip()
        if not number:
            continue
        if not EXTENSION_RE.match(number):
            raise ValidationError(f"{number} isn't an internal number (2 to 4 digits).", "ring_other")
        if number in RESERVED_NUMBERS:
            raise ValidationError(f"{number} is for call parking, so it can't ring.", "ring_other")
        if prefix and number.startswith(prefix):
            raise ValidationError(
                f"{number} starts with {prefix}, which dials outside lines.", "ring_other"
            )
        if number not in clean and number not in phones:
            clean.append(number)
    if len(clean) > 10:
        raise ValidationError("Ring at most 10 other numbers.", "ring_other")
    return clean


def validate_outbound(data, trunk_ids, all_numbers, apps=(), other_numbers=()):
    mode = _text(data, "mode", "direct")
    if mode not in OUTBOUND_MODES:
        raise ValidationError("Choose how outside numbers are dialled.", "mode")
    prefix = _text(data, "prefix", "9")
    if mode == "prefix":
        if not PREFIX_RE.match(prefix):
            raise ValidationError("Prefix must be 1 or 2 digits.", "prefix")
        clashes = sorted(n for n in all_numbers if n.startswith(prefix))
        if clashes:
            raise ValidationError(
                f"Prefix {prefix} clashes with phone number(s) "
                f"{', '.join(clashes)}. Pick a different prefix.",
                "prefix",
            )
        clashes = sorted(n for n in other_numbers if n.startswith(prefix))
        if clashes:
            raise ValidationError(
                f"Prefix {prefix} clashes with {', '.join(clashes)}, which an outside "
                "line rings. Pick a different prefix.",
                "prefix",
            )
        for app in apps:
            route = app.get("route")
            if route and (route["prefix"].startswith(prefix) or prefix.startswith(route["prefix"])):
                raise ValidationError(
                    f"Prefix {prefix} clashes with {route_label(route)}, which go to "
                    f"{app['name']}. Pick a different prefix.",
                    "prefix",
                )
    trunk = data.get("trunk") or None
    if trunk is not None and trunk not in trunk_ids:
        raise ValidationError("Choose an outside line.", "trunk")
    if not PREFIX_RE.match(prefix):
        prefix = "9"
    return {"mode": mode, "prefix": prefix, "trunk": trunk}


def route_pattern(route):
    """{"prefix": "8", "length": 3} -> "_8XX"."""
    return "_" + route["prefix"] + "X" * (route["length"] - len(route["prefix"]))


def route_matches(route, number):
    return bool(route) and len(number) == route["length"] and number.startswith(route["prefix"])


def route_label(route):
    pad = route["length"] - len(route["prefix"])
    return f"Numbers {route['prefix']}{'0' * pad} to {route['prefix']}{'9' * pad}"


def routes_overlap(a, b):
    return a["length"] == b["length"] and (
        a["prefix"].startswith(b["prefix"]) or b["prefix"].startswith(a["prefix"])
    )


def clean_route(value):
    if value in (None, "", False):
        return None
    if not isinstance(value, dict):
        raise ValidationError("Invalid number range.", "route_prefix")
    prefix = str(value.get("prefix", "")).strip()
    try:
        length = int(value.get("length"))
    except (TypeError, ValueError):
        raise ValidationError("Choose how many digits the numbers have.", "route_length") from None
    if not ROUTE_PREFIX_RE.match(prefix):
        raise ValidationError("Numbers should start with 1 to 3 digits.", "route_prefix")
    if not 2 <= length <= 4:
        raise ValidationError("Numbers should be 2 to 4 digits long.", "route_length")
    if len(prefix) >= length:
        raise ValidationError(
            "The starting digits must be shorter than the whole number.", "route_prefix"
        )
    return {"prefix": prefix, "length": length}


def validate_app(data, existing=None, others=(), numbers=(), outbound=None):
    """Return a clean app (ARI login + optional number range) from input.

    ``others`` are the other apps; ``numbers`` are phone numbers in use.
    """
    existing = existing or {}
    preset = _text(data, "preset", existing.get("preset", "")) or None
    if preset is not None and preset not in PRESETS:
        raise ValidationError("Unknown app.", "preset")
    defaults = dict(PRESETS.get(preset, {}), **existing)

    name = clean_name(_text(data, "name", defaults.get("name", "")))

    username = _text(data, "username", defaults.get("username", ""))
    if not ARI_USER_RE.match(username) or username.lower() == "general":
        raise ValidationError(
            "Login name can only contain letters, numbers, - and _.", "username"
        )
    if any(o["username"] == username for o in others):
        raise ValidationError(f"Another app already uses the login {username}.", "username")

    password = _text(data, "password", "") or defaults.get("password", "")
    password = clean_password(password or generate_password(24))

    stasis_app = _text(data, "stasis_app", defaults.get("stasis_app", ""))
    if not STASIS_APP_RE.match(stasis_app):
        raise ValidationError(
            "App name can only contain letters, numbers, - and _.", "stasis_app"
        )

    route = clean_route(data.get("route", defaults.get("route")))
    if route:
        for other in others:
            if other.get("route") and routes_overlap(route, other["route"]):
                raise ValidationError(
                    f"{route_label(route)} overlap with {other['name']}'s numbers.",
                    "route_prefix",
                )
        clashes = sorted(n for n in set(numbers) | RESERVED_NUMBERS if route_matches(route, n))
        if clashes:
            raise ValidationError(
                f"{route_label(route)} include {', '.join(clashes)}, which "
                "already ring something else.",
                "route_prefix",
            )
        if outbound and outbound.get("mode") == "prefix":
            prefix = outbound.get("prefix") or ""
            if route["prefix"].startswith(prefix) or prefix.startswith(route["prefix"]):
                raise ValidationError(
                    f"Numbers starting with {prefix} are used to dial outside lines.",
                    "route_prefix",
                )

    read_only = _bool(data, "read_only", defaults.get("read_only", False))

    app_id = existing.get("id") or _unique_id(preset or name, {o["id"] for o in others})
    return {
        "id": app_id,
        "preset": preset,
        "name": name,
        "username": username,
        "password": password,
        "stasis_app": stasis_app,
        "route": route,
        "read_only": read_only,
    }


def _unique_id(name, taken):
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:32] or "app"
    candidate, n = base, 2
    while candidate in taken:
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def normalize(config):
    """Validate a config loaded from disk; raise ValidationError if bad."""
    if not isinstance(config, dict):
        raise ValidationError("pbx.json must contain a JSON object.")
    clean = default_config()
    seen = set()
    macs = set()
    for ext in config.get("extensions", []):
        ext = validate_extension(ext, taken=seen, taken_macs=macs)
        seen.add(ext["number"])
        if ext["provision"]:
            macs.add(ext["provision"]["mac"])
        clean["extensions"].append(ext)
    ids = set()
    for trunk in config.get("trunks", []):
        trunk_id = str(trunk.get("id") or "")
        if not TRUNK_ID_RE.match(trunk_id) or trunk_id in ids:
            raise ValidationError(f"Invalid outside line id {trunk_id!r}.")
        trunk = validate_trunk(trunk, existing={"id": trunk_id})
        ids.add(trunk_id)
        clean["trunks"].append(trunk)
    clean["outbound"] = validate_outbound(config.get("outbound", {}), ids, set())
    ari = config.get("ari") or {}
    clean["ari"] = {"enabled": _bool(ari, "enabled", False)}
    prov = config.get("provisioning") or {}
    token = str(prov.get("token") or "")
    if token and not PROVISION_TOKEN_RE.match(token):
        raise ValidationError("Invalid provisioning token.")
    clean["provisioning"] = {
        "enabled": _bool(prov, "enabled", False),
        "token": token,
        "public_url": clean_public_url(prov.get("public_url")),
    }
    clean["network"] = validate_network(config.get("network") or {})
    for app in config.get("apps", []):
        app_id = str(app.get("id") or "")
        if not APP_ID_RE.match(app_id):
            raise ValidationError(f"Invalid app id {app_id!r}.")
        clean["apps"].append(validate_app(app, {"id": app_id}, others=clean["apps"]))
    return clean


def clean_public_url(value):
    """'https://ha.example.com' (scheme, host, optional port) or ''."""
    url = str(value or "").strip().rstrip("/")
    if not url:
        return ""
    match = PUBLIC_URL_RE.match(url)
    if not match or (match.group(3) and not 1 <= int(match.group(3)) <= 65535):
        raise ValidationError(
            "Enter just the start of the address, like https://ha.example.com "
            "(no path: /p/... is added for you).",
            "public_url",
        )
    return url


def validate_network(data):
    """The address phones should use to reach Asterisk ('' = detect it)."""
    address = _text(data, "sip_address", "")
    address = re.sub(r"^[a-z]+://", "", address, flags=re.IGNORECASE).strip("/")
    if address and not HOST_RE.match(address):
        raise ValidationError(
            "Enter an IP address like 192.168.1.10 or a host name, without a port.",
            "sip_address",
        )
    return {"sip_address": address}


def next_free_number(taken):
    n = FIRST_UI_EXTENSION
    while str(n) in taken or str(n) in RESERVED_NUMBERS:
        n += 1
    return str(n)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

HEADER = """; THIS FILE IS GENERATED BY THE ASTERISK ADD-ON WEB UI.
; Changes made here are overwritten. Use the web UI, or put your own
; configuration in {custom} instead.
"""

CODECS = {
    "phone": "g722,ulaw,alaw,opus",
    "browser": "opus,ulaw,alaw,g722",
    "doorbell": "ulaw,alaw,g722,opus",
}
VIDEO_CODECS = {"phone": "h264", "browser": "vp8,h264", "doorbell": "h264"}
MAX_CONTACTS = {"phone": 3, "browser": 6, "doorbell": 1}


def _section(name, *options):
    """One [name] block from (key, value) pairs; None values are skipped."""
    lines = [f"[{name}]"]
    lines += [f"{key}={value}" for key, value in options if value is not None]
    return "\n".join(lines) + "\n"


def _extension_sections(ext):
    n, kind = ext["number"], ext["kind"]
    return [
        f"; ---- {ext['name']} ({KINDS[kind]})",
        _section(
            n,
            ("type", "endpoint"),
            ("context", "default"),
            ("disallow", "all"),
            ("allow", CODECS[kind]),
            ("allow", VIDEO_CODECS[kind] if ext["video"] else None),
            ("aors", n),
            ("auth", n),
            ("callerid", f'"{ext["name"]}" <{n}>'),
            ("direct_media", "no"),
            ("rtp_symmetric", "yes"),
            ("force_rport", "yes"),
            ("rewrite_contact", "yes"),
            ("send_rpid", "yes"),
            ("send_pai", "yes"),
            ("device_state_busy_at", "1"),
            ("webrtc", "yes" if kind == "browser" else None),
        ),
        _section(
            n,
            ("type", "auth"),
            ("auth_type", "userpass"),
            ("username", n),
            ("password", ext["password"]),
        ),
        _section(
            n,
            ("type", "aor"),
            ("max_contacts", MAX_CONTACTS[kind]),
            ("remove_existing", "yes"),
            # Browsers come and go; polling them just produces noise.
            ("qualify_frequency", 0 if kind == "browser" else 60),
        ),
    ]


def _trunk_sections(trunk):
    tid, host = trunk["id"], trunk["server"]
    hostport = f"{host}:{trunk['port']}" if trunk["port"] else host
    transport = f"transport-{trunk['transport']}"
    sections = [f"; ---- Outside line: {trunk['name']}"]
    if trunk["register"]:
        sections.append(
            _section(
                tid,
                ("type", "registration"),
                ("transport", transport),
                ("outbound_auth", tid),
                ("server_uri", f"sip:{hostport}"),
                ("client_uri", f"sip:{trunk['username']}@{hostport}"),
                ("contact_user", trunk["username"]),
                ("retry_interval", 60),
                ("forbidden_retry_interval", 600),
                ("expiration", 3600),
                ("line", "yes"),
                ("endpoint", tid),
            )
        )
    sections += [
        _section(
            tid,
            ("type", "auth"),
            ("auth_type", "userpass"),
            ("username", trunk["auth_username"] or trunk["username"]),
            ("password", trunk["password"]),
        ),
        _section(
            tid,
            ("type", "aor"),
            ("contact", f"sip:{hostport}"),
            ("qualify_frequency", 60),
        ),
        _section(
            tid,
            ("type", "endpoint"),
            ("transport", transport),
            ("context", f"ui-incoming-{tid}"),
            ("disallow", "all"),
            ("allow", "g722,ulaw,alaw"),
            ("outbound_auth", tid),
            ("aors", tid),
            ("from_user", trunk["username"]),
            ("from_domain", host),
            ("direct_media", "no"),
            ("rtp_symmetric", "yes"),
            ("force_rport", "yes"),
            ("rewrite_contact", "yes"),
        ),
    ]
    if not trunk["register"]:
        sections.append(
            _section(tid, ("type", "identify"), ("endpoint", tid), ("match", host))
        )
    return sections


def render_pjsip(config):
    parts = [HEADER.format(custom="pjsip_custom.conf")]
    for ext in config["extensions"]:
        parts += _extension_sections(ext)
    for trunk in config["trunks"]:
        parts += _trunk_sections(trunk)
    return "\n".join(parts)


def render_ari(config):
    enabled = "yes" if config["ari"]["enabled"] else "no"
    parts = [
        HEADER.format(custom="ari.conf"),
        _section("general", ("enabled", enabled), ("pretty", "no")),
    ]
    for app in config["apps"]:
        parts.append(f"; ---- {app['name']}")
        parts.append(
            _section(
                app["username"],
                ("type", "user"),
                ("read_only", "yes" if app["read_only"] else "no"),
                ("password", app["password"]),
                ("password_format", "plain"),
            )
        )
    return "\n".join(parts)


def _app_dialplan(config):
    out = ["[ui-apps]"]
    routed = [a for a in config["apps"] if a["route"]]
    if not routed:
        out.append("; No numbers are sent to apps.")
    elif not config["ari"]["enabled"]:
        out.append("; ARI is turned off, so numbers aren't sent to apps.")
    else:
        for app in routed:
            out.append(f"; {route_label(app['route'])} go to {app['name']}")
            out.append(f"exten => {route_pattern(app['route'])},1,NoOp({app['name']})")
            out.append(f" same => n,Stasis({app['stasis_app']},${{EXTEN}})")
            out.append(" same => n,Hangup()")
    out.append("")
    return out


def render_handoff(config, app):
    """What an app needs to connect, for it to read from the shared config folder."""
    route = app["route"]
    numbers = None
    if route:
        pad = route["length"] - len(route["prefix"])
        numbers = {
            "pattern": route_pattern(route),
            "prefix": route["prefix"],
            "length": route["length"],
            "first": route["prefix"] + "0" * pad,
            "last": route["prefix"] + "9" * pad,
        }
    return {
        "version": HANDOFF_VERSION,
        "managed_by": "asterisk-addon-web-ui",
        "ari_enabled": config["ari"]["enabled"],
        "ari_url": ARI_URL,
        "username": app["username"],
        "password": app["password"],
        "app": app["stasis_app"],
        "numbers": numbers,
        # The add-on routes the numbers itself: apps must not edit
        # extensions.conf or ari.conf.
        "dialplan_managed": True,
    }


def render_dialplan(config):
    out = [HEADER.format(custom="extensions.conf")]
    out += _app_dialplan(config)
    trunks = {t["id"]: t for t in config["trunks"]}
    outbound = config["outbound"]
    out_trunk = trunks.get(outbound.get("trunk"))

    out.append("[ui-outbound]")
    if out_trunk is None:
        out.append("; No outside line is set up for outgoing calls.")
    elif outbound["mode"] == "prefix":
        prefix = outbound["prefix"]
        skip = len(prefix)
        out.append(f"; Dial {prefix} then the number to call out on \"{out_trunk['name']}\"")
        out.append(f"exten => _{prefix}X.,1,Goto(ui-dial-out,${{EXTEN:{skip}}},1)")
        out.append(f"exten => _{prefix}+X.,1,Goto(ui-dial-out,${{EXTEN:{skip}}},1)")
    else:
        dots = "X" * LONG_NUMBER_MIN_DIGITS
        out.append(
            f"; Numbers with {LONG_NUMBER_MIN_DIGITS}+ digits (or starting with +) "
            f"go out on \"{out_trunk['name']}\""
        )
        out.append(f"exten => _{dots}.,1,Goto(ui-dial-out,${{EXTEN}},1)")
        out.append("exten => _+X.,1,Goto(ui-dial-out,${EXTEN},1)")
    out.append("")

    out.append("[ui-dial-out]")
    if out_trunk is not None:
        out.append(
            f"exten => _[+0-9].,1,NoOp(Outgoing call to ${{EXTEN}} on {out_trunk['name']})"
        )
        if out_trunk["caller_id"]:
            out.append(f" same => n,Set(CALLERID(num)={out_trunk['caller_id']})")
        out.append(f" same => n,Dial(PJSIP/${{EXTEN}}@{out_trunk['id']},120)")
        out.append(" same => n,Hangup()")
    out.append("")

    for trunk in config["trunks"]:
        tid = trunk["id"]
        out.append(f"; Incoming calls on \"{trunk['name']}\"")
        out.append(f"[ui-incoming-{tid}]")
        out.append("exten => _[+0-9a-zA-Z*#]!,1,Goto(s,1)")
        out.append(
            f"exten => s,1,NoOp(Incoming call on {trunk['name']} from ${{CALLERID(all)}})"
        )
        # Phones ring directly; other numbers (such as a kiosk sent to an app)
        # go through [default], where apps' numbers are routed.
        targets = [f"Local/{n}@local-extensions" for n in trunk["ring"]]
        targets += [f"Local/{n}@default" for n in trunk.get("ring_other", [])]
        if targets:
            out.append(f" same => n,Dial({'&'.join(targets)},45)")
        else:
            out.append(" same => n,Answer()")
            out.append(" same => n,Playback(vm-nobodyavail)")
        out.append(" same => n,Hangup()")
        out.append("")

    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def _atomic_write(path, content, mode=0o600):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(content)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


class Store:
    """Loads/saves pbx.json and writes the generated Asterisk files."""

    def __init__(self, config_path, output_dir, integrations_dir=None):
        self.config_path = config_path
        self.output_dir = output_dir
        # /config/asterisk/integrations next to /config/asterisk/ui/pbx.json
        self.integrations_dir = integrations_dir or os.path.join(
            os.path.dirname(os.path.dirname(config_path)), "integrations"
        )
        self.lock = threading.RLock()

    def load(self):
        with self.lock:
            if not os.path.exists(self.config_path):
                return default_config()
            with open(self.config_path) as fh:
                return normalize(json.load(fh))

    def save(self, config):
        with self.lock:
            _atomic_write(self.config_path, json.dumps(config, indent=2) + "\n")
            self.write_asterisk_files(config)

    def write_asterisk_files(self, config):
        _atomic_write(
            os.path.join(self.output_dir, "pjsip_ui.conf"), render_pjsip(config), 0o640
        )
        _atomic_write(
            os.path.join(self.output_dir, "extensions_ui.conf"),
            render_dialplan(config),
            0o640,
        )
        _atomic_write(os.path.join(self.output_dir, "ari_ui.conf"), render_ari(config), 0o640)
        self.write_handoffs(config)

    def write_handoffs(self, config):
        """One JSON file per app; files for removed apps are deleted."""
        wanted = {app["id"] for app in config["apps"]}
        if not wanted and not os.path.isdir(self.integrations_dir):
            return
        for app in config["apps"]:
            _atomic_write(
                os.path.join(self.integrations_dir, f"{app['id']}.json"),
                json.dumps(render_handoff(config, app), indent=2) + "\n",
                0o600,
            )
        for name in os.listdir(self.integrations_dir):
            if name.endswith(".json") and name[:-5] not in wanted:
                os.unlink(os.path.join(self.integrations_dir, name))
