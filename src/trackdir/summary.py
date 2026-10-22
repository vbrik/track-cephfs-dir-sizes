"""trackdir db summary: report what the database holds, as text or JSON."""

import json
import os
from collections import Counter
from collections.abc import Sequence
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime

from trackdir.db import (
    DbStats,
    Growth,
    RootTotals,
    SampleStats,
    TreeKey,
    TreeStats,
    summarize,
)
from trackdir.dbcli import DB_UNCHANGED, database, display_path, tree_identity, utc
from trackdir.messages import FatalError, describe_errors
from trackdir.model import fs_absolute

_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB")
_GROWTH_COLUMNS = (  # (header, right-aligned) of the growth table
    ("own", True),
    ("of ROOT", True),
    ("total", True),
    ("directory", False),
)
_SAMPLE_COLUMNS = (  # (header, right-aligned) of the sample table
    ("sample", True),
    ("kind", False),
    ("started (UTC)", False),
    ("took", True),
    ("probed", True),
    ("new", True),
    ("modified", True),
    ("deleted", True),
    ("errors", True),
    ("euid", True),
    ("source", False),
)


def run_summary(dsn: str, tree_id: int | None, recent: int, top: int) -> DbStats:
    """Return the statistics of DSN's database; see db.summarize.

    Raises FatalError, also if tree TREE_ID does not exist.
    """
    with database(dsn, DB_UNCHANGED) as conn:
        stats = summarize(conn, tree_id, recent, top)
    if tree_id is not None and not stats.tree_stats:
        raise FatalError(
            f"the database has no tree {tree_id}. Run trackdir db summary without "
            "--tree to list the trees."
        )
    return stats


def render_text(stats: DbStats) -> str:
    """Return STATS as text: a database line, then a block per tree."""
    lines = [
        f"Database: schema version {stats.schema_version}; "
        f"{_count(stats.trees, 'tree')}, {_count(stats.samples, 'sample')}, "
        f"{_count(stats.dir_states, 'dir_state row')}; "
        f"{format_bytes(stats.bytes_on_disk)} on disk"
    ]
    for tree in stats.tree_stats:
        lines += ["", *_tree_lines(tree)]
    return "\n".join(lines) + "\n"


def render_json(stats: DbStats) -> str:
    """Return STATS as one JSON document.

    Paths are fsdecoded and written ASCII-only, as in captures, so undecodable
    bytes survive as escaped surrogates; times are ISO 8601 in UTC.
    """
    data = _jsonable(stats)
    database = {k: v for k, v in data.items() if k != "tree_stats"}
    document = {"database": database, "trees": data["tree_stats"]}
    return json.dumps(document, indent=2) + "\n"


def format_bytes(size: int) -> str:
    """Return SIZE in bytes with a binary unit, e.g. "1023 B" or "-1.5 KiB"."""
    value = abs(size)
    unit = 0
    while value >= 1024 and unit < len(_UNITS) - 1:
        value /= 1024
        unit += 1
    sign = "-" if size < 0 else ""
    if unit == 0:
        return f"{sign}{value} B"
    return f"{sign}{value:.1f} {_UNITS[unit]}"


def _tree_lines(tree: TreeStats) -> list[str]:
    lines = [f"Tree {tree.id} ({tree_identity(tree.key)})"]
    samples = f"  samples: {tree.samples}"
    if tree.first_started is not None and tree.last_started is not None:
        samples += f", {_moment(tree.first_started)} to {_moment(tree.last_started)}"
    lines.append(samples)
    lines.append(
        f"  current: {_count(tree.current_dirs, 'directory')}; "
        f"{describe_errors(tree.current_errors)}"
    )
    lines += _root_lines(tree.root_now, tree.root_first)
    if tree.recent:
        lines += _table(_SAMPLE_COLUMNS, [_sample_row(s) for s in tree.recent])
    if hidden := tree.samples - len(tree.recent):
        noun = "older sample" if tree.recent else "sample"
        lines.append(f"  {_count(hidden, noun)} not shown")
    for growth in tree.growth:
        lines += _growth_lines(growth, tree.key)
    return lines


def _growth_lines(growth: Growth, key: TreeKey) -> list[str]:
    """Return a heading and a table of directories, or one line if none grew.

    Each directory shows its own growth, its share of ROOT's growth and its
    total growth; directories show as absolute paths in tree KEY's filesystem.
    """
    heading = f"  grew most since sample {growth.since}, by own growth:"
    if not growth.dirs:
        return [f"{heading} none"]
    rows = []
    for d in growth.dirs:
        flags = (("new", d.rbytes_before is None), ("subtree", d.subtree))
        marks = [mark for mark, on in flags if on]
        path = display_path(fs_absolute(key.fs_path, d.path))
        rows.append(
            [
                _signed_bytes(d.own),
                _share(d.own, growth.root),
                _signed_bytes(d.rbytes_after - (d.rbytes_before or 0)),
                path + (f" ({', '.join(marks)})" if marks else ""),
            ]
        )
    return [heading, *("  " + line for line in _table(_GROWTH_COLUMNS, rows))]


def _share(part: int, whole: int | None) -> str:
    """Return PART as a percentage of WHOLE rounded down, "<1%" if below 1.

    Without a positive WHOLE, the share is meaningless: return "-".
    """
    if whole is None or whole <= 0:
        return "-"
    percent = part * 100 // whole
    return f"{percent}%" if percent else "<1%"


def _root_lines(now: RootTotals | None, first: RootTotals | None) -> list[str]:
    if now is None:
        return ["  ROOT:    no rstats in its current row"]
    lines = [
        f"  ROOT:    {format_bytes(now.rbytes)}, {_count(now.rfiles, 'file')}, "
        f"{_count(now.rsubdirs, 'directory')}, last changed in sample {now.sample_id}"
    ]
    if first is not None and first.sample_id != now.sample_id:
        lines.append(
            f"  change:  {_signed_bytes(now.rbytes - first.rbytes)}, "
            f"{_count(now.rfiles - first.rfiles, 'file', signed=True)}, "
            f"{_count(now.rsubdirs - first.rsubdirs, 'directory', signed=True)} "
            f"since sample {first.sample_id}"
        )
    return lines


def _signed_bytes(size: int) -> str:
    """Return format_bytes(SIZE) with a sign, e.g. "+0 B" or "-1.5 KiB"."""
    text = format_bytes(size)
    return text if text.startswith("-") else "+" + text


def _count(n: int, noun: str, *, signed: bool = False) -> str:
    """Return e.g. "1 file", "3 files" or, if SIGNED, "+0 directories"."""
    if abs(n) != 1:
        noun = noun[:-1] + "ies" if noun.endswith("y") else noun + "s"
    return f"{n:{'+' if signed else ''}} {noun}"


def _sample_row(sample: SampleStats) -> list[str]:
    took = (sample.finished - sample.started).total_seconds()
    return [
        str(sample.id),
        sample.kind,
        _moment(sample.started),
        f"{took:.1f} s",
        *map(str, (sample.probed, sample.new, sample.modified, sample.deleted)),
        str(sample.errors),
        str(sample.euid),
        "live" if sample.replayed_from is None else "replay",
    ]


def _table(
    columns: Sequence[tuple[str, bool]], rows: Sequence[Sequence[str]]
) -> list[str]:
    """Return a header and ROWS as indented lines; COLUMNS are (header, right)."""
    header = [name for name, _ in columns]
    aligns = [right for _, right in columns]
    widths = [max(len(row[i]) for row in (header, *rows)) for i in range(len(header))]

    def line(cells: Sequence[str]) -> str:
        padded = (
            cell.rjust(width) if right else cell.ljust(width)
            for cell, width, right in zip(cells, widths, aligns, strict=True)
        )
        return ("  " + "  ".join(padded)).rstrip()

    return [line(header), *map(line, rows)]


def _moment(moment: datetime) -> str:
    """Return MOMENT in UTC to the second, e.g. 2026-10-16T20:48:11Z."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _jsonable(value: object) -> object:
    """Return VALUE, a statistics dataclass or a field of one, as JSON data."""
    match value:
        case Counter():
            return [
                {"stage": stage, "code": code, "dirs": n}
                for (stage, code), n in sorted(value.items())
            ]
        case bytes():
            return os.fsdecode(value)
        case datetime():
            return utc(value)
        case tuple():
            return [_jsonable(v) for v in value]
        case _ if is_dataclass(value):
            return {f.name: _jsonable(getattr(value, f.name)) for f in fields(value)}
        case _:
            return value
