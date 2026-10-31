#!/usr/bin/env python3
"""Render the Vault's publication inventory from a private sync-review receipt."""

import argparse
import json
from pathlib import Path

from markdown_library.core import Library
from markdown_library.overview import publication_overview


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True)
    parser.add_argument("--review", required=True)
    parser.add_argument("--observed-at")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    review = json.loads(Path(args.review).read_text(encoding="utf-8"))
    result = publication_overview(Library(args.vault), review, observed_at=args.observed_at, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
