#!/usr/bin/env python3
"""Check/reconcile maintained instructions, templates and their declared dependencies."""
import argparse
import json
from pathlib import Path
import sys

from markdown_library.core import Library, LibraryError
from markdown_library.maintenance import maintain


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault', required=True)
    parser.add_argument('--manifest', default='config/library-maintenance.json')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--expected-token')
    parser.add_argument('--accept-reviewed', action='store_true')
    args = parser.parse_args()
    try:
        result = maintain(Library(args.vault), project=Path(__file__).resolve().parents[1],
                          manifest=args.manifest, apply=args.apply, expected_token=args.expected_token,
                          accept_reviewed=args.accept_reviewed)
    except (LibraryError, ValueError, KeyError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['current'] else 1


if __name__ == '__main__':
    sys.exit(main())
