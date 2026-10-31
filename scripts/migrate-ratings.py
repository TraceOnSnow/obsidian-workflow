#!/usr/bin/env python3
"""Preview or apply the local legacy 10-point to 100-point rating migration.

Coordinate website scale migration and pause external writers before apply.
No website calls, new publication selections, or conflict resolutions occur.
"""
import argparse
import json

from markdown_library.core import Library, LibraryError
from markdown_library.ratings import migrate_ratings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault', required=True)
    parser.add_argument('--source-scale', type=int, choices=(10, 100), default=10)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--expected-token')
    args = parser.parse_args()
    try:
        result = migrate_ratings(Library(args.vault), source_scale=args.source_scale,
                                 apply=args.apply, expected_token=args.expected_token)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    except (LibraryError, ValueError, OSError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
