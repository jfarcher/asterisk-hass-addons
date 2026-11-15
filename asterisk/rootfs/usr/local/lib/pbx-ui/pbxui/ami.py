"""Minimal Asterisk Manager Interface (AMI) client.

Used to read live status (who's online, line registration, active calls) and
to place test calls. Connects to a local-only AMI user that the add-on
creates at startup.
"""

import itertools
import socket


class AmiError(Exception):
    pass


class Ami:
    def __init__(self, host, port, username, secret, timeout=5.0):
        self.host = host
        self.port = port
        self.username = username
        self.secret = secret
        self.timeout = timeout
        self._ids = itertools.count(1)
        self._sock = None
        self._file = None

    def __enter__(self):
        try:
            self._sock = socket.create_connection((self.host, self.port), self.timeout)
        except OSError as err:
            raise AmiError(f"Can't reach Asterisk: {err}") from err
        self._file = self._sock.makefile("rb")
        self._file.readline()  # "Asterisk Call Manager/x.y" banner
        resp, _ = self.action(
            "Login", Username=self.username, Secret=self.secret, Events="off"
        )
        if resp.get("Response") != "Success":
            self.close()
            raise AmiError("Asterisk refused the web UI's login.")
        return self

    def __exit__(self, *exc):
        try:
            self._send("Logoff")
        except OSError:
            pass
        self.close()

    def close(self):
        for closable in (self._file, self._sock):
            if closable is not None:
                try:
                    closable.close()
                except OSError:
                    pass
        self._file = self._sock = None

    def _send(self, action, **fields):
        action_id = str(next(self._ids))
        lines = [f"Action: {action}", f"ActionID: {action_id}"]
        for key, value in fields.items():
            if value is None:
                continue
            text = str(value)
            if "\r" in text or "\n" in text:
                raise AmiError("Invalid characters in request.")
            lines.append(f"{key}: {text}")
        self._sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        return action_id

    def _read_message(self):
        message = {}
        while True:
            raw = self._file.readline()
            if not raw:
                raise AmiError("Asterisk closed the connection.")
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line:
                if message:
                    return message
                continue
            key, sep, value = line.partition(":")
            if not sep:
                continue
            key, value = key.strip(), value.strip()
            if key in message:
                existing = message[key]
                if not isinstance(existing, list):
                    message[key] = existing = [existing]
                existing.append(value)
            else:
                message[key] = value

    def action(self, action, **fields):
        """Send an action; return (response, events) for event-list actions."""
        action_id = self._send(action, **fields)
        response = None
        events = []
        while True:
            msg = self._read_message()
            if msg.get("ActionID") != action_id:
                continue
            if response is None and "Response" in msg:
                response = msg
                if msg.get("EventList") != "start":
                    return response, events
                continue
            if msg.get("EventList") == "Complete":
                return response, events
            events.append(msg)

    def command(self, command):
        response, _ = self.action("Command", Command=command)
        output = response.get("Output", [])
        if isinstance(output, str):
            output = [output]
        if response.get("Response") != "Success" and not output:
            return response.get("Message", "Command failed.")
        return "\n".join(output)
