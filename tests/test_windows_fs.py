from __future__ import annotations

import unittest
from pathlib import Path, PureWindowsPath

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
        self.open_calls: list[tuple[str, int, int, int, int]] = []
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
            self.add_file(path, (1, len(self.nodes) + 100))
        if key not in self.nodes:
            raise FileNotFoundError(path)
        handle = self.next_handle
        self.next_handle += 1
        self.handles[handle] = key
        return handle

    def close(self, handle: int) -> None:
        self.handles.pop(handle, None)

    def identity(self, handle: int) -> tuple[int, int]:
        return self.nodes[self.handles[handle]]["identity"]  # type: ignore[return-value]

    def attributes(self, handle: int) -> tuple[bool, bool]:
        node = self.nodes[self.handles[handle]]
        return bool(node["directory"]), bool(node["reparse"])

    def link_count(self, handle: int) -> int:
        return int(self.nodes[self.handles[handle]]["links"])

    def final_path(self, handle: int) -> str:
        return str(self.nodes[self.handles[handle]]["path"])

    def list_directory(self, path: str) -> list[str]:
        parent = PureWindowsPath(path)
        return sorted(
            PureWindowsPath(str(node["path"])).name
            for node in self.nodes.values()
            if PureWindowsPath(str(node["path"])).parent == parent
        )

    def rename(self, handle: int, destination: str) -> None:
        old_key = self.handles[handle]
        node = self.nodes.pop(old_key)
        old_path = PureWindowsPath(str(node["path"]))
        destination_path = PureWindowsPath(destination)
        node["path"] = str(destination_path)
        new_key = self.canonical(destination_path)
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

    def dispose(self, handle: int) -> None:
        key = self.handles[handle]
        node = self.nodes[key]
        if bool(node["directory"]) and self.list_directory(str(node["path"])):
            raise OSError("directory not empty")
        self.nodes.pop(key)

    def read(self, handle: int, maximum: int) -> bytes:
        return bytes(self.nodes[self.handles[handle]]["content"])[:maximum]

    def write(self, handle: int, payload: bytes) -> None:
        self.nodes[self.handles[handle]]["content"] = payload

    def flush(self, handle: int) -> None:
        return None


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

    def test_unmarked_nonempty_root_is_not_adopted(self) -> None:
        self.api.add_file(self.root + r"\foreign.txt", (7, 75), b"foreign")

        with self.assertRaises(WindowsFilesystemError):
            self.fs.initialize_marker(self.root, "marker.json", b"{}", 512)

        self.assertEqual(self.api.nodes[self.api.canonical(self.root + r"\foreign.txt")]["content"], b"foreign")

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

        self.assertIn(self.api.canonical(victim + r"\manifest.json"), self.api.nodes)

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
