#!/usr/bin/env python3
"""Private launcher for the official tunnel-client (no tunnel CRUD)."""
from __future__ import annotations

import argparse
import fcntl
import getpass
import http.client
import json
import os
from pathlib import Path
import re
import select
import shlex
import signal
import socket
import subprocess
import sys
import time

PROJECT = Path(__file__).resolve().parents[1]
RUNTIME = PROJECT / "runtime" / "library-tunnel"
BINARY = PROJECT / "tools" / "tunnel-client" / "tunnel-client"
VAULT = Path("/home/trace/projects/personal-records-vault")
PROFILE = "markdown-library"


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def private_dir(root):
    if root.is_symlink():
        raise ValueError("runtime directory must not be a symlink")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)


def write_json(target, value):
    if target.is_symlink():
        raise ValueError("runtime file must not be a symlink")
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream)
    target.chmod(0o600)


def clean_env():
    # Do not let ambient tunnel flags enable raw logging, alternate commands or ingress.
    allow = {"HOME", "PATH", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE",
             "SSL_CERT_DIR", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
             "https_proxy", "http_proxy", "no_proxy"}
    return {name: value for name, value in os.environ.items() if name in allow}


def secret_env(key_env):
    value = os.environ.get(key_env)
    if not value:
        if not sys.stdin.isatty():
            raise ValueError("runtime key absent: set the configured environment variable or run in a terminal")
        value = getpass.getpass("Tunnel runtime API key (not saved): ")
    if not value or any(char.isspace() for char in value):
        raise ValueError("runtime key is empty or contains whitespace")
    env = clean_env()
    env["CONTROL_PLANE_API_KEY"] = value
    return env


def configure(args, root=RUNTIME, binary=BINARY):
    tunnel_id = args.tunnel_id or input("Tunnel ID: ").strip()
    if not re.fullmatch(r"tunnel_(?:[a-z0-9]{4}_)?[a-z0-9]{32}", tunnel_id):
        raise ValueError("invalid tunnel ID")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.key_env):
        raise ValueError("invalid environment variable name")
    private_dir(root)
    profiles = root / "profiles"
    private_dir(profiles)
    if (root / "settings.json").exists() or any(profiles.iterdir()):
        raise ValueError("configuration exists; preserve it and review manually before changing tunnel identity")
    # Strip the runtime key before launching the stdio child; it needs no API key.
    command = shlex.join(["/usr/bin/env", "-u", "CONTROL_PLANE_API_KEY",
                          "-u", "OPENAI_API_KEY", str(PROJECT / ".venv/bin/python"),
                          "-m", "markdown_library.mcp_server", "--vault", str(VAULT)])
    result = subprocess.run([str(binary), "init", "--sample", "sample_mcp_stdio_local",
                             "--profile", PROFILE, "--profile-dir", str(profiles),
                             "--tunnel-id", tunnel_id, "--mcp-command", command,
                             "--control-plane-api-key-ref", "env:CONTROL_PLANE_API_KEY",
                             "--health-listen-addr", "127.0.0.1:0"],
                            env=clean_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode:
        raise ValueError("official profile initialization failed; see tunnel-client init --help")
    for path in profiles.iterdir():
        if path.is_file():
            path.chmod(0o600)
    write_json(root / "settings.json", {"key_env": args.key_env, "tunnel_id": tunnel_id})
    emit({"configured": True, "runtime": str(root), "key_saved": False,
          "next": "doctor, then start"})


def load_settings(root):
    value = json.loads((root / "settings.json").read_text())
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value["key_env"]):
        raise ValueError("invalid key environment reference")
    return value


def command(action, root=RUNTIME, binary=BINARY):
    return [str(binary), action, "--profile", PROFILE, "--profile-dir", str(root / "profiles"),
            "--health.unix-socket", str(root / "admin.sock"),
            "--log.http-raw-unsafe=false", "--tracing.enabled=false",
            "--allow-remote-ui=false", "--open-web-ui=false",
            "--mcp.stdio-send-initialized-notification=true"]


def process_start(pid):
    # comm may contain spaces and parentheses; starttime is stat field 22.
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, target):
        super().__init__("localhost", timeout=2)
        self.target = target

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self.target))


def probe(root, endpoint):
    connection = UnixHTTP(root / "admin.sock")
    try:
        connection.request("GET", endpoint)
        return connection.getresponse().status == 200
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def verified_pid(root):
    try:
        state = json.loads((root / "process.json").read_text())
        pid = int(state["pid"])
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        if (process_start(pid) == state["start"] and
                str(root / "profiles").encode() in cmdline and b"run" in cmdline and
                os.getpgid(pid) == pid):
            return pid
    except (OSError, ValueError, KeyError):
        pass
    return None


def status(root=RUNTIME):
    alive = verified_pid(root) is not None
    return {"configured": (root / "settings.json").is_file(), "process_running": alive,
            "healthy": alive and probe(root, "/healthz"),
            "ready": alive and probe(root, "/readyz"),
            "chatgpt_verified": False}


def doctor(root=RUNTIME, binary=BINARY):
    settings = load_settings(root)
    result = subprocess.run(command("doctor", root, binary) + ["--explain", "--json"],
                            env=secret_env(settings["key_env"]), stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=45)
    # Suppress upstream diagnostics rather than risk logging literal credentials.
    emit({"doctor_passed": result.returncode == 0, "exit_code": result.returncode,
          "chatgpt_verified": False,
          "next": "start" if result.returncode == 0 else "check profile, key permissions and official doctor --help"})
    return result.returncode


def run(root=RUNTIME, binary=BINARY, env=None, started_fd=None):
    settings = load_settings(root)
    private_dir(root)
    with (root / "run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("a foreground tunnel launcher is already running") from None
        if env is None:
            env = secret_env(settings["key_env"])
        child = subprocess.Popen(command("run", root, binary), env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True, cwd=PROJECT)
        try:
            write_json(root / "process.json", {"pid": child.pid, "start": process_start(child.pid)})
            if started_fd is not None:
                os.write(started_fd, b"started")
                os.close(started_fd)
            emit({"started": True, "foreground": True, "pid": child.pid,
                  "next": "use status in another terminal; Ctrl+C stops this tunnel"})
            previous = {}
            for signum in (signal.SIGTERM, signal.SIGINT):
                previous[signum] = signal.signal(signum, lambda sig, frame: os.killpg(child.pid, sig))
            try:
                return child.wait()
            finally:
                for signum, handler in previous.items():
                    signal.signal(signum, handler)
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
            (root / "process.json").unlink(missing_ok=True)


def start(root=RUNTIME, binary=BINARY):
    if verified_pid(root) is not None:
        emit({"already_running": True, **status(root)})
        return 0
    settings = load_settings(root)
    # Prompt before detaching. Only an in-memory copy crosses fork.
    env = secret_env(settings["key_env"])
    read_fd, write_fd = os.pipe()
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            os.setsid()
            with open(os.devnull, "r+b", buffering=0) as null:
                for fd in (0, 1, 2):
                    os.dup2(null.fileno(), fd)
            try:
                code = run(root, binary, env=env, started_fd=write_fd)
            except BaseException:
                try:
                    os.write(write_fd, b"failed")
                except OSError:
                    pass
                code = 1
            os._exit(0 if code == 0 else 1)
        except BaseException:
            os._exit(1)
    os.close(write_fd)
    try:
        readable, _, _ = select.select([read_fd], [], [], 10)
        result = os.read(read_fd, 64) if readable else b"pending"
    finally:
        os.close(read_fd)
    if result != b"started":
        emit({"started": False, "pending": result == b"pending",
              "next": "check status; another launcher may already hold the lock"})
        return 1
    emit({"started": True, "background": True, **status(root),
          "next": "check status for ready; terminal can now close"})
    return 0


def stop(root=RUNTIME):
    pid = verified_pid(root)
    if pid is None:
        emit({"stopped": True, "already_stopped": True})
        return 0
    # Revalidate immediately before signalling the specific client process group.
    if verified_pid(root) != pid:
        raise ValueError("process identity changed")
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 10
    while verified_pid(root) == pid and time.monotonic() < deadline:
        time.sleep(0.05)
    stopped = verified_pid(root) != pid
    emit({"stopped": stopped, "next": "status"})
    return 0 if stopped else 1


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    setup = sub.add_parser("configure", help="create local profile only; no remote tunnel creation")
    setup.add_argument("--tunnel-id", help="existing Platform tunnel ID; prompted if omitted")
    setup.add_argument("--key-env", default="CONTROL_PLANE_API_KEY", help="runtime-key environment reference")
    for name in ("doctor", "start", "stop", "run", "status"):
        sub.add_parser(name)
    args = parser.parse_args()
    try:
        if args.action == "configure":
            configure(args)
        elif args.action == "status":
            emit(status())
        elif args.action == "doctor":
            return doctor()
        elif args.action == "start":
            return start()
        elif args.action == "stop":
            return stop()
        else:
            return run()
    except (OSError, ValueError, subprocess.TimeoutExpired, EOFError):
        # Never include exception text: subprocess exceptions can include argv/env.
        emit({"error": "Operation failed. Check binary, local configuration, runtime key and permissions; no credential was printed."})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
