from copy import deepcopy
import importlib.util
from pathlib import Path
import tempfile
import unittest

from markdown_library import Library
from markdown_library.transport import _Store
from test_imported_mapping import MappingPayload


spec = importlib.util.spec_from_file_location("reconcile_imported", Path(__file__).resolve().parents[1] / "scripts/reconcile-imported.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ReconcileImportedTests(unittest.TestCase):
    def test_preview_then_apply_batches_public_private_and_blocked_without_remote_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            library = Library.initialize(Path(temporary) / "vault")
            client = MappingPayload()
            client.seed("posts", 10, title="Public post", link="public", locale="zh", markdown="Public", _status="published")
            client.seed("posts", 11, title="Protected", link="protected", locale="zh", markdown="PRIVATE", protectedContent=True)
            client.seed("works", 20, title="Private Work", slug="private", type="book", visibility="private")
            snapshot = {"rating_scale": 100, "target": client.target,
                        **{name: client.list_collection(name) for name in ("posts", "works")},
                        "published": {name: client.list_collection(name, draft=False) for name in ("posts", "works")}}
            def files():
                return {str(path.relative_to(library.vault)): path.read_bytes()
                        for path in library.vault.rglob("*") if path.is_file()}
            before = files()
            preview = module.reconcile(library, client, snapshot)
            self.assertEqual(preview["counts"], {"plan-adopt-public": 1, "plan-map-imported-draft": 1, "blocked": 1})
            self.assertEqual(files(), before)
            result = module.reconcile(library, client, snapshot, apply=True)
            self.assertEqual(result["counts"], {"adopted": 1, "mapped-imported-draft": 1, "blocked": 1})
            self.assertEqual(client.mutations, [])
            self.assertEqual(len(library.list_notes()), 3)
            self.assertEqual(len(library.publications()), 1)
            state = _Store(library, client.target).load()
            self.assertEqual(sorted(row["state"] for row in state["baselines"].values()), ["imported-draft", "published"])
            again = module.reconcile(library, client, deepcopy(snapshot), apply=True)
            self.assertEqual(again["counts"], {"existing-baseline": 2, "blocked": 1})
            self.assertEqual(client.mutations, [])

    def test_changed_fresh_public_content_is_blocked_before_local_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            library = Library.initialize(Path(temporary) / "vault")
            client = MappingPayload()
            client.seed("posts", 10, title="Public post", link="public", locale="zh", markdown="Original", _status="published")
            snapshot = {"rating_scale": 100, "target": client.target,
                        **{name: client.list_collection(name) for name in ("posts", "works")},
                        "published": {name: client.list_collection(name, draft=False) for name in ("posts", "works")}}
            client.latest["posts"]["10"]["protectedContent"] = True
            result = module.reconcile(library, client, snapshot, apply=True)
            self.assertEqual(result["counts"], {"blocked": 1})
            self.assertEqual(library.publications(), [])
            self.assertEqual(_Store(library, client.target).load()["baselines"], {})
            self.assertEqual(client.mutations, [])

    def test_admin_race_after_virtual_preflight_leaves_no_provisional_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            library = Library.initialize(Path(temporary) / "vault")
            client = MappingPayload()
            client.seed("posts", 10, title="Public", link="public", locale="zh", markdown="Original", _status="published")
            snapshot = {"rating_scale": 100, "target": client.target,
                        **{name: client.list_collection(name) for name in ("posts", "works")},
                        "published": {name: client.list_collection(name, draft=False) for name in ("posts", "works")}}
            get, calls = client.get, []
            def racing_get(collection, remote_id, *, draft=True):
                calls.append(draft)
                if len(calls) == 3:
                    client.latest[collection][str(remote_id)]["protectedContent"] = True
                return get(collection, remote_id, draft=draft)
            client.get = racing_get
            result = module.reconcile(library, client, snapshot, apply=True)
            self.assertEqual(result["counts"], {"blocked": 1})
            self.assertEqual(library.publications(), [])
            self.assertEqual(_Store(library, client.target).load()["baselines"], {})
            self.assertEqual(client.mutations, [])
