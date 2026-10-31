#!/usr/bin/env python3
"""Move website-only Work properties into private identity metadata; preview first."""
import argparse
import json

from markdown_library.core import Library, LibraryError
from markdown_library.work_profile import compact_works


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault', required=True)
    parser.add_argument('--seed-steam-end', action='store_true')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--expected-token')
    args = parser.parse_args()
    try:
        print(json.dumps(compact_works(Library(args.vault), apply=args.apply,
            expected_token=args.expected_token, seed_steam=args.seed_steam_end), ensure_ascii=False))
    except LibraryError as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
