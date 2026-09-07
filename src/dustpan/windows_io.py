"""Windows no-reparse traversal for local disk paths.

Each ancestor is held without write/delete sharing until the child is open.
The final file handle also denies write/delete sharing and is transferred to
the CRT in binary mode. No path is reopened after descriptor validation.
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes


class _Info(ctypes.Structure):
    _fields_ = [
        ("attributes", wintypes.DWORD),
        ("creation", wintypes.FILETIME),
        ("access", wintypes.FILETIME),
        ("write", wintypes.FILETIME),
        ("volume", wintypes.DWORD),
        ("size_high", wintypes.DWORD),
        ("size_low", wintypes.DWORD),
        ("links", wintypes.DWORD),
        ("index_high", wintypes.DWORD),
        ("index_low", wintypes.DWORD),
    ]


def open_guarded(path: str) -> int:
    """Return a binary descriptor, refusing all reparse and non-disk paths."""
    if sys.platform != "win32":
        raise OSError("Windows guarded reader requires Windows")
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    info = kernel.GetFileInformationByHandle
    info.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Info)]
    info.restype = wintypes.BOOL
    absolute = os.path.abspath(path)
    drive, tail = os.path.splitdrive(absolute)
    if len(drive) != 2 or drive[1] != ":" or ":" in tail:
        raise OSError("guarded reads require a local disk path without alternate streams")
    parts = [p for p in tail.split(os.sep) if p]
    current = drive + os.sep
    paths = [current]
    for part in parts:
        current = os.path.join(current, part)
        paths.append(current)
    handles: list[int] = []
    try:
        for index, current in enumerate(paths):
            final = index == len(paths) - 1
            # GENERIC_READ for bytes; FILE_READ_ATTRIBUTES for ancestors.
            handle = create(
                current,
                0x80000000 if final else 0x80,
                1,
                None,
                3,
                0x00200000 | 0x02000000,
                None,
            )
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            handles.append(handle)
            details = _Info()
            if not info(handle, ctypes.byref(details)):
                raise ctypes.WinError(ctypes.get_last_error())
            if details.attributes & 0x400:
                raise OSError(f"refused reparse point: {current}")
            if not final and not details.attributes & 0x10:
                raise OSError(f"expected directory: {current}")
            if final and details.attributes & 0x10:
                raise OSError(f"expected regular file: {current}")
        descriptor = msvcrt.open_osfhandle(handles[-1], os.O_RDONLY | os.O_BINARY)
        handles.pop()  # CRT owns the final handle; os.close closes it.
        return descriptor
    finally:
        for handle in reversed(handles):
            close(handle)
