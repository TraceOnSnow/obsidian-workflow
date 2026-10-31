#!/usr/bin/env python3
"""Preview/install the local writing Skill without overwriting an existing one."""

import argparse
import json
import os
from pathlib import Path

from markdown_library.core import Library, LibraryError


def install(library, *, apply=False):
    library.validate()
    template = Path(__file__).resolve().parents[1] / "assets/copilot/personal-library"
    files = []
    sources = [(template / relative, "copilot/skills/personal-library/" + relative)
               for relative in ["SKILL.md", "references/fields.md"]]
    sources.append((template.parent / "库内助手使用.md", "Copilot库内助手.md"))
    for source, relative in sources:
        content = source.read_bytes()
        target = library._path(relative)
        if target.exists():
            if target.read_bytes() != content:
                raise LibraryError("Existing personal-library Skill retained; review changes before updating")
            continue
        files.append((target, content))
    if apply:
        for target, content in files:
            target.parent.mkdir(parents=True, exist_ok=True)
            library._check_components(target.parent)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
    return {"applied": apply, "created": [str(target.relative_to(library.vault)) for target, _ in files],
            "backend": "opencode", "backend_verified_on_user_device": False, "model_calls": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(install(Library(args.vault), apply=args.apply), ensure_ascii=False))


if __name__ == "__main__":
    main()
