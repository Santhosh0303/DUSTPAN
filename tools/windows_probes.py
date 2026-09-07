"""Six Windows read-boundary checks, executed on real Windows runners."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from dustpan import safeio
from dustpan.ir import Estate


class WindowsReads(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name) / "root"
        self.root.mkdir()
        self.file = self.root / "data.txt"
        self.file.write_bytes(b"alpha\r\nbeta\x1az")

    def open(self) -> int:
        fd = safeio._open_contained(str(self.file), str(self.root))
        self.addCleanup(os.close, fd)
        return fd

    def test_physical_bytes_are_preserved(self) -> None:
        self.assertEqual(os.read(self.open(), 100), b"alpha\r\nbeta\x1az")

    def test_open_file_cannot_be_overwritten(self) -> None:
        self.open()
        with self.assertRaises(PermissionError):
            self.file.write_bytes(b"modified")

    def test_open_file_cannot_be_replaced(self) -> None:
        self.open()
        replacement = self.root / "replacement.txt"
        replacement.write_bytes(b"modified")
        with self.assertRaises(PermissionError):
            os.replace(replacement, self.file)

    def test_final_reparse_point_is_refused(self) -> None:
        link = self.root / "link.txt"
        link.symlink_to(self.file)
        with self.assertRaises(OSError):
            fd = safeio._open_contained(str(link), str(self.root))
            os.close(fd)

    def test_directory_reparse_point_is_refused(self) -> None:
        link = self.root / "linked"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            fd = safeio._open_contained(str(link / "data.txt"), str(self.root))
            os.close(fd)

    def test_guarded_read_is_not_silently_degraded(self) -> None:
        estate = Estate(scan_root=str(self.root))
        self.assertEqual(
            safeio.read_bytes(str(self.file), estate, "windows-probe"),
            b"alpha\r\nbeta\x1az",
        )
        self.assertEqual(estate.errors, [])


if __name__ == "__main__":
    if os.name != "nt":
        raise SystemExit("Run these checks on Windows, not an emulated platform")
    unittest.main(verbosity=2)
