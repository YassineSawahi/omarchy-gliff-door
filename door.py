#!/usr/bin/env python3
"""Gliff Door: let named machines on your tailnet remote-desktop into this PC
with Gliff, and only Gliff, each behind its own switch in the bar.

A machine's SSH key sits in ~/.ssh/authorized_keys only while its switch is
on, on a line tagged `omarchy-gliff-door:<name>` and pinned with:

  restrict                 no shell, no pty, no port/agent/X11 forwarding
  from="<tailscale ip>"    only from that machine's address
  command="door.py gate"   every login runs the gate below, which starts
                           `gliff-server --stdio` and nothing else

Switching off removes the line and ends that machine's running sessions.

  door.py [status]                         JSON for the bar widget
  door.py add <name> <tailscale-name|ip>   add a machine (its switch starts off)
  door.py key <name> <file|key|-|--downloads>
  door.py allow <name> on|off|toggle
  door.py kick <pid|name>                  end a running session
  door.py remove <name>                    switch off and forget the machine
  door.py copy connect|key|add             put a setup command on the clipboard
  door.py gate <name>                      run by sshd, not by hand

Machines live in ~/.config/omarchy-gliff-door/peers.json; the log is
~/.local/state/omarchy-gliff-door/door.log.
"""

import contextlib
import fcntl
import getpass
import ipaddress
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True

HOME = Path.home()
SCRIPT = Path(__file__).resolve()
CONFIG = HOME / ".config/omarchy-gliff-door/peers.json"
STATE = HOME / ".local/state/omarchy-gliff-door"
LOG = STATE / "door.log"
AUTH = HOME / ".ssh/authorized_keys"
DOWNLOADS = HOME / "Downloads"
SERVER = "/usr/bin/gliff-server"
TAG = "omarchy-gliff-door:"
PEER_ENV = "GLIFF_DOOR_PEER"

NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")
KEY_RE = re.compile(
    r"(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)"
    r"|sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com)"
    r" ([A-Za-z0-9+/]{16,}={0,3})(?: (.*))?"
)

# The gliff-server options a client may pass. --listen and --allow-remote open
# an unauthenticated TCP server, and --instance and --render-node reach past
# the running session, so anything not listed here is refused.
SERVER_FLAGS = {
    "--stdio": None,
    "--headless": None,
    "--low-bandwidth": None,
    "--full-chroma": None,
    "--output": re.compile(r"[A-Za-z0-9._-]{1,64}"),
    "--bitrate": re.compile(r"[0-9]{1,10}"),
    "--video": re.compile(r"gpu|cpu"),
    "--capture": re.compile(r"auto|dmabuf|shm"),
}


class Fail(Exception):
    pass


# ---------------------------------------------------------------- plumbing

def log(msg):
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        fd = os.open(LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


@contextlib.contextmanager
def locked():
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / "lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def write_private(path, text):
    """Atomic write with mode 600, through a symlink if path is one."""
    target = path.resolve() if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".gliff-door.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)


def run(cmd, timeout=4, **kw):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
    except (OSError, subprocess.TimeoutExpired):
        return None


def session_env():
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={env['XDG_RUNTIME_DIR']}/bus")
    return env


def notify(title, body, urgent=False):
    """Fire and forget. The gate's stdout is Gliff's video stream, so the
    child gets none of the gate's stdio."""
    cmd = ["notify-send", "-a", "Gliff Door", "-i", "gliff"]
    if urgent:
        cmd += ["-u", "critical"]
    try:
        subprocess.Popen(cmd + [title, body], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, env=session_env(), start_new_session=True)
    except OSError:
        pass


# ------------------------------------------------------------------- peers

def load_peers():
    try:
        data = json.loads(CONFIG.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise Fail(f"Can't read {CONFIG}: {e}")
    peers = {}
    for name, p in (data.get("peers") or {}).items():
        if not NAME_RE.fullmatch(name) or not isinstance(p, dict):
            continue
        try:
            addr = str(ipaddress.ip_address(p.get("address", "")))
        except ValueError:
            continue
        key = p.get("key")
        if key is not None and not (isinstance(key, str) and KEY_RE.fullmatch(key)):
            key = None
        peers[name] = {"address": addr, "key": key}
    return peers


def save_peers(peers):
    write_private(CONFIG, json.dumps({"peers": peers}, indent=2) + "\n")


def get_peer(peers, name):
    if name not in peers:
        raise Fail(f"Unknown machine '{name}'. Known: {', '.join(peers) or 'none yet'}.")
    return peers[name]


def check_name(name):
    if not NAME_RE.fullmatch(name):
        raise Fail("A machine name is letters, digits, '.', '_' and '-', up to 32 characters.")


# --------------------------------------------------------- authorized_keys

def read_auth():
    try:
        return AUTH.read_text().splitlines()
    except FileNotFoundError:
        return []


def tagged(line, name):
    return line.rstrip().endswith(" " + TAG + name)


def is_allowed(name, lines=None):
    return any(tagged(l, name) for l in (read_auth() if lines is None else lines))


def auth_line(name, peer):
    if re.search(r'["\\\s]', str(SCRIPT)):
        raise Fail(f"The plugin path {SCRIPT} has a space or quote in it; move it somewhere plain.")
    cmd = f"/usr/bin/python3 -I {SCRIPT} gate {name}"
    return f'restrict,from="{peer["address"]}",command="{cmd}" {peer["key"]} {TAG}{name}'


def write_auth(lines):
    (HOME / ".ssh").mkdir(mode=0o700, exist_ok=True)
    os.chmod(HOME / ".ssh", 0o700)
    write_private(AUTH, "".join(l + "\n" for l in lines))


# -------------------------------------------------------------------- keys

def parse_key(text):
    """One public key out of text, normalised to `type base64`."""
    if "PRIVATE KEY" in text:
        raise Fail("That's a private key. Use the .pub file.")
    lines = [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]
    if len(lines) != 1:
        raise Fail("Expected exactly one public key line." if lines else "No key found.")
    m = KEY_RE.fullmatch(lines[0])
    if not m:
        raise Fail("That doesn't look like an SSH public key (ssh-ed25519 AAAA…).")
    key = f"{m.group(1)} {m.group(2)}"
    if fingerprint(key) is None:
        raise Fail("ssh-keygen rejects that key.")
    return key, (m.group(3) or "").strip()


def fingerprint(key):
    r = run(["ssh-keygen", "-lf", "-"], input=key + "\n")
    if not r or r.returncode != 0:
        return None
    parts = r.stdout.split()
    return parts[1] if len(parts) > 1 else None


def fingerprints_with_full_access():
    """This PC's own keys and every untagged authorized key: a machine using
    one of these already gets a full shell, so a switch couldn't keep it out."""
    out = {}
    for pub in sorted((HOME / ".ssh").glob("*.pub")):
        with contextlib.suppress(OSError, Fail):
            key, _ = parse_key(pub.read_text())
            out[fingerprint(key)] = f"this PC's own key ({pub.name})"
    for line in read_auth():
        if TAG in line or not line.strip() or line.lstrip().startswith("#"):
            continue
        m = KEY_RE.search(line)
        if m:
            out.setdefault(fingerprint(f"{m.group(1)} {m.group(2)}"), "a key that already has full SSH access here")
    out.pop(None, None)
    return out


def downloads_candidate():
    """The newest .pub in ~/Downloads, where Taildrop drops files."""
    try:
        pubs = sorted((p for p in DOWNLOADS.glob("*.pub") if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for p in pubs[:1]:
        try:
            if p.stat().st_size > 16384:
                return None
            key, comment = parse_key(p.read_text())
        except (OSError, UnicodeDecodeError, Fail):
            return None
        return {"path": str(p), "file": p.name, "fingerprint": fingerprint(key), "comment": comment[:80]}
    return None


# ---------------------------------------------------------------- sessions

def _boot_time():
    with open("/proc/stat") as f:
        for line in f:
            if line.startswith("btime "):
                return int(line.split()[1])
    return 0


def sessions():
    uid, out = os.getuid(), []
    tick, boot = os.sysconf("SC_CLK_TCK"), _boot_time()
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        p = f"/proc/{d}"
        try:
            if os.stat(p).st_uid != uid:
                continue
            with open(p + "/comm") as f:
                if f.read().strip() != "gliff-server":
                    continue
            with open(p + "/environ", "rb") as f:
                env = dict(kv.split(b"=", 1) for kv in f.read().split(b"\0") if b"=" in kv)
            with open(p + "/cmdline", "rb") as f:
                argv = [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
            with open(p + "/stat") as f:
                start = int(f.read().rsplit(")", 1)[1].split()[19])
        except (OSError, ValueError, IndexError):
            continue
        output = next((argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--output"), None)
        output = output or next((a.split("=", 1)[1] for a in argv if a.startswith("--output=")), None)
        mode = "own screen" if "--headless" in argv else f"mirroring {output}" if output and output != "auto" else "mirroring"
        conn = env.get(b"SSH_CONNECTION", b"").decode(errors="replace").split()
        out.append({
            "pid": int(d),
            "peer": env.get(PEER_ENV.encode(), b"").decode(errors="replace") or None,
            "address": conn[0] if conn else None,
            "mode": mode,
            "since": boot + start // tick,
        })
    return sorted(out, key=lambda s: s["since"])


def kick_pids(pids):
    for pid in pids:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGTERM)
    deadline = time.time() + 2
    while pids and time.time() < deadline:
        time.sleep(0.1)
        pids = [p for p in pids if os.path.exists(f"/proc/{p}")]
    for pid in pids:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)


# ------------------------------------------------------------------ tailnet

def tailscale_status():
    r = run(["tailscale", "status", "--json"])
    try:
        return json.loads(r.stdout) if r and r.returncode == 0 else {}
    except ValueError:
        return {}


def tailnet():
    data = tailscale_status()
    online = {}
    for peer in (data.get("Peer") or {}).values():
        for ip in peer.get("TailscaleIPs") or []:
            online[ip] = bool(peer.get("Online"))
    own = [ip for ip in ((data.get("Self") or {}).get("TailscaleIPs") or []) if ":" not in ip]
    return (own[0] if own else None), online


def resolve_address(where):
    """An IP as given, or a tailnet device's IPv4 looked up by its name."""
    with contextlib.suppress(ValueError):
        return str(ipaddress.ip_address(where))
    want = where.lower().rstrip(".")
    for peer in (tailscale_status().get("Peer") or {}).values():
        names = {str(peer.get("HostName", "")).lower(), str(peer.get("DNSName", "")).lower().rstrip(".")}
        names.add(str(peer.get("DNSName", "")).lower().split(".")[0])
        if want in names:
            v4 = [ip for ip in peer.get("TailscaleIPs") or [] if ":" not in ip]
            if v4:
                return v4[0]
    raise Fail(f"No device called '{where}' on your tailnet. Give its Tailscale IP instead.")


# ------------------------------------------------------------------ status

def status():
    peers = load_peers()
    lines = read_auth()
    host, online = tailnet()
    sess = sessions()
    sshd = run(["systemctl", "is-active", "sshd.service"])
    out_peers = []
    for name, p in peers.items():
        out_peers.append({
            "name": name,
            "address": p["address"],
            "hasKey": bool(p["key"]),
            "fingerprint": fingerprint(p["key"]) if p["key"] else None,
            "allowed": is_allowed(name, lines),
            "online": online.get(p["address"], False),
            "sessions": sum(1 for s in sess if s["peer"] == name),
        })
    return {
        "host": host,
        "user": getpass.getuser(),
        "sshd": (sshd.stdout.strip() if sshd else "") or "unknown",
        "gliff": os.access(SERVER, os.X_OK),
        "peers": out_peers,
        "sessions": sess,
        "downloadsKey": downloads_candidate() if any(not p["key"] for p in peers.values()) else None,
    }


# ----------------------------------------------------------------- actions

def add(name, where):
    check_name(name)
    address = resolve_address(where)
    with locked():
        peers = load_peers()
        peer = peers.setdefault(name, {"address": address, "key": None})
        peer["address"] = address
        save_peers(peers)
        lines = read_auth()
        if peer["key"] and is_allowed(name, lines):
            write_auth([auth_line(name, peer) if tagged(l, name) else l for l in lines])
    log(f"{name} added at {address}")
    return f"Added {name} at {address}. Next, send its public key here."


def remove(name):
    with locked():
        peers = load_peers()
        get_peer(peers, name)
        write_auth([l for l in read_auth() if not tagged(l, name)])
        del peers[name]
        save_peers(peers)
    victims = [s["pid"] for s in sessions() if s["peer"] == name]
    kick_pids(victims)
    log(f"{name} removed")
    return f"Forgot {name}" + (" and ended its session." if victims else ".")


def allow(name, mode):
    if mode not in ("on", "off", "toggle"):
        raise Fail("Say on, off or toggle.")
    with locked():
        peers = load_peers()
        peer = get_peer(peers, name)
        lines = read_auth()
        want = (not is_allowed(name, lines)) if mode == "toggle" else mode == "on"
        if want and not peer["key"]:
            raise Fail(f"There's no key for {name} yet. Send its public key here first.")
        kept = [l for l in lines if not tagged(l, name)]
        if want:
            kept.append(auth_line(name, peer))
        write_auth(kept)
    log(f"{name} switched {'on' if want else 'off'}")
    if want:
        sshd = run(["systemctl", "is-active", "sshd.service"])
        if not sshd or sshd.stdout.strip() != "active":
            return f"{name} is let in, but the SSH server is off, so it can't connect yet."
        return f"{name} can connect with Gliff now."
    victims = [s["pid"] for s in sessions() if s["peer"] == name]
    kick_pids(victims)
    if victims:
        log(f"{name} cut off ({len(victims)} session(s))")
    return f"{name} is shut out" + (" and its session was ended." if victims else ".")


def set_key(name, source):
    if source == "--downloads":
        cand = downloads_candidate()
        if not cand:
            raise Fail("No .pub file in ~/Downloads.")
        text = Path(cand["path"]).read_text()
    elif source == "-":
        text = sys.stdin.read(16384)
    elif os.path.isfile(os.path.expanduser(source)):
        text = Path(os.path.expanduser(source)).read_text()
    else:
        text = source
    key, comment = parse_key(text)
    fp = fingerprint(key)
    clash = fingerprints_with_full_access().get(fp)
    if clash:
        raise Fail(f"That's {clash}, not a separate key for {name}.")
    with locked():
        peers = load_peers()
        get_peer(peers, name)["key"] = key
        save_peers(peers)
        lines = read_auth()
        if is_allowed(name, lines):
            write_auth([auth_line(name, peers[name]) if tagged(l, name) else l for l in lines])
    log(f"{name} key set to {fp} {comment}")
    return f"Saved {name}'s key {fp}" + (f" ({comment})" if comment else "") + "."


def kick(target):
    sess = sessions()
    if target.isdigit():
        victims = [s for s in sess if s["pid"] == int(target)]
    else:
        victims = [s for s in sess if s["peer"] == target]
    if not victims:
        raise Fail("No such Gliff session.")
    kick_pids([s["pid"] for s in victims])
    who = ", ".join(sorted({s["peer"] or s["address"] or "someone" for s in victims}))
    log(f"disconnected {who}")
    return f"Disconnected {who}."


def copy(what):
    """Put a setup command on the clipboard, through wl-copy's stdin."""
    host = tailnet()[0] or "<this-pc>"
    texts = {
        "connect": f"gliff {getpass.getuser()}@{host}",
        "key": f"omarchy tailscale send {host} ~/.ssh/id_ed25519.pub",
        "add": f"python3 {str(SCRIPT).replace(str(HOME), '~', 1)} add <name> <tailscale-name>",
    }
    if what not in texts:
        raise Fail("Say connect, key or add.")
    try:
        r = subprocess.run(["wl-copy"], input=texts[what], text=True, timeout=4,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        r = None
    if not r or r.returncode != 0:
        raise Fail("wl-copy failed.")
    return f"Copied: {texts[what]}"


# -------------------------------------------------------------------- gate

def gate(name):
    """sshd's forced command for a machine's key: run gliff-server, or refuse."""
    orig = os.environ.get("SSH_ORIGINAL_COMMAND")
    src = (os.environ.get("SSH_CONNECTION") or "?").split()[0]

    def refuse(why):
        log(f"refused {name} from {src}: {why} [{orig!r}]")
        notify(f"Refused {name}", why, urgent=True)
        sys.stderr.write(f"gliff-door: {why}\n")
        sys.exit(1)

    if not NAME_RE.fullmatch(name) or not is_allowed(name):
        refuse("its switch is off")
    if orig is None:
        refuse("this key only opens Gliff; connect with gliff, not ssh")
    try:
        argv = shlex.split(orig)
    except ValueError:
        refuse("unreadable command")
    if not argv or argv[0] not in ("gliff-server", SERVER):
        refuse("this key only runs gliff-server")

    args, clean, i = argv[1:], [], 0
    while i < len(args):
        flag, eq, val = args[i].partition("=")
        if flag not in SERVER_FLAGS:
            refuse(f"gliff-server option {flag[:40]} isn't allowed")
        pattern = SERVER_FLAGS[flag]
        if pattern is None:
            if eq:
                refuse(f"{flag} takes no value")
            clean.append(flag)
        else:
            if not eq:
                i += 1
                if i >= len(args):
                    refuse(f"{flag} needs a value")
                val = args[i]
            if not pattern.fullmatch(val):
                refuse(f"bad value for {flag}")
            clean += [flag, val]
        i += 1
    if "--stdio" not in clean:
        refuse("gliff-server must run with --stdio")

    mode = "with a screen of its own" if "--headless" in clean else "mirroring your screen"
    log(f"connect {name} from {src}: {' '.join(clean)}")
    notify(f"{name} connected", f"Remote desktop over Gliff, {mode}.")
    env = session_env()
    env[PEER_ENV] = name
    os.execve(SERVER, ["gliff-server"] + clean, env)


# -------------------------------------------------------------------- main

def main(argv):
    cmd = argv[0] if argv else "status"
    if cmd == "gate":
        gate(argv[1] if len(argv) > 1 else "")
        return 1
    try:
        if cmd == "status":
            print(json.dumps(status()))
            return 0
        if cmd == "add" and len(argv) == 3:
            msg = add(argv[1], argv[2])
        elif cmd == "remove" and len(argv) == 2:
            msg = remove(argv[1])
        elif cmd == "allow" and len(argv) == 3:
            msg = allow(argv[1], argv[2])
        elif cmd == "key" and len(argv) == 3:
            msg = set_key(argv[1], argv[2])
        elif cmd == "kick" and len(argv) == 2:
            msg = kick(argv[1])
        elif cmd == "copy" and len(argv) == 2:
            msg = copy(argv[1])
        else:
            raise Fail(__doc__.split("\n\n")[2].strip())
    except Fail as e:
        print(json.dumps({"ok": False, "message": str(e)}))
        return 1
    print(json.dumps({"ok": True, "message": msg}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
