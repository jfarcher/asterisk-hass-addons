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

from pbxui import model  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
