#!/usr/bin/env python3
"""Preview or explicitly apply the guarded Work catalog migration."""
import argparse
import json
from markdown_library.core import Library
from markdown_library.catalog_migration import migrate_work_catalog

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--vault', required=True)
parser.add_argument('--apply', action='store_true')
parser.add_argument('--expected-token')
args = parser.parse_args()
print(json.dumps(migrate_work_catalog(Library(args.vault), apply=args.apply,
                                     expected_token=args.expected_token), ensure_ascii=False))
