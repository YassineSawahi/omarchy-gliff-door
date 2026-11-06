"""Tests for door.py. Everything runs against a throwaway $HOME, so the real
~/.ssh/authorized_keys is never touched. Needs ssh-keygen.

    python3 -m unittest discover -s tests
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOOR = REPO / "door.py"


class DoorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gliff-door-test-"))
        self.home = self.tmp / "home"
        ssh = self.home / ".ssh"
        ssh.mkdir(parents=True, mode=0o700)
        (self.tmp / "keys").mkdir()
        self.own = self.keygen(ssh / "id_ed25519", "me@this-pc")
        self.laptop = self.keygen(self.tmp / "keys/laptop", "me@laptop")
        self.other = self.keygen(self.tmp / "keys/other", "me@other")
        self.auth = ssh / "authorized_keys"
        self.original = f"# my keys\n{self.own.read_text().strip()}\n"
        self.auth.write_text(self.original)
        self.auth.chmod(0o600)
        self.stub = self.tmp / "fake-gliff-server"
        self.stub.write_text('#!/bin/sh\necho "ARGS:$*|PEER:$GLIFF_DOOR_PEER"\n')
        self.stub.chmod(0o755)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def keygen(self, path, comment):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(path)], check=True)
        return Path(str(path) + ".pub")

    def env(self, **extra):
        env = {"HOME": str(self.home), "PATH": os.environ.get("PATH", "/usr/bin"),
               "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent", "XDG_RUNTIME_DIR": str(self.tmp)}
        env.update(extra)
        return env

    def door(self, *args):
        r = subprocess.run([sys.executable, "-I", "-B", str(DOOR), *args], capture_output=True, text=True, env=self.env())
        return r.returncode, json.loads(r.stdout)

    def gate(self, command, name="laptop", connection="100.64.0.2 50000 100.64.0.1 22"):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import door; "
                "door.SERVER = sys.argv[2]; door.gate(sys.argv[3])")
        extra = {"SSH_CONNECTION": connection}
        if command is not None:
            extra["SSH_ORIGINAL_COMMAND"] = command
        r = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(REPO), str(self.stub), name],
                           capture_output=True, text=True, env=self.env(**extra))
        return r.returncode, r.stdout.strip(), r.stderr.strip()

    def let_laptop_in(self):
        self.assertEqual(self.door("add", "laptop", "100.64.0.2")[0], 0)
        self.assertEqual(self.door("key", "laptop", str(self.laptop))[0], 0)
        self.assertEqual(self.door("allow", "laptop", "on")[0], 0)

    # ------------------------------------------------------------ switch

    def test_switch_on_adds_one_pinned_line_and_off_restores_the_file(self):
        self.door("add", "laptop", "100.64.0.2")
        code, out = self.door("allow", "laptop", "on")
        self.assertEqual(code, 1, "no key yet")
        self.assertIn("no key", out["message"])

        self.door("key", "laptop", str(self.laptop))
        self.assertEqual(self.door("allow", "laptop", "on")[0], 0)
        lines = self.auth.read_text().splitlines()
        self.assertEqual(lines[:2], self.original.splitlines())
        added = lines[2]
        key = " ".join(self.laptop.read_text().split()[:2])
        self.assertTrue(added.startswith('restrict,from="100.64.0.2",command="/usr/bin/python3 -I '))
        self.assertIn(f'door.py gate laptop" {key} omarchy-gliff-door:laptop', added)
        self.assertEqual(oct(self.auth.stat().st_mode & 0o777), "0o600")

        self.assertEqual(self.door("allow", "laptop", "off")[0], 0)
        self.assertEqual(self.auth.read_text(), self.original)

    def test_toggle_and_remove(self):
        self.let_laptop_in()
        self.door("allow", "laptop", "toggle")
        self.assertNotIn("omarchy-gliff-door:laptop", self.auth.read_text())
        self.door("allow", "laptop", "toggle")
        self.assertIn("omarchy-gliff-door:laptop", self.auth.read_text())
        self.assertEqual(self.door("remove", "laptop")[0], 0)
        self.assertEqual(self.auth.read_text(), self.original)
        self.assertEqual(self.door("allow", "laptop", "on")[0], 1)

    def test_new_key_or_address_rewrites_the_line(self):
        self.let_laptop_in()
        self.door("key", "laptop", str(self.other))
        text = self.auth.read_text()
        self.assertIn(self.other.read_text().split()[1], text)
        self.assertNotIn(self.laptop.read_text().split()[1], text)
        self.door("add", "laptop", "100.64.0.1")
        self.assertIn('from="100.64.0.1"', self.auth.read_text())
        self.assertEqual(self.auth.read_text().count("omarchy-gliff-door:"), 1)

    def test_names_that_would_break_the_line_are_refused(self):
        for name in ['a b', 'x"y', '../x', '-x', 'a' * 33, 'x,command="sh"']:
            self.assertEqual(self.door("add", name, "100.64.0.2")[0], 1, name)
        self.assertFalse((self.home / ".config/omarchy-gliff-door/peers.json").exists())

    # -------------------------------------------------------------- keys

    def test_keys_that_must_not_be_imported(self):
        self.door("add", "laptop", "100.64.0.2")
        key = self.laptop.read_text().strip()
        cases = {
            "private key": str(self.tmp / "keys/laptop"),
            "this PC's own key": str(self.own),
            "two keys": key + "\n" + self.other.read_text(),
            "options in front": 'command="sh" ' + key,
            "not a key": "hello world",
            "broken key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAInotreallyakeyatall",
        }
        for what, source in cases.items():
            code, out = self.door("key", "laptop", source)
            self.assertEqual(code, 1, f"{what}: {out}")
        self.assertIsNone(json.loads((self.home / ".config/omarchy-gliff-door/peers.json").read_text())["peers"]["laptop"]["key"])

    def test_a_key_with_full_access_is_refused(self):
        self.auth.write_text(self.original + self.other.read_text())
        self.door("add", "laptop", "100.64.0.2")
        code, out = self.door("key", "laptop", str(self.other))
        self.assertEqual(code, 1)
        self.assertIn("full SSH access", out["message"])

    def test_key_from_downloads(self):
        self.door("add", "laptop", "100.64.0.2")
        (self.home / "Downloads").mkdir()
        shutil.copy(self.laptop, self.home / "Downloads/id_ed25519.pub")
        code, out = self.door("key", "laptop", "--downloads")
        self.assertEqual(code, 0, out)

    # -------------------------------------------------------------- gate

    def test_gate_runs_gliff_server_with_allowed_options(self):
        self.let_laptop_in()
        cases = {
            "gliff-server --stdio": "--stdio",
            "gliff-server --stdio --headless": "--stdio --headless",
            "gliff-server --stdio --output DP-1": "--stdio --output DP-1",
            "gliff-server --stdio --output=HDMI-A-1 --low-bandwidth": "--stdio --output HDMI-A-1 --low-bandwidth",
            "gliff-server --stdio --video cpu --capture shm --bitrate 8000000 --full-chroma":
                "--stdio --video cpu --capture shm --bitrate 8000000 --full-chroma",
        }
        for command, args in cases.items():
            code, out, err = self.gate(command)
            self.assertEqual((code, out), (0, f"ARGS:{args}|PEER:laptop"), f"{command}: {err}")

    def test_gate_refuses_everything_else(self):
        self.let_laptop_in()
        for command in [
            None,
            "bash",
            "id; cat /etc/passwd",
            "gliff-server --stdio; rm -rf ~",
            "gliff-server --stdio --listen 0.0.0.0:9000 --allow-remote",
            "gliff-server --stdio --instance abc",
            "gliff-server --stdio --render-node /dev/dri/card0",
            "gliff-server --stdio --output $(id)",
            "gliff-server --stdio --output",
            "gliff-server --stdio --headless=1",
            "gliff-server --headless",
            "env XDG_RUNTIME_DIR=/x gliff-server --stdio",
            "sh -c 'gliff-server --stdio'",
            "gliff-server --stdio 'unclosed",
        ]:
            code, out, err = self.gate(command)
            self.assertEqual(code, 1, f"{command!r} ran: {out}")
            self.assertEqual(out, "", command)
            self.assertTrue(err.startswith("gliff-door: "), err)

    def test_gate_refuses_a_machine_whose_switch_is_off(self):
        self.let_laptop_in()
        self.door("allow", "laptop", "off")
        self.assertEqual(self.gate("gliff-server --stdio")[0], 1)
        self.assertEqual(self.gate("gliff-server --stdio", name="someone-else")[0], 1)

    def test_gate_logs_what_it_did(self):
        self.let_laptop_in()
        self.gate("gliff-server --stdio")
        self.gate("bash")
        log = (self.home / ".local/state/omarchy-gliff-door/door.log").read_text()
        self.assertIn("connect laptop from 100.64.0.2: --stdio", log)
        self.assertIn("refused laptop from 100.64.0.2: this key only runs gliff-server", log)


if __name__ == "__main__":
    unittest.main()
