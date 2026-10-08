"""Read another process's EXEC-TIME environment -- the OS mechanism dev.finding
a0166920 names, not an inference from os.environ.

On linux this is /proc/<pid>/environ. On darwin it is sysctl KERN_PROCARGS2, read
through ctypes: a first sysctl call sizes the buffer, a second fills it, and the
buffer is argc (native int) + the exec path (NUL-terminated, then NUL-padded) +
argv (each NUL-terminated) + envp (each NUL-terminated). `/bin/ps` is deliberately
NOT used as the darwin route: ps is setuid root, and the worker's sandbox profile
refuses to exec it (rc 71), so a ps-based test would silently skip everywhere the
factory actually runs it. If the sysctl call itself is refused, `read_exec_environment`
returns None so the caller can skip with a reason instead of asserting on nothing.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import struct
import sys
from pathlib import Path

CTL_KERN = 1
KERN_PROCARGS2 = 49


def _sysctl_procargs2(pid: int) -> bytes | None:
    library_path = ctypes.util.find_library("c")
    if library_path is None:
        return None
    libc = ctypes.CDLL(library_path, use_errno=True)
    mib = (ctypes.c_int * 3)(CTL_KERN, KERN_PROCARGS2, pid)
    size = ctypes.c_size_t(0)
    if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0:
        return None
    if size.value == 0:
        return None
    buf = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
        return None
    return buf.raw[: size.value]


def _parse_procargs2(data: bytes) -> tuple[list[bytes], list[bytes]]:
    if len(data) < 4:
        return [], []
    (argc,) = struct.unpack_from("i", data, 0)
    offset = 4
    end = data.find(b"\x00", offset)
    if end == -1:
        return [], []
    offset = end
    while offset < len(data) and data[offset] == 0:
        offset += 1

    argv: list[bytes] = []
    for _ in range(argc):
        end = data.find(b"\x00", offset)
        if end == -1:
            return argv, []
        argv.append(data[offset:end])
        offset = end + 1

    envp: list[bytes] = []
    while offset < len(data):
        end = data.find(b"\x00", offset)
        if end == -1:
            break
        chunk = data[offset:end]
        offset = end + 1
        if not chunk:
            break
        envp.append(chunk)
    return argv, envp


def _read_darwin(pid: int) -> list[str] | None:
    data = _sysctl_procargs2(pid)
    if data is None:
        return None
    _argv, envp = _parse_procargs2(data)
    return [entry.decode("utf-8", "surrogateescape") for entry in envp]


def _read_linux(pid: int) -> list[str] | None:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    return [
        chunk.decode("utf-8", "surrogateescape")
        for chunk in raw.split(b"\x00")
        if chunk
    ]


def read_exec_environment(pid: int) -> list[str] | None:
    """The process's exec-time environment as ["NAME=VALUE", ...], or None if unreadable."""
    if sys.platform == "darwin":
        return _read_darwin(pid)
    if sys.platform.startswith("linux"):
        return _read_linux(pid)
    return None
