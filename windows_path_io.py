"""Windows long-path helpers for read-only MinerU source files."""

from __future__ import annotations

import os


def io_path(path: os.PathLike[str] | str, *, windows: bool | None = None) -> str:
    """Return an extended-length path for absolute Windows paths near MAX_PATH.

    The original path should still be stored in provenance and reports. Use
    this value only at the filesystem call boundary.
    """
    value = os.fspath(path)
    use_windows = os.name == "nt" if windows is None else windows
    if not use_windows or value.startswith("\\\\?\\") or not os.path.isabs(value):
        return value
    absolute = os.path.abspath(value)
    if len(absolute) < 240:
        return value
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def open_path(path: os.PathLike[str] | str, mode: str = "r", **kwargs):
    return open(io_path(path), mode, **kwargs)


def is_file(path: os.PathLike[str] | str) -> bool:
    return os.path.isfile(io_path(path))


def stat_path(path: os.PathLike[str] | str):
    return os.stat(io_path(path))


def read_bytes(path: os.PathLike[str] | str) -> bytes:
    with open_path(path, "rb") as stream:
        return stream.read()
