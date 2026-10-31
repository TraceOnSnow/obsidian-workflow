#!/usr/bin/env python3
"""Observe/route new managed inbox notes. No model requests in this release.

First `run --apply` baselines current identities. Later runs observe new inbox
IDs twice, at least 60 seconds apart. status/plan never save observations.
Uncertain titles and links needing repair are queued instead of guessed.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from markdown_library.core import LibraryError
from markdown_library.inbox import organize


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("status", "plan", "run"), default="status")
    parser.add_argument("--vault", required=True)
    parser.add_argument("--max-new", type=int, default=4)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.apply and args.command != "run":
        parser.error("Only run accepts --apply")
    try:
        result = organize(args.vault, apply=args.apply, max_new=args.max_new)
    except (LibraryError, OSError, ValueError):
        print(json.dumps({"applied": False, "reason": "inbox_operation_blocked_check_private_library_state"}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
