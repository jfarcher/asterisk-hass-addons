"""Tests for the web UI's config model.

Run from the repository root:
    python3 -m unittest discover -s asterisk/tests
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "rootfs", "usr", "local", "lib", "pbx-ui"),
)

from pbxui import model, provision  # noqa: E402
from pbxui.model import ValidationError  # noqa: E402

TRUNK = {
    "name": "My Provider",
    "server": "sip:sip.example.com:5080",
    "username": "4412345",
    "password": "s3cret!",
    "caller_id": "+44 1234 5678",
    "ring": ["200"],
}


class ExtensionValidation(unittest.TestCase):
    def test_minimal_extension_gets_password_and_video_default(self):
        ext = model.validate_extension({"number": "200", "name": "Kitchen", "kind": "browser"})
        self.assertEqual(ext["number"], "200")
        self.assertTrue(model.PASSWORD_RE.match(ext["password"]))
        self.assertTrue(ext["video"])
        phone = model.validate_extension({"number": "201", "name": "Desk", "kind": "phone"})
        self.assertFalse(phone["video"])

    def test_rejects_config_injection_in_name(self):
        for name in ['Bob" <1>', "x\n[evil]", "a;b", "[200]"]:
            with self.assertRaises(ValidationError, msg=name):
                model.validate_extension({"number": "200", "name": name})

    def test_rejects_bad_password(self):
        for pw in ["short", "has space1", "semi;colon", 'quote"quote']:
            with self.assertRaises(ValidationError, msg=pw):
                model.validate_extension({"number": "200", "name": "A", "password": pw})

    def test_number_rules(self):
        for number in ["1", "12345", "abc", "444", "701"]:
            with self.assertRaises(ValidationError, msg=number):
                model.validate_extension({"number": number, "name": "A"})
        with self.assertRaises(ValidationError):
            model.validate_extension({"number": "200", "name": "A"}, taken={"200"})
        with self.assertRaises(ValidationError):
            model.validate_extension(
                {"number": "900", "name": "A"}, outbound={"mode": "prefix", "prefix": "9"}
            )

    def test_edit_keeps_password_when_blank(self):
        existing = model.validate_extension({"number": "200", "name": "A", "password": "abcdefgh"})
        edited = model.validate_extension({"name": "B", "password": ""}, existing)
        self.assertEqual(edited["password"], "abcdefgh")
        self.assertEqual(edited["name"], "B")

    def test_next_free_number_skips_taken(self):
        self.assertEqual(model.next_free_number({"200", "201"}), "202")


class TrunkValidation(unittest.TestCase):
    def test_parses_pasted_server(self):
        trunk = model.validate_trunk(TRUNK, known_numbers={"200"})
        self.assertEqual(trunk["server"], "sip.example.com")
        self.assertEqual(trunk["port"], 5080)
        self.assertEqual(trunk["caller_id"], "+4412345678")
        self.assertEqual(trunk["id"], "trunk-my-provider")

    def test_unique_id(self):
        trunk = model.validate_trunk(TRUNK, taken_ids={"trunk-my-provider"})
        self.assertEqual(trunk["id"], "trunk-my-provider-2")

    def test_rejects_unknown_ring_target(self):
        with self.assertRaises(ValidationError):
            model.validate_trunk(dict(TRUNK, ring=["999"]), known_numbers={"200"})

    def test_rejects_injection(self):
        for field, value in [("server", "a.com\n[x]"), ("username", "a@b"), ("password", "a;b")]:
            with self.assertRaises(ValidationError, msg=field):
                model.validate_trunk(dict(TRUNK, **{field: value}))

    def test_prefix_clash(self):
        with self.assertRaises(ValidationError):
            model.validate_outbound({"mode": "prefix", "prefix": "1"}, set(), {"100"})


class Rendering(unittest.TestCase):
    def setUp(self):
        self.config = model.default_config()
        self.config["extensions"] = [
            model.validate_extension({"number": "200", "name": "Kitchen", "kind": "phone"}),
            model.validate_extension({"number": "201", "name": "Tablet", "kind": "browser"}),
        ]
        trunk = model.validate_trunk(TRUNK)
        self.config["trunks"] = [trunk]
        self.config["outbound"]["trunk"] = trunk["id"]

    def test_pjsip(self):
        conf = model.render_pjsip(self.config)
        self.assertIn('callerid="Kitchen" <200>', conf)
        self.assertIn("webrtc=yes", conf)
        self.assertEqual(conf.count("webrtc=yes"), 1)
        self.assertIn("server_uri=sip:sip.example.com:5080", conf)
        self.assertIn("client_uri=sip:4412345@sip.example.com:5080", conf)
        self.assertIn("context=ui-incoming-trunk-my-provider", conf)
        self.assertNotIn("type=identify", conf)

    def test_dialplan_direct(self):
        plan = model.render_dialplan(self.config)
        self.assertIn("exten => _XXXXX.,1,Goto(ui-dial-out,${EXTEN},1)", plan)
        self.assertIn("Set(CALLERID(num)=+4412345678)", plan)
        self.assertIn("Dial(PJSIP/${EXTEN}@trunk-my-provider,120)", plan)
        self.assertIn("Dial(Local/200@local-extensions,45)", plan)

    def test_dialplan_prefix(self):
        self.config["outbound"].update(mode="prefix", prefix="9")
        plan = model.render_dialplan(self.config)
        self.assertIn("exten => _9X.,1,Goto(ui-dial-out,${EXTEN:1},1)", plan)

    def test_no_trunk_means_no_outbound_rules(self):
        plan = model.render_dialplan(model.default_config())
        self.assertIn("[ui-outbound]", plan)
        self.assertNotIn("exten =>", plan)

    def test_store_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = model.Store(os.path.join(tmp, "ui", "pbx.json"), tmp)
            store.save(self.config)
            self.assertEqual(store.load(), self.config)
            self.assertEqual(os.stat(store.config_path).st_mode & 0o777, 0o600)
            self.assertTrue(os.path.exists(os.path.join(tmp, "pjsip_ui.conf")))
            with open(store.config_path) as fh:
                json.load(fh)


class Apps(unittest.TestCase):
    def kig(self, **extra):
        return model.validate_app(dict({"preset": "kiosk-intercom-gateway"}, **extra))

    def test_preset_fills_everything(self):
        app = self.kig()
        self.assertEqual(app["id"], "kiosk-intercom-gateway")
        self.assertEqual(app["username"], "kiosk-gateway")
        self.assertEqual(app["stasis_app"], "kiosk-intercom")
        self.assertEqual(model.route_pattern(app["route"]), "_8XX")
        self.assertFalse(app["read_only"])
        self.assertTrue(model.PASSWORD_RE.match(app["password"]))

    def test_route_clashes(self):
        app = self.kig()
        with self.assertRaises(ValidationError):
            model.validate_extension({"number": "801", "name": "A"}, apps=[app])
        model.validate_extension({"number": "8011", "name": "A"}, apps=[app])  # 4 digits is fine
        with self.assertRaises(ValidationError):  # overlapping number ranges
            model.validate_app({"preset": "kiosk-intercom-gateway", "username": "x"}, others=[app])
        with self.assertRaises(ValidationError):  # phone 812 already exists
            model.validate_app({"preset": "kiosk-intercom-gateway"}, numbers={"812"})
        with self.assertRaises(ValidationError):  # parking
            model.validate_app({"name": "P", "username": "p", "stasis_app": "p",
                                "route": {"prefix": "7", "length": 3}})
        with self.assertRaises(ValidationError):
            model.validate_outbound({"mode": "prefix", "prefix": "8"}, set(), set(), [app])

    def test_rejects_injection(self):
        for field, value in [("username", "a\n[x]"), ("stasis_app", "a,b"), ("name", "x;y")]:
            with self.assertRaises(ValidationError, msg=field):
                self.kig(**{field: value})

    def test_rendering_and_handoff(self):
        config = model.default_config()
        config["apps"] = [self.kig()]
        ari = model.render_ari(config)
        self.assertIn("enabled=no", ari)
        self.assertIn("[kiosk-gateway]", ari)
        self.assertNotIn("Stasis(", model.render_dialplan(config))  # ARI off
        config["ari"]["enabled"] = True
        self.assertIn("enabled=yes", model.render_ari(config))
        plan = model.render_dialplan(config)
        self.assertIn("exten => _8XX,1,NoOp(Kiosk Intercom Gateway)", plan)
        self.assertIn(" same => n,Stasis(kiosk-intercom,${EXTEN})", plan)
        handoff = model.render_handoff(config, config["apps"][0])
        self.assertEqual(handoff["numbers"]["first"], "800")
        self.assertEqual(handoff["ari_url"], "http://127.0.0.1:8088")
        self.assertTrue(handoff["dialplan_managed"])

    def test_store_writes_and_prunes_handoffs(self):
        config = model.default_config()
        config["ari"]["enabled"] = True
        config["apps"] = [self.kig()]
        with tempfile.TemporaryDirectory() as tmp:
            store = model.Store(os.path.join(tmp, "asterisk", "ui", "pbx.json"), tmp)
            store.save(config)
            handoff = os.path.join(tmp, "asterisk", "integrations", "kiosk-intercom-gateway.json")
            with open(handoff) as fh:
                self.assertEqual(json.load(fh)["username"], "kiosk-gateway")
            self.assertEqual(store.load(), config)
            config["apps"] = []
            store.save(config)
            self.assertFalse(os.path.exists(handoff))


class Provisioning(unittest.TestCase):
    EXT = {"number": "200", "name": "Kitchen & Co", "password": "p&ss<word>1", "kind": "phone"}

    def test_parse_requests(self):
        cases = [
            ("805ec0123456.cfg", "Yealink SIP-T46U 108.86.0.20 80:5e:c0:12:34:56", ("805ec0123456", "yealink", "yealink")),
            ("0c383e123456.cfg", "Fanvil X4U 2.12.1 0c383e123456", ("0c383e123456", "fanvil", "fanvil")),
            ("0004f2123456.cfg", "FileTransport PolycomVVX-VVX_450-UA/6.4.3.5018", ("0004f2123456", "polycom", "polycom-master")),
            ("0004f2123456-asterisk.cfg", "FileTransport PolycomVVX-VVX_450-UA/6.4.3", ("0004f2123456", "polycom", "polycom-config")),
            ("cfg000b82123456.xml", "Grandstream Model HW GRP2614 SW 1.0.5.68 DevId 000b82123456", ("000b82123456", "grandstream", "grandstream")),
            ("snomD785.htm", "Mozilla/4.0 (compatible; snomD785-SIP 10.1.159.12 000413123456)", ("000413123456", "snom", "snom")),
            ("snomD785-000413123456.htm", "", ("000413123456", "snom", "snom")),
            ("805EC0123456.cfg", "", ("805ec0123456", "yealink", "yealink")),  # OUI fallback
        ]
        for name, ua, expected in cases:
            self.assertEqual(provision.parse_request(name, ua), expected, name)
        for name in ["y000000000028.cfg", "000000000000.cfg", "sip.ld", "../etc/passwd", "aabbccddeeff.cfg"]:
            self.assertIsNone(provision.parse_request(name, "curl/8"), name)

    def test_models(self):
        self.assertEqual(provision.model_from("yealink", "Yealink SIP-T46U 108.86"), "T46U")
        self.assertEqual(provision.model_from("polycom", "FileTransport PolycomVVX-VVX_450-UA/6"), "VVX 450")
        self.assertEqual(provision.model_from("snom", "snomD785-SIP 10"), "D785")

    def test_render_all_brands(self):
        for kind in ["yealink", "fanvil", "grandstream", "snom", "polycom-config"]:
            ctype, body = provision.render(kind, self.EXT, "192.168.1.10")
            self.assertIn("200", body, kind)
            self.assertIn("192.168.1.10", body, kind)
            if ctype == "application/xml":
                import xml.dom.minidom
                xml.dom.minidom.parseString(body)  # well-formed despite & and < >
                self.assertNotIn("p&ss<", body)
        _, master = provision.render("polycom-master", self.EXT, "h", mac="0004f2123456")
        self.assertIn('CONFIG_FILES="0004f2123456-asterisk.cfg"', master)

    def test_extension_provision_validation(self):
        ext = model.validate_extension({"number": "200", "name": "A",
                                        "provision": {"mac": "80:5E:C0:12:34:56"}})
        self.assertEqual(ext["provision"], {"mac": "805ec0123456", "vendor": "yealink"})
        with self.assertRaises(ValidationError):
            model.validate_extension({"number": "200", "name": "A", "provision": {"mac": "nope"}})
        with self.assertRaises(ValidationError):  # brand unknown and not guessable
            model.validate_extension({"number": "200", "name": "A", "provision": {"mac": "aabbccddeeff"}})
        with self.assertRaises(ValidationError):
            model.validate_extension({"number": "200", "name": "A", "provision": {"mac": "805ec0123456"}},
                                     taken_macs={"805ec0123456"})

    def test_seen(self):
        seen = provision.Seen()
        seen.waiting("805ec0123456", "yealink", "Yealink SIP-T46U 108", "192.168.1.50")
        waiting, _ = seen.snapshot(set())
        self.assertEqual(waiting[0]["model"], "T46U")
        seen.fetched("805ec0123456")
        waiting, fetched = seen.snapshot({"805ec0123456"})
        self.assertEqual(waiting, [])
        self.assertIn("805ec0123456", fetched)


class Network(unittest.TestCase):
    def test_sip_address(self):
        self.assertEqual(model.validate_network({"sip_address": " 192.168.1.10 "}), {"sip_address": "192.168.1.10"})
        self.assertEqual(model.validate_network({"sip_address": "http://ha.local/"}), {"sip_address": "ha.local"})
        self.assertEqual(model.validate_network({}), {"sip_address": ""})
        self.assertEqual(model.clean_public_url(" https://ha.example.com/ "), "https://ha.example.com")
        self.assertEqual(model.clean_public_url("http://ha.example.com:8443"), "http://ha.example.com:8443")
        for bad in ["ha.example.com", "https://ha.example.com/p/x", "ftp://x", "https://x:99999", "https://a b"]:
            with self.assertRaises(ValidationError, msg=bad):
                model.clean_public_url(bad)
        for bad in ["192.168.1.10:5060", "a b", "x;y"]:
            with self.assertRaises(ValidationError, msg=bad):
                model.validate_network({"sip_address": bad})


class RingOther(unittest.TestCase):
    def test_validation(self):
        t = model.validate_trunk(dict(TRUNK, ring_other="801, 802 801"))
        self.assertEqual(t["ring_other"], ["801", "802"])
        self.assertEqual(model.validate_trunk(dict(TRUNK, ring_other=["200"]))["ring_other"], [])  # already a phone
        for bad in ["07700900123", "+44", "444", "8a1"]:
            with self.assertRaises(ValidationError, msg=bad):
                model.validate_trunk(dict(TRUNK, ring_other=[bad]))
        with self.assertRaises(ValidationError):  # would match the dial-out prefix
            model.validate_trunk(dict(TRUNK, ring_other=["901"]), outbound={"mode": "prefix", "prefix": "9"})
        with self.assertRaises(ValidationError):  # switching to a clashing prefix
            model.validate_outbound({"mode": "prefix", "prefix": "8"}, set(), set(), (), {"801"})

    def test_dialplan(self):
        config = model.default_config()
        config["trunks"] = [model.validate_trunk(dict(TRUNK, ring_other=["801"]))]
        plan = model.render_dialplan(config)
        self.assertIn("Dial(Local/200@local-extensions&Local/801@default,45)", plan)


if __name__ == "__main__":
    unittest.main()
