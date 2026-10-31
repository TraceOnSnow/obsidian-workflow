#!/usr/bin/env python3
"""Verify and preview a pinned offline Super Table installation; apply requires token."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PIN = ROOT / "assets/super-table/pin.json"
SKILL = ROOT / "assets/super-table/copilot/SKILL.md"


class InstallError(ValueError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_path(path):
    """Do not resolve symlinks away before checking them."""
    path = Path(os.path.abspath(path))
    for component in [*reversed(path.parents), path]:
        if component.is_symlink():
            raise InstallError(f"Symlink not allowed: {component}")
        if component != path and component.exists() and not component.is_dir():
            raise InstallError(f"Not a directory: {component}")
    return path


def snapshot(path, limit=2_000_000):
    path = safe_path(path)
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise InstallError(f"Not a regular file: {path}")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise InstallError(f"Not a regular file: {path}")
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise InstallError(f"File exceeds size limit: {path}")
    return content


def install(vault, bundle_dir, *, enable=False, with_skill=False, apply=False, expected_token=None):
    vault = safe_path(vault)
    if not vault.is_dir():
        raise InstallError("Vault must be an existing directory")
    bundle_dir = safe_path(bundle_dir)
    pin_bytes = PIN.read_bytes()
    pin = json.loads(pin_bytes)
    desired = {}
    source_hashes = {}
    for name, metadata in pin["assets"].items():
        data = snapshot(bundle_dir / name)
        if data is None or len(data) != metadata["size"] or digest(data) != metadata["sha256"]:
            raise InstallError(f"Pinned release checksum mismatch: {name}")
        source_hashes[name] = digest(data)
        desired[f".obsidian/plugins/super-table/{name}"] = data
    manifest = json.loads(desired[".obsidian/plugins/super-table/manifest.json"])
    if (manifest.get("id"), manifest.get("version"), manifest.get("minAppVersion")) != (
        pin["plugin_id"], pin["tag"], pin["min_app_version"]
    ):
        raise InstallError("Pinned manifest identity mismatch")
    if with_skill:
        desired["copilot/skills/super-table/SKILL.md"] = SKILL.read_bytes()
    baseline = {}
    for relative, content in desired.items():
        previous = snapshot(vault / relative)
        if previous is not None and previous != content:
            raise InstallError(f"Existing different file retained: {relative}")
        baseline[relative] = previous
    community_relative = ".obsidian/community-plugins.json"
    if enable:
        previous = snapshot(vault / community_relative)
        try:
            plugins = json.loads(previous) if previous is not None else []
        except (ValueError, UnicodeError) as error:
            raise InstallError("Invalid community-plugins.json retained") from error
        if not isinstance(plugins, list) or any(not isinstance(item, str) for item in plugins):
            raise InstallError("community-plugins.json must be a string array")
        baseline[community_relative] = previous
        desired[community_relative] = previous if "super-table" in plugins else (
            json.dumps([*plugins, "super-table"], ensure_ascii=False, indent=2) + "\n"
        ).encode()
    states = {relative: digest(data) if data is not None else None for relative, data in baseline.items()}
    plan = {"vault": str(vault), "pin_sha256": digest(pin_bytes), "release": pin["tag"],
            "commit": pin["commit"], "enable": enable, "with_skill": with_skill,
            "before": states, "after": {key: digest(data) for key, data in desired.items()}}
    token = digest(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode())
    changes = [key for key, content in desired.items() if baseline[key] != content]
    if apply:
        if expected_token != token:
            raise InstallError("Preview token changed or absent; read a fresh preview before apply")
        for relative, previous in baseline.items():
            if snapshot(vault / relative) != previous:
                raise InstallError(f"File changed since preview: {relative}")
        for relative in changes:
            target = safe_path(vault / relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            safe_path(target)
            if snapshot(target) != baseline[relative]:
                raise InstallError(f"Concurrent change retained: {relative}")
            if baseline[relative] is None:
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(desired[relative])
                    stream.flush()
                    os.fsync(stream.fileno())
            else:
                # The only replaceable existing file is the community plugin list.
                fd, temporary = tempfile.mkstemp(prefix=".super-table-install-", dir=target.parent)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(desired[relative])
                        stream.flush()
                        os.fsync(stream.fileno())
                    safe_path(target)
                    if snapshot(target) != baseline[relative]:
                        raise InstallError(f"Concurrent change retained: {relative}")
                    os.replace(temporary, target)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
        for relative, content in desired.items():
            if snapshot(vault / relative) != content:
                raise InstallError(f"Installed file did not read back: {relative}")
    return {"applied": apply, "token": token, "release": pin["tag"], "commit": pin["commit"],
            "source_sha256": source_hashes, "changes": changes, "enabled": enable,
            "user_device_verified": False, "model_calls": 0, "website_writes": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True)
    parser.add_argument("--bundle-dir", required=True, help="Directory containing the three pinned release assets")
    parser.add_argument("--enable", action="store_true")
    parser.add_argument("--with-skill", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-token")
    args = parser.parse_args()
    try:
        print(json.dumps(install(args.vault, args.bundle_dir, enable=args.enable, with_skill=args.with_skill,
                                 apply=args.apply, expected_token=args.expected_token), ensure_ascii=False))
    except (InstallError, OSError) as error:
        parser.exit(2, f"{error}\n")


if __name__ == "__main__":
    main()
