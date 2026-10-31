#!/usr/bin/env python3
"""Prepare or explicitly execute an isolated official Sync pull-only readback.

Authentication and encryption configuration are copied only inside the original
private Headless configuration tree, never into the project or downloaded Vault.
Default invocation inspects configuration only and never starts a sync client.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import stat
import subprocess
import tempfile
from datetime import datetime, timezone


class ReadbackError(Exception):
    pass


def safe_absolute(value):
    path = Path(value).expanduser()
    if not path.is_absolute() or path == Path("/") or ".." in path.parts:
        raise ReadbackError("Expected a safe explicit absolute directory")
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ReadbackError("Symlinks are not supported for readback paths")
    return path


def private_file(path, content):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def configuration(config_root, vault):
    root, vault = safe_absolute(config_root), safe_absolute(vault)
    if root.stat().st_uid != os.getuid() or stat.S_IMODE(root.stat().st_mode) & 0o077:
        raise ReadbackError("Original Headless configuration directory must be private and owned by this user")
    auth = safe_absolute(root / "auth_token")
    if not auth.is_file() or stat.S_IMODE(auth.stat().st_mode) & 0o077:
        raise ReadbackError("Original authentication file must be private")
    found = []
    for path in (root / "sync").glob("*/config.json"):
        safe_absolute(path)
        config = json.loads(path.read_bytes())
        if config.get("vaultPath") == str(vault):
            found.append((path, config))
    if len(found) != 1:
        raise ReadbackError("Exactly one original connection must match the source Vault")
    path, config = found[0]
    if (not isinstance(config.get("vaultId"), str) or config["vaultId"] != path.parent.name
            or not all(config.get(key) for key in ("encryptionKey", "encryptionSalt", "host"))
            or not isinstance(config.get("encryptionVersion"), int)):
        raise ReadbackError("Original connection has incomplete or inconsistent encrypted setup")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ReadbackError("Original encrypted configuration must be private")
    for key in ("allowTypes", "allowSpecialFiles", "ignoreFolders"):
        if key in config and (not isinstance(config[key], list) or any(not isinstance(value, str) for value in config[key])):
            raise ReadbackError("Unsupported source filter configuration")
    return path, config, auth


def syncable(relative, config):
    """Official 0.0.14 filter, with nested hidden paths excluded by traversal."""
    parts = Path(relative).parts
    config_dir = config.get("configDir", ".obsidian")
    if any(relative == folder or relative.startswith(folder + "/") for folder in config.get("ignoreFolders", [])):
        return False
    if parts and parts[0] == config_dir:
        rest = parts[1:]
        if not rest or any(part.startswith(".") or part == "node_modules" for part in rest):
            return False
        name, category = rest[-1], None
        if name in {"workspace.json", "workspace-mobile.json"} and len(rest) == 1:
            return False
        if len(rest) == 1:
            category = {"app.json": "app", "types.json": "app", "appearance.json": "appearance",
                        "hotkeys.json": "hotkey", "core-plugins.json": "core-plugin",
                        "core-plugins-migration.json": "core-plugin",
                        "community-plugins.json": "community-plugin"}.get(name)
            if category is None and name.endswith(".json"):
                category = "core-plugin-data"
        elif rest[0] == "themes" and len(rest) == 3 and name in {"theme.css", "manifest.json"}:
            category = "appearance-data"
        elif rest[0] == "snippets" and len(rest) == 2 and name.endswith(".css"):
            category = "appearance-data"
        elif rest[0] == "plugins" and len(rest) == 3 and name in {"main.js", "manifest.json", "styles.css", "data.json"}:
            category = "community-plugin-data"
        return category is not None and category in config.get("allowSpecialFiles", [])
    if any(part.startswith(".") for part in parts):
        return False
    extension = Path(relative).suffix.lower().removeprefix(".")
    if extension in {"md", "canvas", "base"}:
        return True
    allow = config.get("allowTypes", ["image", "audio", "pdf", "video"])
    groups = {"image": {"bmp", "png", "jpg", "jpeg", "gif", "svg", "webp", "avif"},
              "audio": {"mp3", "wav", "m4a", "3gp", "flac", "ogg", "oga", "opus"},
              "video": {"mp4", "ogv", "mov", "mkv"}, "pdf": {"pdf"}}
    if extension == "webm":
        return "audio" in allow or "video" in allow
    for group, extensions in groups.items():
        if extension in extensions:
            return group in allow
    return "unsupported" in allow


def manifest(vault, config):
    result = {}
    config_dir = config.get("configDir", ".obsidian")
    for directory, names, files in os.walk(vault, followlinks=False):
        for name in list(names):
            path = Path(directory) / name
            relative = path.relative_to(vault)
            if path.is_symlink():
                raise ReadbackError("Symlink directory in verification Vault")
            if ((name.startswith(".") and relative.parts != (config_dir,))
                    or name == "node_modules"
                    or any(relative.as_posix() == ignored or relative.as_posix().startswith(ignored + "/")
                           for ignored in config.get("ignoreFolders", []))):
                names.remove(name)
        for name in files:
            path = Path(directory) / name
            relative = path.relative_to(vault).as_posix()
            if not syncable(relative, config):
                continue
            if path.is_symlink():
                raise ReadbackError("Symlink file in verification Vault")
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def verify_readback(vault, output, config_root, command, *, timeout=900, runner=subprocess.run):
    vault, output, config_root = map(safe_absolute, (vault, output, config_root))
    if (output == vault or output.is_relative_to(vault) or vault.is_relative_to(output)
            or output.is_relative_to(config_root) or config_root.is_relative_to(output)):
        raise ReadbackError("Readback output must be independent of source Vault and authentication tree")
    config_path, original, auth_path = configuration(config_root, vault)
    config_bytes, auth_bytes = config_path.read_bytes(), auth_path.read_bytes()
    expected = manifest(vault, original)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir(mode=0o700)
    downloaded = output / "vault"
    downloaded.mkdir(mode=0o700)
    temporary = None
    report = {"success": False, "expected_files": len(expected), "downloaded_files": 0,
              "fully_synced": False, "pending": None, "initial_complete": False,
              "missing": [], "extra": [], "different": [], "source_changed": [],
              "original_auth_config_unchanged": False, "temporary_auth_removed": False}
    try:
        temporary = Path(tempfile.mkdtemp(prefix=".final-readback-xdg-", dir=config_root))
        temporary.chmod(0o700)
        app = temporary / "obsidian-headless"
        remote = app / "sync" / original["vaultId"]
        remote.mkdir(parents=True, mode=0o700)
        for directory in (app, app / "sync", remote): directory.chmod(0o700)
        private_file(app / "auth_token", auth_bytes)
        copied = {**original, "vaultPath": str(downloaded), "syncMode": "pull-only"}
        private_file(remote / "config.json", json.dumps(copied, ensure_ascii=False).encode())
        # Never clone the original index. A missing state.db forces independent
        # remote enumeration and download through the official client.
        if (remote / "state.db").exists():
            raise ReadbackError("Independent readback must start without an index")
        env = os.environ.copy()
        env.pop("OBSIDIAN_AUTH_TOKEN", None)
        env["XDG_CONFIG_HOME"] = str(temporary)
        completed = runner([*command, "sync", "--path", str(downloaded)], env=env,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=timeout, check=False)
        report["fully_synced"] = completed.returncode == 0 and any(
            line.strip().endswith("Fully synced") for line in completed.stdout.splitlines())
        if completed.returncode != 0:
            report["failure"] = "official-client-nonzero-exit"
        state_db = remote / "state.db"
        if state_db.is_file():
            with sqlite3.connect(state_db.as_uri() + "?mode=ro", uri=True) as db:
                report["pending"] = db.execute("SELECT count(*) FROM pending_files").fetchone()[0]
                row = db.execute("SELECT value FROM meta WHERE key='initial'").fetchone()
                report["initial_complete"] = row is not None and row[0] == "false"
        actual = manifest(downloaded, original)
        after = manifest(vault, original)
        report.update({"downloaded_files": len(actual),
                       "missing": sorted(expected.keys() - actual.keys()), "extra": sorted(actual.keys() - expected.keys()),
                       "different": sorted(key for key in expected.keys() & actual.keys() if expected[key] != actual[key]),
                       "source_changed": sorted(key for key in expected.keys() | after.keys() if expected.get(key) != after.get(key))})
    except BaseException as exc:
        # Never persist subprocess output, exception bodies, environment,
        # encrypted connection config, auth token, or encryption material.
        report["failure"] = "verification-interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "verification-failed"
    finally:
        if temporary is not None:
            try:
                shutil.rmtree(temporary)
            except OSError:
                report["failure"] = "temporary-private-config-cleanup-failed"
        report["temporary_auth_removed"] = temporary is None or not temporary.exists()
        report["original_auth_config_unchanged"] = config_path.read_bytes() == config_bytes and auth_path.read_bytes() == auth_bytes
        report["success"] = (report["fully_synced"] and report["pending"] == 0 and report["initial_complete"]
                             and report["temporary_auth_removed"] and report["original_auth_config_unchanged"]
                             and not any(report[key] for key in ("missing", "extra", "different", "source_changed"))
                             and "failure" not in report)
        private_file(output / "report.json", json.dumps(report, ensure_ascii=False, indent=2).encode())
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    project = Path(__file__).resolve().parents[1]
    parser.add_argument("--vault", required=True)
    parser.add_argument("--config-root", default=str(Path.home() / ".config/obsidian-headless"))
    parser.add_argument("--output", help="New independent private runtime directory")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--execute", action="store_true", help="Actually run official pull-only readback; use only after source is stable")
    args = parser.parse_args()
    os.umask(0o077)
    def interrupt(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupt)
    try:
        _, config, _ = configuration(args.config_root, args.vault)
        if not args.execute:
            print(json.dumps({"mode": "prepared-only", "started": False, "temporary_config_created": False,
                              "source_mode": config.get("syncMode", "bidirectional"), "readback_mode": "pull-only",
                              "fresh_index_required": True, "credentials_will_remain_in_private_config_tree": True}))
            return 0
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        output = args.output or str(project / "runtime/vault-merge" / ("final-readback-" + stamp))
        result = verify_readback(args.vault, output, args.config_root, [str(project / "scripts/obsidian-sync")], timeout=args.timeout)
        print(json.dumps({key: (len(value) if isinstance(value, list) else value)
                          for key, value in result.items()}))
        return 0 if result["success"] else 2
    except (ReadbackError, OSError, ValueError):
        print(json.dumps({"success": False, "failure": "readback-precondition-rejected"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
