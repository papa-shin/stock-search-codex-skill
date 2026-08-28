from __future__ import annotations

import hashlib
import time
import unittest
import sys
from pathlib import Path, PureWindowsPath


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from papa_shin_stock._windows_fs import (
    CREATE_NEW,
    DELETE,
    FILE_FLAG_BACKUP_SEMANTICS,
    FILE_FLAG_OPEN_REPARSE_POINT,
    FILE_SHARE_DELETE,
    FILE_SHARE_READ,
    FILE_SHARE_WRITE,
    GENERIC_READ,
    GENERIC_WRITE,
    WindowsFilesystem,
    WindowsFilesystemError,
    WindowsIdentity,
)


class FakeWindowsApi:
    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, object]] = {}
        self.handles: dict[int, str] = {}
        self.offsets: dict[int, int] = {}
        self.close_calls: list[int] = []
        self.close_failure_handles: set[int] = set()
        self.open_calls: list[tuple[str, int, int, int, int]] = []
        self.child_open_calls: list[tuple[int, str, int, int, int, int]] = []
        self.relative_rename_calls: list[tuple[int, int, str, bool]] = []
        self.relative_rename_failures_after_side_effect_remaining = 0
        self.directory_list_calls: list[int] = []
        self.flush_calls: list[int] = []
        self.flush_failure_handles: set[int] = set()
        self.defer_disposition_until_close = False
        self.pending_disposition_handles: set[int] = set()
        self.next_handle = 10

    @staticmethod
    def canonical(path: str | Path) -> str:
        return str(PureWindowsPath(path)).casefold()

    def add_directory(
        self,
        path: str,
        identity: tuple[int, int] = (1, 1),
        *,
        reparse: bool = False,
    ) -> None:
        self.nodes[self.canonical(path)] = {
            "path": str(PureWindowsPath(path)),
            "directory": True,
            "identity": identity,
            "reparse": reparse,
            "links": 1,
            "content": b"",
            "mtime": time.time(),
        }

    def add_file(
        self,
        path: str,
        identity: tuple[int, int],
        content: bytes = b"",
        *,
        reparse: bool = False,
        links: int = 1,
    ) -> None:
        self.nodes[self.canonical(path)] = {
            "path": str(PureWindowsPath(path)),
            "directory": False,
            "identity": identity,
            "reparse": reparse,
            "links": links,
            "content": content,
            "mtime": time.time(),
        }

    def create_file(
        self,
        path: str,
        access: int,
        share: int,
        creation: int,
        flags: int,
    ) -> int:
        key = self.canonical(path)
        self.open_calls.append((path, access, share, creation, flags))
        if creation == CREATE_NEW:
            if key in self.nodes:
                raise FileExistsError(path)
            if flags & FILE_FLAG_BACKUP_SEMANTICS:
                self.add_directory(path, (1, len(self.nodes) + 100))
            else:
                self.add_file(path, (1, len(self.nodes) + 100))
        if key not in self.nodes:
            raise FileNotFoundError(path)
        handle = self.next_handle
        self.next_handle += 1
        self.handles[handle] = key
        self.offsets[handle] = 0
        return handle

    def close(self, handle: int) -> None:
        self.close_calls.append(handle)
        if handle in self.pending_disposition_handles:
            self.pending_disposition_handles.remove(handle)
            key = self.handles.get(handle)
            if key is not None:
                self.nodes.pop(key, None)
        self.handles.pop(handle, None)
        self.offsets.pop(handle, None)
        if handle in self.close_failure_handles:
            self.close_failure_handles.remove(handle)
            raise OSError("synthetic close failure")

    def create_child(
        self,
        parent_handle: int,
        name: str,
        access: int,
        share: int,
        creation: int,
        flags: int,
    ) -> int:
        self.child_open_calls.append(
            (parent_handle, name, access, share, creation, flags)
        )
        parent = self.nodes[self.handles[parent_handle]]
        if not bool(parent["directory"]):
            raise NotADirectoryError(name)
        return self.create_file(
            str(PureWindowsPath(str(parent["path"])) / name),
            access,
            share,
            creation,
            flags,
        )

    def identity(self, handle: int) -> tuple[int, int]:
        return self.nodes[self.handles[handle]]["identity"]  # type: ignore[return-value]

    def attributes(self, handle: int) -> tuple[bool, bool]:
        node = self.nodes[self.handles[handle]]
        return bool(node["directory"]), bool(node["reparse"])

    def link_count(self, handle: int) -> int:
        return int(self.nodes[self.handles[handle]]["links"])

    def size(self, handle: int) -> int:
        return len(bytes(self.nodes[self.handles[handle]]["content"]))

    def last_write_time(self, handle: int) -> float:
        return float(self.nodes[self.handles[handle]]["mtime"])

    def final_path(self, handle: int) -> str:
        return str(self.nodes[self.handles[handle]]["path"])

    def list_directory(self, path: str) -> list[str]:
        parent = PureWindowsPath(path)
        return sorted(
            PureWindowsPath(str(node["path"])).name
            for node in self.nodes.values()
            if PureWindowsPath(str(node["path"])).parent == parent
        )

    def list_directory_handle(self, handle: int) -> list[str]:
        self.directory_list_calls.append(handle)
        return self.list_directory(str(self.nodes[self.handles[handle]]["path"]))

    def rename(self, handle: int, destination: str) -> None:
        old_key = self.handles[handle]
        new_key = self.canonical(destination)
        if new_key in self.nodes and new_key != old_key:
            raise FileExistsError(destination)
        node = self.nodes.pop(old_key)
        old_path = PureWindowsPath(str(node["path"]))
        destination_path = PureWindowsPath(destination)
        node["path"] = str(destination_path)
        self.nodes[new_key] = node
        self.handles[handle] = new_key
        descendants = [
            (key, value)
            for key, value in self.nodes.items()
            if key != new_key
            and PureWindowsPath(str(value["path"])).is_relative_to(old_path)
        ]
        for key, value in descendants:
            self.nodes.pop(key)
            relative = PureWindowsPath(str(value["path"])).relative_to(old_path)
            value["path"] = str(destination_path / relative)
            replacement = self.canonical(str(value["path"]))
            self.nodes[replacement] = value
            for open_handle, handle_key in list(self.handles.items()):
                if handle_key == key:
                    self.handles[open_handle] = replacement

    def rename_relative(
        self,
        handle: int,
        parent_handle: int,
        destination_name: str,
        *,
        replace: bool,
    ) -> None:
        self.relative_rename_calls.append(
            (handle, parent_handle, destination_name, replace)
        )
        parent = self.nodes[self.handles[parent_handle]]
        destination = str(
            PureWindowsPath(str(parent["path"])) / destination_name
        )
        if replace:
            target_key = self.canonical(destination)
            if target_key in self.nodes and target_key != self.handles[handle]:
                self.nodes.pop(target_key)
        self.rename(handle, destination)
        if self.relative_rename_failures_after_side_effect_remaining > 0:
            self.relative_rename_failures_after_side_effect_remaining -= 1
            raise OSError("synthetic post-rename API failure")

    def dispose(self, handle: int) -> None:
        key = self.handles[handle]
        node = self.nodes[key]
        if bool(node["directory"]) and self.list_directory(str(node["path"])):
            raise OSError("directory not empty")
        if self.defer_disposition_until_close:
            self.pending_disposition_handles.add(handle)
            return
        self.nodes.pop(key)

    def read(self, handle: int, maximum: int) -> bytes:
        content = bytes(self.nodes[self.handles[handle]]["content"])
        offset = self.offsets[handle]
        result = content[offset : offset + maximum]
        self.offsets[handle] = offset + len(result)
        return result

    def write(self, handle: int, payload: bytes) -> None:
        node = self.nodes[self.handles[handle]]
        content = bytes(node["content"])
        offset = self.offsets[handle]
        node["content"] = content[:offset] + payload + content[offset + len(payload) :]
        self.offsets[handle] = offset + len(payload)
        node["mtime"] = time.time()

    def touch(self, handle: int) -> None:
        self.nodes[self.handles[handle]]["mtime"] = time.time()

    def flush(self, handle: int) -> None:
        self.flush_calls.append(handle)
        if handle in self.flush_failure_handles:
            self.flush_failure_handles.remove(handle)
            raise OSError("synthetic flush failure")


class WindowsFilesystemTest(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeWindowsApi()
        self.fs = WindowsFilesystem(self.api)
        self.root = r"C:\Users\manager\.cache\papa-shin-stock"
        self.api.add_directory(self.root, (7, 70))

    def test_destructive_open_pins_identity_and_denies_delete_sharing(self) -> None:
        victim = self.root + r"\victim.json"
        self.api.add_file(victim, (7, 71), b"{}")

        with self.fs.open_verified(
            victim,
            directory=False,
            destructive=True,
            expected=WindowsIdentity(7, 71),
        ):
            pass

        _, access, share, _, flags = self.api.open_calls[-1]
        self.assertEqual(access & DELETE, DELETE)
        self.assertEqual(share, FILE_SHARE_READ | FILE_SHARE_WRITE)
        self.assertEqual(share & FILE_SHARE_DELETE, 0)
        self.assertEqual(flags & FILE_FLAG_OPEN_REPARSE_POINT, FILE_FLAG_OPEN_REPARSE_POINT)

    def test_directory_open_uses_backup_semantics_and_rejects_reparse(self) -> None:
        junction = self.root + r"\junction"
        self.api.add_directory(junction, (7, 72), reparse=True)

        with self.assertRaises(WindowsFilesystemError):
            self.fs.open_verified(junction, directory=True)

        _, _, _, _, flags = self.api.open_calls[-1]
        self.assertEqual(flags & FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_BACKUP_SEMANTICS)
        self.assertEqual(flags & FILE_FLAG_OPEN_REPARSE_POINT, FILE_FLAG_OPEN_REPARSE_POINT)

    def test_regular_file_with_multiple_links_is_rejected(self) -> None:
        victim = self.root + r"\linked.json"
        self.api.add_file(victim, (7, 73), links=2)

        with self.assertRaises(WindowsFilesystemError):
            self.fs.open_verified(victim, directory=False)

    def test_final_path_replacement_is_rejected(self) -> None:
        victim = self.root + r"\victim.json"
        self.api.add_file(victim, (7, 74))
        self.api.nodes[self.api.canonical(victim)]["path"] = self.root + r"\other.json"

        with self.assertRaises(WindowsFilesystemError):
            self.fs.open_verified(victim, directory=False)

    def test_initialize_and_attest_marker(self) -> None:
        marker = ".papa-shin-stock-cache-root.json"
        payload = b'{"kind":"papa-shin-stock-cache-root","schema_version":1,"ownership_token":"0123456789abcdef0123456789abcdef"}'

        evidence = self.fs.initialize_marker(self.root, marker, payload, 512)
        confirmed = self.fs.attest_marker(self.root, marker, 512)

        self.assertEqual(evidence.root_identity, WindowsIdentity(7, 70))
        self.assertEqual(confirmed.marker_identity, evidence.marker_identity)
        self.assertEqual(confirmed.payload, payload)
        marker_call = next(call for call in self.api.open_calls if call[3] == CREATE_NEW)
        self.assertEqual(marker_call[1] & (GENERIC_READ | GENERIC_WRITE), GENERIC_READ | GENERIC_WRITE)
        self.assertEqual(marker_call[3], CREATE_NEW)
        self.assertGreaterEqual(
            [call[1] for call in self.api.child_open_calls].count(marker), 3
        )

    def test_unmarked_nonempty_root_is_not_adopted(self) -> None:
        self.api.add_file(self.root + r"\foreign.txt", (7, 75), b"foreign")

        with self.assertRaises(WindowsFilesystemError):
            self.fs.initialize_marker(self.root, "marker.json", b"{}", 512)

        self.assertEqual(self.api.nodes[self.api.canonical(self.root + r"\foreign.txt")]["content"], b"foreign")

    def test_marker_initialization_race_rolls_back_marker_without_adopting_foreign_root(
        self,
    ) -> None:
        marker = ".papa-shin-stock-cache-root.json"
        marker_path = self.root + "\\" + marker
        foreign = self.root + r"\generation-foreign"
        payload = b'{"kind":"papa-shin-stock-cache-root","schema_version":1,"ownership_token":"0123456789abcdef0123456789abcdef"}'
        original_list = self.api.list_directory_handle
        root_lists = 0

        def add_foreign_after_marker(handle: int) -> list[str]:
            nonlocal root_lists
            if self.api.handles[handle] == self.api.canonical(self.root):
                root_lists += 1
                if root_lists == 3:
                    self.api.add_directory(foreign, (7, 760))
            return original_list(handle)

        self.api.list_directory_handle = add_foreign_after_marker  # type: ignore[method-assign]
        with self.assertRaises(WindowsFilesystemError):
            self.fs.open_cache_root(
                self.root,
                marker,
                payload=payload,
                maximum=512,
                create=True,
            )

        self.assertNotIn(self.api.canonical(marker_path), self.api.nodes)
        self.assertIn(self.api.canonical(foreign), self.api.nodes)
        self.api.list_directory_handle = original_list  # type: ignore[method-assign]
        with self.assertRaises(WindowsFilesystemError):
            self.fs.open_cache_root(
                self.root,
                marker,
                payload=payload,
                maximum=512,
                create=True,
            )

    def test_cache_root_session_retains_handle_and_rechecks_marker_identity(
        self,
    ) -> None:
        marker = ".papa-shin-stock-cache-root.json"
        payload = b'{"kind":"papa-shin-stock-cache-root","schema_version":1,"ownership_token":"0123456789abcdef0123456789abcdef"}'

        session = self.fs.open_cache_root(
            self.root,
            marker,
            payload=payload,
            maximum=512,
            create=True,
        )
        self.addCleanup(session.close)
        session.assert_current()

        marker_key = self.api.canonical(self.root + "\\" + marker)
        self.api.nodes[marker_key]["identity"] = (7, 999)

        with self.assertRaises(WindowsFilesystemError):
            session.assert_current()
        self.assertEqual(self.api.nodes[marker_key]["content"], payload)

    def test_cache_root_close_attempts_root_after_marker_close_failure(self) -> None:
        marker = ".papa-shin-stock-cache-root.json"
        payload = b'{"kind":"papa-shin-stock-cache-root","schema_version":1,"ownership_token":"0123456789abcdef0123456789abcdef"}'
        session = self.fs.open_cache_root(
            self.root,
            marker,
            payload=payload,
            maximum=512,
            create=True,
        )
        marker_handle = session.marker.handle
        root_handle = session.root.handle
        self.api.close_failure_handles.add(marker_handle)

        with self.assertRaisesRegex(OSError, "synthetic close failure"):
            session.close()

        self.assertIn(marker_handle, self.api.close_calls)
        self.assertIn(root_handle, self.api.close_calls)

    def test_missing_cache_root_is_created_relative_to_existing_parent(self) -> None:
        parent = r"C:\Users\manager\.cache"
        root = parent + r"\new-cache"
        marker = ".papa-shin-stock-cache-root.json"
        payload = b'{"kind":"papa-shin-stock-cache-root","schema_version":1,"ownership_token":"abcdef0123456789abcdef0123456789"}'
        self.api.add_directory(parent, (7, 200))

        with self.fs.open_cache_root(
            root,
            marker,
            payload=payload,
            maximum=512,
            create=True,
        ) as session:
            session.assert_current()

        self.assertIn(self.api.canonical(root), self.api.nodes)
        self.assertIn(self.api.canonical(root + "\\" + marker), self.api.nodes)
        created_children = [
            call[1] for call in self.api.child_open_calls if call[4] == CREATE_NEW
        ]
        self.assertIn("new-cache", created_children)

    def test_relative_directory_create_accepts_native_already_exists_error(self) -> None:
        marker = ".papa-shin-stock-cache-root.json"
        payload = b'{"kind":"papa-shin-stock-cache-root","schema_version":1,"ownership_token":"abcdef0123456789abcdef0123456789"}'

        with self.fs.open_cache_root(
            self.root,
            marker,
            payload=payload,
            maximum=512,
            create=True,
        ) as session:
            original_create_child = self.api.create_child
            raced = False

            def create_child_with_race(
                parent_handle: int,
                name: str,
                access: int,
                share: int,
                creation: int,
                flags: int,
            ) -> int:
                nonlocal raced
                if name == "generations" and creation == CREATE_NEW and not raced:
                    raced = True
                    parent = self.api.nodes[self.api.handles[parent_handle]]
                    self.api.add_directory(
                        str(PureWindowsPath(str(parent["path"])) / name),
                        (7, 250),
                    )
                    raise WindowsFilesystemError(183, "already exists")
                return original_create_child(
                    parent_handle,
                    name,
                    access,
                    share,
                    creation,
                    flags,
                )

            self.api.create_child = create_child_with_race  # type: ignore[method-assign]
            identity = session.ensure_directory(("generations",))

        self.assertTrue(raced)
        self.assertEqual(identity, WindowsIdentity(7, 250))

    def test_cache_root_session_handles_nested_generation_lifecycle(self) -> None:
        marker = ".papa-shin-stock-cache-root.json"
        payload = b'{"kind":"papa-shin-stock-cache-root","schema_version":1,"ownership_token":"0123456789abcdef0123456789abcdef"}'

        with self.fs.open_cache_root(
            self.root,
            marker,
            payload=payload,
            maximum=512,
            create=True,
        ) as session:
            session.ensure_directory(("generations",))
            staging = session.create_directory(("generations",), ".staging-fixed")
            session.write_new_file(
                ("generations", ".staging-fixed"), "manifest.json", b"{}"
            )
            session.write_new_file(
                ("generations", ".staging-fixed"), "products.jsonl", b"row\n"
            )
            self.assertEqual(
                session.read_file(
                    ("generations", ".staging-fixed"), "manifest.json", 16
                ),
                b"{}",
            )
            session.verify_file(
                ("generations", ".staging-fixed"),
                "products.jsonl",
                expected_bytes=4,
                expected_sha256=hashlib.sha256(b"row\n").hexdigest(),
            )
            session.rename_directory(
                ("generations",),
                ".staging-fixed",
                staging,
                "generation-fixed",
            )
            self.assertEqual(
                session.list_directory(("generations",)), ["generation-fixed"]
            )
            session.delete_flat_directory(
                ("generations",),
                "generation-fixed",
                staging,
                quarantine_name=".generation-fixed.delete-fixed",
            )
            self.assertEqual(session.list_directory(("generations",)), [])

    def test_download_destination_streams_into_retained_parent_handle(self) -> None:
        marker = ".papa-shin-stock-cache-root.json"
        marker_payload = b'{"kind":"papa-shin-stock-cache-root","schema_version":1,"ownership_token":"0123456789abcdef0123456789abcdef"}'

        with self.fs.open_cache_root(
            self.root,
            marker,
            payload=marker_payload,
            maximum=512,
            create=True,
        ) as session:
            session.ensure_directory(("generations",))
            session.create_directory(("generations",), ".staging-download")
            destination = session.create_download_destination(
                ("generations", ".staging-download"), "products.jsonl"
            )
            try:
                with destination.open("wb") as output:
                    output.write(b"first-")
                    output.write(b"second")
                destination.fsync()
            finally:
                destination.close()

            self.assertEqual(
                session.read_file(
                    ("generations", ".staging-download"),
                    "products.jsonl",
                    64,
                ),
                b"first-second",
            )

    def test_session_touch_updates_only_expected_handle_bound_file(self) -> None:
        marker = ".papa-shin-stock-cache-root.json"
        marker_payload = b'{"kind":"papa-shin-stock-cache-root","schema_version":1,"ownership_token":"0123456789abcdef0123456789abcdef"}'

        with self.fs.open_cache_root(
            self.root,
            marker,
            payload=marker_payload,
            maximum=512,
            create=True,
        ) as session:
            session.create_directory((), ".refresh.lock")
            identity = session.write_new_file(
                (".refresh.lock",), "heartbeat-token", b""
            )
            heartbeat_key = self.api.canonical(
                self.root + r"\.refresh.lock\heartbeat-token"
            )
            self.api.nodes[heartbeat_key]["mtime"] = 100.0

            session.touch_file(
                (".refresh.lock",), "heartbeat-token", identity
            )

            self.assertGreater(
                session.last_write_time(
                    (".refresh.lock",),
                    "heartbeat-token",
                    directory=False,
                    expected=identity,
                ),
                100.0,
            )

    def test_session_snapshot_rejects_reparse_and_hardlinked_entries(self) -> None:
        marker = ".papa-shin-stock-cache-root.json"
        marker_payload = b'{"kind":"papa-shin-stock-cache-root","schema_version":1,"ownership_token":"0123456789abcdef0123456789abcdef"}'

        with self.fs.open_cache_root(
            self.root,
            marker,
            payload=marker_payload,
            maximum=512,
            create=True,
        ) as session:
            session.create_directory((), "hardlinked")
            self.api.add_file(
                self.root + r"\hardlinked\foreign.json",
                (7, 301),
                b"foreign",
                links=2,
            )
            with self.assertRaises(WindowsFilesystemError):
                session.snapshot_flat_directory(("hardlinked",))

            self.api.add_directory(
                self.root + r"\junction", (7, 302), reparse=True
            )
            with self.assertRaises(WindowsFilesystemError):
                session.directory_identity(("junction",))

    def test_flat_directory_cleanup_renames_by_handle_and_preserves_foreign_file(self) -> None:
        generations = self.root + r"\generations"
        victim = generations + r"\generation-old"
        foreign = generations + r"\foreign.txt"
        self.api.add_directory(generations, (7, 80))
        self.api.add_directory(victim, (7, 81))
        self.api.add_file(victim + r"\manifest.json", (7, 82), b"{}")
        self.api.add_file(victim + r"\products.jsonl", (7, 83), b"{}\n")
        self.api.add_file(foreign, (7, 84), b"safe")

        removed = self.fs.delete_flat_directory(
            generations,
            "generation-old",
            WindowsIdentity(7, 81),
            quarantine_name=".generation-old.delete-fixed",
        )

        self.assertTrue(removed)
        self.assertNotIn(self.api.canonical(victim), self.api.nodes)
        self.assertEqual(self.api.nodes[self.api.canonical(foreign)]["content"], b"safe")

    def test_cleanup_closes_delete_pending_children_before_directory_disposition(
        self,
    ) -> None:
        generations = self.root + r"\generations"
        victim = generations + r"\generation-old"
        self.api.add_directory(generations, (7, 180))
        self.api.add_directory(victim, (7, 181))
        self.api.add_file(victim + r"\manifest.json", (7, 182), b"{}")
        self.api.defer_disposition_until_close = True

        removed = self.fs.delete_flat_directory(
            generations,
            "generation-old",
            WindowsIdentity(7, 181),
            quarantine_name=".generation-old.delete-fixed",
        )

        self.assertTrue(removed)
        self.assertFalse(
            any(
                "generation-old" in name
                for name in self.api.list_directory(generations)
            )
        )

    def test_destructive_children_and_rename_are_bound_to_parent_handles(self) -> None:
        generations = self.root + r"\generations"
        victim = generations + r"\generation-old"
        self.api.add_directory(generations, (7, 85))
        self.api.add_directory(victim, (7, 86))
        self.api.add_file(victim + r"\manifest.json", (7, 87), b"{}")

        self.fs.delete_flat_directory(
            generations,
            "generation-old",
            WindowsIdentity(7, 86),
            quarantine_name=".generation-old.delete-fixed",
        )

        opened_names = [call[1] for call in self.api.child_open_calls]
        self.assertIn("generation-old", opened_names)
        self.assertIn("manifest.json", opened_names)
        self.assertEqual(
            [call[2:] for call in self.api.relative_rename_calls],
            [(".generation-old.delete-fixed", False)],
        )
        self.assertGreaterEqual(len(self.api.directory_list_calls), 3)

    def test_cleanup_fails_closed_when_child_is_replaced(self) -> None:
        generations = self.root + r"\generations"
        victim = generations + r"\generation-old"
        self.api.add_directory(generations, (7, 90))
        self.api.add_directory(victim, (7, 91))
        self.api.add_file(victim + r"\manifest.json", (7, 92), b"{}")

        original_list = self.api.list_directory
        calls = 0

        def replace_during_inventory(path: str) -> list[str]:
            nonlocal calls
            result = original_list(path)
            calls += 1
            if calls == 2:
                child = self.api.nodes[self.api.canonical(victim + r"\manifest.json")]
                child["identity"] = (7, 999)
            return result

        self.api.list_directory = replace_during_inventory  # type: ignore[method-assign]

        with self.assertRaises(WindowsFilesystemError):
            self.fs.delete_flat_directory(
                generations,
                "generation-old",
                WindowsIdentity(7, 91),
                quarantine_name=".generation-old.delete-fixed",
            )

        quarantine_child = generations + r"\.generation-old.delete-fixed\manifest.json"
        self.assertNotIn(self.api.canonical(victim), self.api.nodes)
        self.assertEqual(
            self.api.nodes[self.api.canonical(quarantine_child)]["identity"],
            (7, 999),
        )

    def test_cleanup_preserves_preexisting_foreign_quarantine_target(self) -> None:
        generations = self.root + r"\generations"
        victim = generations + r"\generation-old"
        quarantine = generations + r"\.generation-old.delete-fixed"
        self.api.add_directory(generations, (7, 100))
        self.api.add_directory(victim, (7, 101))
        self.api.add_file(victim + r"\manifest.json", (7, 102), b"owned")
        self.api.add_file(quarantine, (7, 103), b"foreign")

        with self.assertRaises(FileExistsError):
            self.fs.delete_flat_directory(
                generations,
                "generation-old",
                WindowsIdentity(7, 101),
                quarantine_name=".generation-old.delete-fixed",
            )

        self.assertEqual(
            self.api.nodes[self.api.canonical(quarantine)]["content"], b"foreign"
        )
        self.assertEqual(
            self.api.nodes[self.api.canonical(victim + r"\manifest.json")]["content"],
            b"owned",
        )

    def test_cleanup_payload_mismatch_preserves_quarantined_directory(self) -> None:
        lock = self.root + r"\.refresh.lock"
        quarantine = self.root + r"\.refresh.lock.release-token-fixed"
        owner = lock + r"\owner.json"
        heartbeat = lock + r"\heartbeat-token"
        self.api.add_directory(lock, (7, 104))
        self.api.add_file(owner, (7, 105), b"foreign-owner")
        self.api.add_file(heartbeat, (7, 106), b"")

        removed = self.fs.delete_flat_directory(
            self.root,
            ".refresh.lock",
            WindowsIdentity(7, 104),
            quarantine_name=".refresh.lock.release-token-fixed",
            expected_inventory={
                "owner.json": WindowsIdentity(7, 105),
                "heartbeat-token": WindowsIdentity(7, 106),
            },
            expected_payloads={
                "owner.json": b"expected-owner",
                "heartbeat-token": b"",
            },
        )

        self.assertFalse(removed)
        self.assertNotIn(self.api.canonical(lock), self.api.nodes)
        self.assertEqual(
            self.api.nodes[self.api.canonical(quarantine + r"\owner.json")]["content"],
            b"foreign-owner",
        )

    def test_cleanup_timestamp_mismatch_preserves_quarantined_directory(self) -> None:
        lock = self.root + r"\.refresh.lock"
        quarantine = self.root + r"\.refresh.lock.reclaim-token-fixed"
        heartbeat = lock + r"\heartbeat-token"
        self.api.add_directory(lock, (7, 107))
        self.api.add_file(heartbeat, (7, 108), b"")
        heartbeat_key = self.api.canonical(heartbeat)
        observed = float(self.api.nodes[heartbeat_key]["mtime"])
        self.api.nodes[heartbeat_key]["mtime"] = observed + 1.0

        removed = self.fs.delete_flat_directory(
            self.root,
            ".refresh.lock",
            WindowsIdentity(7, 107),
            quarantine_name=".refresh.lock.reclaim-token-fixed",
            expected_inventory={"heartbeat-token": WindowsIdentity(7, 108)},
            expected_payloads={"heartbeat-token": b""},
            expected_write_times={"heartbeat-token": observed},
        )

        self.assertFalse(removed)
        self.assertIn(self.api.canonical(quarantine + r"\heartbeat-token"), self.api.nodes)

    def test_atomic_replace_uses_payload_cas_relative_rename_and_parent_flush(
        self,
    ) -> None:
        pointer = self.root + r"\current.json"
        self.api.add_file(pointer, (7, 110), b"old-pointer")

        with self.fs.open_verified(
            self.root, directory=True, destructive=True, writable=True
        ) as root:
            self.fs.replace_file_cas(
                root,
                "current.json",
                expected=b"old-pointer",
                payload=b"new-pointer",
            )
            root_handle = root.handle

        self.assertEqual(
            self.api.nodes[self.api.canonical(pointer)]["content"], b"new-pointer"
        )
        self.assertIn(root_handle, self.api.flush_calls)
        self.assertEqual(
            [call[2:] for call in self.api.relative_rename_calls],
            [("current.json", True)],
        )

    def test_atomic_replace_cas_mismatch_preserves_foreign_target(self) -> None:
        pointer = self.root + r"\current.json"
        self.api.add_file(pointer, (7, 120), b"foreign-pointer")

        with self.fs.open_verified(
            self.root, directory=True, destructive=True, writable=True
        ) as root:
            with self.assertRaises(WindowsFilesystemError):
                self.fs.replace_file_cas(
                    root,
                    "current.json",
                    expected=b"expected-pointer",
                    payload=b"new-pointer",
                )

        self.assertEqual(
            self.api.nodes[self.api.canonical(pointer)]["content"],
            b"foreign-pointer",
        )
        self.assertFalse(
            any("current.json" in name and name != "current.json" for name in self.api.list_directory(self.root))
        )

    def test_atomic_replace_post_rename_flush_failure_preserves_published_target(
        self,
    ) -> None:
        pointer = self.root + r"\current.json"
        self.api.add_file(pointer, (7, 130), b"old-pointer")

        with self.fs.open_verified(
            self.root, directory=True, destructive=True, writable=True
        ) as root:
            self.api.flush_failure_handles.add(root.handle)
            with self.assertRaisesRegex(OSError, "synthetic flush failure"):
                self.fs.replace_file_cas(
                    root,
                    "current.json",
                    expected=b"old-pointer",
                    payload=b"new-pointer",
                )

        self.assertEqual(
            self.api.nodes[self.api.canonical(pointer)]["content"], b"new-pointer"
        )
        self.assertFalse(
            any(
                "current.json" in name and name != "current.json"
                for name in self.api.list_directory(self.root)
            )
        )

    def test_atomic_replace_post_rename_api_failure_preserves_published_target(
        self,
    ) -> None:
        pointer = self.root + r"\current.json"
        self.api.add_file(pointer, (7, 140), b"old-pointer")
        self.api.relative_rename_failures_after_side_effect_remaining = 1

        with self.fs.open_verified(
            self.root, directory=True, destructive=True, writable=True
        ) as root:
            with self.assertRaisesRegex(OSError, "synthetic post-rename API failure"):
                self.fs.replace_file_cas(
                    root,
                    "current.json",
                    expected=b"old-pointer",
                    payload=b"new-pointer",
                )

        self.assertEqual(
            self.api.nodes[self.api.canonical(pointer)]["content"], b"new-pointer"
        )
        self.assertFalse(
            any(
                "current.json" in name and name != "current.json"
                for name in self.api.list_directory(self.root)
            )
        )

    def test_two_sequential_generation_cleanups_leave_only_current(self) -> None:
        generations = self.root + r"\generations"
        self.api.add_directory(generations, (8, 1))
        for suffix, identity in (("first", (8, 2)), ("second", (8, 3))):
            directory = generations + rf"\generation-{suffix}"
            self.api.add_directory(directory, identity)
            self.api.add_file(directory + r"\manifest.json", (identity[0], identity[1] + 10))
        current = generations + r"\generation-current"
        self.api.add_directory(current, (8, 4))
        self.api.add_file(current + r"\manifest.json", (8, 14))

        for suffix, identity in (("first", (8, 2)), ("second", (8, 3))):
            self.assertTrue(
                self.fs.delete_flat_directory(
                    generations,
                    f"generation-{suffix}",
                    WindowsIdentity(*identity),
                    quarantine_name=f".generation-{suffix}.delete-fixed",
                )
            )

        self.assertEqual(self.api.list_directory(generations), ["generation-current"])

    def test_lock_release_cleanup_leaves_no_release_artifact(self) -> None:
        lock = self.root + r"\.refresh.lock.release-token-fixed"
        self.api.add_directory(lock, (9, 1))
        self.api.add_file(lock + r"\owner.json", (9, 2), b"{}")
        self.api.add_file(lock + r"\heartbeat-token", (9, 3))

        self.assertTrue(
            self.fs.delete_flat_directory(
                self.root,
                ".refresh.lock.release-token-fixed",
                WindowsIdentity(9, 1),
                quarantine_name="..refresh.lock.release-token-fixed.delete-fixed",
            )
        )

        self.assertFalse(
            any("refresh.lock.release" in name for name in self.api.list_directory(self.root))
        )

    def test_inventory_order_change_is_not_treated_as_namespace_race(self) -> None:
        generations = self.root + r"\generations"
        victim = generations + r"\generation-old"
        self.api.add_directory(generations, (10, 1))
        self.api.add_directory(victim, (10, 2))
        self.api.add_file(victim + r"\a.json", (10, 3))
        self.api.add_file(victim + r"\b.json", (10, 4))
        original_list = self.api.list_directory
        calls = 0

        def alternate_order(path: str) -> list[str]:
            nonlocal calls
            values = original_list(path)
            if PureWindowsPath(path).name == "generation-old":
                calls += 1
                if calls == 2:
                    return list(reversed(values))
            return values

        self.api.list_directory = alternate_order  # type: ignore[method-assign]

        self.assertTrue(
            self.fs.delete_flat_directory(
                generations,
                "generation-old",
                WindowsIdentity(10, 2),
                quarantine_name=".generation-old.delete-fixed",
            )
        )


if __name__ == "__main__":
    unittest.main()
