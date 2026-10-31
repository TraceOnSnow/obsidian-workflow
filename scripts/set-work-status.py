#!/usr/bin/env python3
"""Bulk-set catalog status, preserve unrelated data, and optionally set its default."""
import argparse
from copy import deepcopy
import json

from markdown_library.bootstrap import _index
from markdown_library.core import Library, LibraryError, _hash, _read
from markdown_library.sync import content_hash
from markdown_library.work_catalog import build_catalog, parse_catalog


def update(library, status, *, set_default=False, overwrite_existing=False, apply=False, expected_token=None):
    if status not in {'planned', 'in-progress', 'completed', 'on-hold', 'dropped'}:
        raise LibraryError('Unknown Work status')
    with library._lock():
        state = library._load()
        state, notes = _index(library)
        config = state['work_catalog']
        path = config['path']
        original = _read(library._path(path))
        catalog = parse_catalog(original)
        before = deepcopy(catalog.document)
        changed = 0
        for row in catalog.document['records']:
            if row['id'] in notes and (overwrite_existing or row['values']['status'] in {None, ''}):
                changed += row['values']['status'] != status
                row['values']['status'] = status
        # This is also the repeatable checker: every cell/layout except status
        # must compare equal before anything is written.
        check = deepcopy(catalog.document)
        for old, new in zip(before['records'], check['records']):
            new['values']['status'] = old['values']['status']
        if check != before:
            raise LibraryError('Bulk status operation changed unrelated table data')
        token = content_hash({'catalog': _hash(original), 'default': config.get('default_status'),
                              'status': status, 'set_default': set_default, 'overwrite_existing': overwrite_existing})
        if apply:
            if expected_token != token:
                raise LibraryError('Preview changed; read a fresh preview')
            for note in notes.values():
                if note.get('catalog_backed'):
                    library._remember(state, note, 'bulk-work-status-original')
            if set_default:
                config['default_status'] = status
            library._commit(state, [(path, build_catalog(catalog.document['records'], catalog), _hash(original))],
                            storage_resolved=True)
            saved = parse_catalog(_read(library._path(path))).document
            if saved != catalog.document:
                raise LibraryError('Table changed after write; review current data')
        return {'applied': apply, 'works': len(catalog.document['records']), 'changed': changed,
                'status': status, 'set_default': set_default, 'other_cells_and_views_preserved': True,
                'token': token}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault', required=True)
    parser.add_argument('--status', default='completed')
    parser.add_argument('--set-default', action='store_true')
    parser.add_argument('--overwrite-existing', action='store_true', help='Explicitly replace existing nonempty statuses')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--expected-token')
    args = parser.parse_args()
    print(json.dumps(update(Library(args.vault), args.status, set_default=args.set_default,
                            overwrite_existing=args.overwrite_existing,
                            apply=args.apply, expected_token=args.expected_token), ensure_ascii=False))


if __name__ == '__main__':
    main()
