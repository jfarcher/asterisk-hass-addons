"""Automatic setup (provisioning) for SIP desk phones.

Phones download their settings over HTTP when they start, from a URL typed
into the phone or handed out by the router (DHCP option 66). Each brand asks
for differently named files; this module works out who is asking, which phone
it is (by MAC address) and renders that brand's settings file for the phone's
extension.

A phone whose MAC address isn't attached to an extension yet is remembered as
"waiting", so the web UI can offer to set it up.

All values written into the files have already been validated by ``model``
(names can't contain quotes, < >, ; and so on); XML values are escaped anyway.
"""

import re
import threading
import time
from xml.sax.saxutils import escape, quoteattr

VENDORS = {
    "yealink": "Yealink",
    "grandstream": "Grandstream",
    "snom": "Snom",
    "fanvil": "Fanvil",
    "polycom": "Poly / Polycom",
}

# SIP NOTIFY (from Asterisk's stock pjsip_notify.conf) that makes a registered
# phone download its settings again.
CHECK_SYNC = {
    "yealink": "polycom-check-cfg",  # Event: check-sync
    "grandstream": "polycom-check-cfg",
    "fanvil": "polycom-check-cfg",
    "polycom": "polycom-check-cfg",
    "snom": "snom-check-cfg",  # Event: check-sync;reboot=false
}

# Fallback when the User-Agent doesn't say: the maker's MAC address prefixes.
OUI = {
    "yealink": ("001565", "805ec0", "249ad8", "44dbd2", "805e0c"),
    "grandstream": ("000b82", "c074ad", "ec74d7"),
    "snom": ("000413",),
    "fanvil": ("0c383e", "7c2f80", "0c1105"),
    "polycom": ("0004f2", "64167f", "482567", "000420"),
}

MAC_RE = re.compile(r"^[0-9a-f]{12}$")

_UA_VENDOR = [
    ("yealink", re.compile(r"yealink", re.I)),
    ("grandstream", re.compile(r"grandstream", re.I)),
    ("snom", re.compile(r"\bsnom", re.I)),
    ("fanvil", re.compile(r"fanvil", re.I)),
    ("polycom", re.compile(r"polycom|\bpoly\b", re.I)),
]
_UA_MODEL = {
    "yealink": re.compile(r"Yealink[ _-]+(?:SIP[ -]+)?([A-Za-z]+[0-9][A-Za-z0-9]*)", re.I),
    "grandstream": re.compile(r"Grandstream (?:Model HW )?([A-Z]+[0-9][A-Za-z0-9]*)", re.I),
    "snom": re.compile(r"snom([A-Za-z]*[0-9][A-Za-z0-9]*)", re.I),
    "fanvil": re.compile(r"Fanvil[ _-]+([A-Za-z]+[0-9][A-Za-z0-9]*)", re.I),
    "polycom": re.compile(r"Polycom[A-Za-z]*-([A-Za-z]+_?[0-9][A-Za-z0-9_]*)", re.I),
}
_UA_MAC = re.compile(r"\b([0-9A-Fa-f]{12})\b")


def normalize_mac(value):
    """'80:5E:C0:AA:BB:CC', '80-5e-c0-aa-bb-cc' -> '805ec0aabbcc' (or '' if invalid)."""
    mac = re.sub(r"[^0-9a-fA-F]", "", str(value or "")).lower()
    return mac if MAC_RE.match(mac) else ""


def format_mac(mac):
    return ":".join(mac[i:i + 2] for i in range(0, 12, 2)).upper()


def vendor_from(user_agent, mac=""):
    for vendor, rx in _UA_VENDOR:
        if rx.search(user_agent or ""):
            return vendor
    for vendor, prefixes in OUI.items():
        if mac.startswith(prefixes):
            return vendor
    return None


def model_from(vendor, user_agent):
    rx = _UA_MODEL.get(vendor)
    match = rx.search(user_agent or "") if rx else None
    return match.group(1).upper().replace("_", " ") if match else ""


# ---------------------------------------------------------------------------
# Which file is being asked for
# ---------------------------------------------------------------------------

_PATTERNS = [
    # (regex on the lower-cased file name, kind)
    (re.compile(r"^([0-9a-f]{12})-asterisk\.cfg$"), "polycom-config"),
    (re.compile(r"^cfg([0-9a-f]{12})(?:\.xml)?$"), "grandstream"),
    (re.compile(r"^snom[a-z0-9]*-([0-9a-f]{12})\.(?:htm|xml)$"), "snom"),
    (re.compile(r"^snom[a-z0-9]*\.(?:htm|xml)$"), "snom"),
    (re.compile(r"^([0-9a-f]{12})\.(?:cfg|xml)$"), "mac"),
]


def parse_request(filename, user_agent):
    """Return (mac, vendor, kind) for a settings request, or None if it's not one.

    ``kind`` is the file to render: a brand name, or 'polycom-master' /
    'polycom-config' for Poly's two-step setup.
    """
    name = filename.lower()
    for rx, kind in _PATTERNS:
        match = rx.match(name)
        if not match:
            continue
        mac = match.group(1) if match.groups() else ""
        if not mac:  # snomD785.htm: the MAC is in the User-Agent
            found = _UA_MAC.search(user_agent or "")
            mac = found.group(1).lower() if found else ""
        if not MAC_RE.match(mac):
            return None
        if kind == "mac":
            vendor = vendor_from(user_agent, mac)
            if vendor not in ("yealink", "fanvil", "polycom"):
                return None
            kind = "polycom-master" if vendor == "polycom" else vendor
        elif kind == "polycom-config":
            vendor = "polycom"
        else:
            vendor = kind
        return mac, vendor, kind
    return None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render(kind, ext, server, port=5060, mac=""):
    """The settings file for one phone. Returns (content_type, body)."""
    name, number, password = ext["name"], ext["number"], ext["password"]
    if kind == "yealink":
        return "text/plain", "\n".join([
            "#!version:1.0.0.1",
            "## Set up by the Asterisk add-on for Home Assistant",
            "account.1.enable = 1",
            f"account.1.label = {name}",
            f"account.1.display_name = {name}",
            f"account.1.auth_name = {number}",
            f"account.1.user_name = {number}",
            f"account.1.password = {password}",
            f"account.1.sip_server.1.address = {server}",
            f"account.1.sip_server.1.port = {port}",
            "account.1.sip_server.1.transport_type = 0",
            "account.1.sip_server.1.expires = 300",
            "account.1.nat.udp_update_enable = 1",
            "",
        ])
    if kind == "fanvil":
        return "text/plain", "\n".join([
            "<<VOIP CONFIG FILE>>Version:2.0000000000",
            "<SIP CONFIG MODULE>",
            "--SIP Line List--  :",
            f"SIP1 Phone Number       :{number}",
            f"SIP1 Display Name       :{name}",
            f"SIP1 Register Addr      :{server}",
            f"SIP1 Register Port      :{port}",
            f"SIP1 Register User      :{number}",
            f"SIP1 Register Pswd      :{password}",
            "SIP1 Register TTL       :300",
            "SIP1 Enable Reg         :1",
            "<<END OF FILE>>",
            "",
        ])
    if kind == "grandstream":
        values = [
            ("P271", "1"),  # account active
            ("P270", name),  # account name
            ("P47", f"{server}:{port}"),  # SIP server
            ("P35", number),  # SIP user ID
            ("P36", number),  # authenticate ID
            ("P34", password),  # authenticate password
            ("P3", name),  # display name
            ("P31", "1"),  # register
            ("P32", "5"),  # register expiration (minutes)
        ]
        body = "\n".join(f"    <{k}>{escape(v)}</{k}>" for k, v in values)
        return "application/xml", (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<gs_provision version="1">\n'
            "  <config version=\"1\">\n"
            f"{body}\n"
            "  </config>\n"
            "</gs_provision>\n"
        )
    if kind == "snom":
        values = [
            ("user_active", "on"),
            ("user_realname", name),
            ("user_name", number),
            ("user_pname", number),
            ("user_pass", password),
            ("user_host", f"{server}:{port}"),
            ("user_idle_text", name),
            ("user_expiry", "300"),
        ]
        body = "\n".join(
            f'    <{k} idx="1" perm="">{escape(v)}</{k}>' for k, v in values
        )
        return "application/xml", (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            "<settings>\n"
            '  <phone-settings e="2">\n'
            f"{body}\n"
            "  </phone-settings>\n"
            "</settings>\n"
        )
    if kind == "polycom-master":
        return "application/xml", (
            '<?xml version="1.0" standalone="yes"?>\n'
            '<APPLICATION APP_FILE_PATH="sip.ld" '
            f'CONFIG_FILES="{mac}-asterisk.cfg" MISC_FILES="" '
            'LOG_FILE_DIRECTORY="" OVERRIDES_DIRECTORY="" CONTACTS_DIRECTORY="" '
            'LICENSE_DIRECTORY="" USER_PROFILES_DIRECTORY="" CALL_LISTS_DIRECTORY=""/>\n'
        )
    if kind == "polycom-config":
        attrs = [
            ("reg.1.address", number),
            ("reg.1.auth.userId", number),
            ("reg.1.auth.password", password),
            ("reg.1.displayName", name),
            ("reg.1.label", name),
            ("reg.1.server.1.address", server),
            ("reg.1.server.1.port", str(port)),
            ("reg.1.server.1.transport", "UDPOnly"),
            ("reg.1.server.1.expires", "300"),
        ]
        body = " ".join(f"{k}={quoteattr(v)}" for k, v in attrs)
        return "application/xml", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f"<polycomConfig>\n  <reg {body}/>\n</polycomConfig>\n"
        )
    raise ValueError(f"unknown kind {kind}")


# ---------------------------------------------------------------------------
# Phones seen
# ---------------------------------------------------------------------------


class Seen:
    """Phones that asked for settings: waiting ones and when known ones last did."""

    MAX_WAITING = 50
    FORGET_AFTER = 7 * 24 * 3600

    def __init__(self):
        self._lock = threading.Lock()
        self._waiting = {}  # mac -> info
        self._fetched = {}  # mac -> timestamp

    def waiting(self, mac, vendor, user_agent, address):
        with self._lock:
            self._waiting[mac] = {
                "mac": mac,
                "vendor": vendor,
                "model": model_from(vendor, user_agent),
                "address": address,
                "seen": time.time(),
            }
            if len(self._waiting) > self.MAX_WAITING:
                oldest = min(self._waiting.values(), key=lambda w: w["seen"])
                self._waiting.pop(oldest["mac"])

    def fetched(self, mac):
        with self._lock:
            self._fetched[mac] = time.time()
            self._waiting.pop(mac, None)

    def dismiss(self, mac):
        with self._lock:
            return self._waiting.pop(mac, None) is not None

    def snapshot(self, known_macs):
        now = time.time()
        with self._lock:
            for mac in [m for m, w in self._waiting.items() if now - w["seen"] > self.FORGET_AFTER]:
                self._waiting.pop(mac)
            waiting = [dict(w) for m, w in self._waiting.items() if m not in known_macs]
            fetched = {m: t for m, t in self._fetched.items() if m in known_macs}
        return sorted(waiting, key=lambda w: -w["seen"]), fetched
