"""Capture files: gzipped JSON Lines of directory probes, for offline use.

Line 1 is a CaptureHeader; each further line is one DirProbe, in tree order,
without children (every listed subdirectory has its own line). Paths are
decoded with surrogateescape and written with ensure_ascii, so names that are
not UTF-8 round-trip exactly and the file stays ASCII.
"""

import dataclasses
import errno
import gzip
import json
import os
import tempfile
import zlib
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Self

from trackdir.model import (
    ROOT,
    DirProbe,
    ProbeError,
    ShouldList,
    Stage,
    check_path,
    split,
)

FORMAT = "trackdir-capture"
VERSION = 1
GZIP_LEVEL = 6  # the stdlib default, 9, is several times slower for a few % less
_STAT_FIELDS = (
    "ino",
    "mode",
    "uid",
    "gid",
    "nlink",
    "rctime",
    "rbytes",
    "rfiles",
    "rsubdirs",
)


@dataclass(frozen=True, slots=True)
class CaptureHeader:
    """Metadata on line 1 of a capture file."""

    root: str  # ROOT as the kernel resolved it
    max_depth: int
    cluster_fsid: str | None
    rstat_mode: str  # an RStatMode value
    openat2: bool
    jobs: int
    started: str  # ISO 8601, UTC
    finished: str
    euid: int
    host: str | None
    kernel: str
    trackdir_version: str
    dirs: int
    errors: int  # directories with an error
    anonymized: bool


@dataclass(frozen=True, slots=True)
class Capture:
    """A loaded capture; probes are keyed by path, in tree order, with children."""

    header: CaptureHeader
    probes: dict[bytes, DirProbe]


class CaptureFormatError(Exception):
    """A capture file is malformed or of an unsupported format or version."""


def is_listed(probe: DirProbe, max_depth: int) -> bool:
    """Return whether a capture taken at MAX_DEPTH listed PROBE's directory."""
    if probe.depth >= max_depth:
        return False
    return probe.error is None or probe.error.stage == Stage.RSTATS


class CaptureWriter:
    """Writes one capture file atomically and never overwrites.

    The constructor creates a hidden temp file next to OUT, so an unusable
    output directory fails before a long walk. commit() links it to OUT;
    close() removes it. Without commit(), no output appears.
    """

    def __init__(self, out: Path) -> None:
        """Raises FileExistsError if OUT exists, OSError if the temp file fails."""
        if out.exists() or out.is_symlink():
            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(out))
        fd, tmp = tempfile.mkstemp(
            dir=out.parent, prefix=f".{out.name}.", suffix=".tmp"
        )
        self._out = out
        self._tmp = Path(tmp)
        self._file = open(fd, "wb")  # noqa: SIM115 -- closed by close()

    def commit(self, header: CaptureHeader, probes: Iterable[DirProbe]) -> None:
        """Write HEADER and PROBES (in tree order), then publish the file as OUT.

        Raises FileExistsError if OUT appeared meanwhile; other OSErrors propagate.
        """
        # mtime=0 keeps the bytes reproducible for identical content.
        with gzip.GzipFile(
            fileobj=self._file, mode="wb", compresslevel=GZIP_LEVEL, mtime=0
        ) as gz:
            gz.write(_json_line(_header_to_json(header)))
            for probe in probes:
                gz.write(_json_line(_probe_to_json(probe)))
        self._file.flush()
        os.fsync(self._file.fileno())
        os.link(self._tmp, self._out)

    def close(self) -> None:
        self._file.close()
        self._tmp.unlink(missing_ok=True)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def write_capture(out: Path, header: CaptureHeader, probes: Iterable[DirProbe]) -> None:
    """Write a capture file to OUT; see CaptureWriter."""
    with CaptureWriter(out) as writer:
        writer.commit(header, probes)


def load_capture(path: Path) -> Capture:
    """Read and validate a capture file. Raises CaptureFormatError or OSError."""
    try:
        with gzip.open(path, "rb") as f:
            header = _header_from_json(json.loads(f.readline()))
            records = [_probe_from_json(json.loads(line)) for line in f]
    # ValueError covers JSON and UTF-8 decoding; the others, gzip damage.
    except (EOFError, gzip.BadGzipFile, zlib.error, ValueError) as e:
        raise CaptureFormatError(f"{path}: {e}") from e
    return Capture(header, _with_children(records, header.max_depth))


class ReplayBackend:
    """Backend that serves a loaded capture, so walks run without a filesystem."""

    def __init__(self, capture: Capture, max_depth: int) -> None:
        """MAX_DEPTH is the walk's max depth; it must not exceed the capture's."""
        if max_depth > capture.header.max_depth:
            raise ValueError(
                f"cannot replay to depth {max_depth}: the capture was taken at "
                f"depth {capture.header.max_depth}"
            )
        self._probes = capture.probes

    def probe(self, path: bytes, should_list: ShouldList) -> DirProbe:
        """Return PATH's stored probe, with children only if should_list agrees."""
        stored = self._probes[path]
        unlisted = replace(stored, children=None)
        return stored if should_list(unlisted) else unlisted


def _json_line(obj: dict[str, object]) -> bytes:
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode("ascii")


def _header_to_json(header: CaptureHeader) -> dict[str, object]:
    return {"format": FORMAT, "version": VERSION, **dataclasses.asdict(header)}


def _header_from_json(obj: object) -> CaptureHeader:
    if not isinstance(obj, dict) or obj.get("format") != FORMAT:
        raise CaptureFormatError("not a trackdir capture")
    if obj.get("version") != VERSION:
        raise CaptureFormatError(
            f"unsupported capture version {obj.get('version')!r}; "
            f"this trackdir reads version {VERSION}"
        )
    fields = {k: v for k, v in obj.items() if k not in ("format", "version")}
    try:
        header = CaptureHeader(**fields)
    except TypeError as e:
        raise CaptureFormatError(f"bad header: {e}") from e
    if type(header.max_depth) is not int or header.max_depth < 0:
        raise CaptureFormatError(f"bad header: max_depth {header.max_depth!r}")
    return header


def _probe_to_json(probe: DirProbe) -> dict[str, object]:
    record: dict[str, object] = {"path": os.fsdecode(probe.path)}
    for name in _STAT_FIELDS:
        value = getattr(probe, name)
        if value is not None:
            record[name] = value
    if probe.error is not None:
        record["error"] = {"stage": str(probe.error.stage), "code": probe.error.code}
    return record


def _probe_from_json(record: object) -> DirProbe:
    try:
        if not isinstance(record, dict):
            raise TypeError("not an object")
        path = os.fsencode(record["path"])
        check_path(path)
        error = record.get("error")
        return DirProbe(
            path=path,
            **{name: _optional_int(record, name) for name in _STAT_FIELDS},
            error=None
            if error is None
            else ProbeError(Stage(error["stage"]), str(error["code"])),
        )
    except (KeyError, TypeError, ValueError) as e:
        raise CaptureFormatError(f"bad record {record!r}: {e}") from e


def _optional_int(record: dict[str, object], name: str) -> int | None:
    value = record.get(name)
    if value is not None and type(value) is not int:
        raise ValueError(f"{name} is not an integer")
    return value


def _with_children(records: list[DirProbe], max_depth: int) -> dict[bytes, DirProbe]:
    """Index RECORDS by path and rebuild children; RECORDS must be in tree order."""
    if not records or records[0].path != ROOT:
        raise CaptureFormatError('the first record must be ROOT (".")')
    probes: dict[bytes, DirProbe] = {}
    children: dict[bytes, list[bytes]] = {}
    for probe in records:
        if probe.path in probes:
            raise CaptureFormatError(f"duplicate path {probe.path!r}")
        if probe.depth > max_depth:
            raise CaptureFormatError(
                f"{probe.path!r} is deeper than max_depth {max_depth}"
            )
        if probe.path != ROOT:
            parent, name = split(probe.path)
            if parent not in children:
                raise CaptureFormatError(f"{probe.path!r} has no listed parent")
            children[parent].append(name)
        probes[probe.path] = probe
        if is_listed(probe, max_depth):
            children[probe.path] = []
    return {
        path: replace(
            probe, children=tuple(children[path]) if path in children else None
        )
        for path, probe in probes.items()
    }
