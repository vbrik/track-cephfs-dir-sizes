"""trackdir db summary: report what the database holds, as text or JSON."""

import json
import os
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime
from typing import TextIO

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
from trackdir.growth import Window
from trackdir.messages import FatalError, describe_errors
from trackdir.model import fs_absolute

_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB")
_PREFIXES = ("k", "M", "G", "T", "P", "E")  # of counts, powers of 1000
_GROWTH_COLUMNS = (  # (header, right-aligned) of the growth table
    ("own", True),
    ("share", True),
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


@dataclass(frozen=True)
class _Style:
    """Wraps text in ANSI SGR codes if COLOR, else returns it unchanged.

    Spans never nest, as each ends by resetting every attribute.
    """

    color: bool

    def bold(self, text: str) -> str:
        return self._sgr("1", text)

    def dim(self, text: str) -> str:
        return self._sgr("2", text)

    def warn(self, text: str) -> str:
        return self._sgr("33", text)  # yellow

    def _sgr(self, code: str, text: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.color else text


class _Warning(str):
    """A table cell shown in the warning style."""


def color_wanted(stream: TextIO) -> bool:
    """Return whether text for STREAM should be colored: it is a terminal and
    NO_COLOR is unset or empty (https://no-color.org)."""
    return stream.isatty() and not os.environ.get("NO_COLOR")


def run_summary(
    dsn: str, tree: str | None, recent: int, top: int, since: Sequence[Window]
) -> DbStats:
    """Return the statistics of DSN's database; see db.summarize.

    Raises FatalError, also if no tree is named TREE.
    """
    with database(dsn, DB_UNCHANGED) as conn:
        stats = summarize(conn, tree, recent, top, since)
    if tree is not None and not stats.tree_stats:
        raise FatalError(
            f"the database has no tree {tree}. Run trackdir db summary without "
            "--tree to list the trees."
        )
    return stats


def render_text(stats: DbStats, *, color: bool = False) -> str:
    """Return STATS as text: a database line, then a block per tree; COLOR
    adds ANSI styles that leave the layout as it is."""
    style = _Style(color)
    lines = [
        f"{style.bold('Database:')} schema version {stats.schema_version}; "
        f"{_count(stats.trees, 'tree')}, {_count(stats.samples, 'sample')}, "
        f"{_count(stats.dir_states, 'dir_state row')}; "
        f"{format_bytes(stats.bytes_on_disk)} on disk"
    ]
    for tree in stats.tree_stats:
        lines += ["", *_tree_lines(tree, style)]
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


def format_count(n: int) -> str:
    """Return N to 3 significant digits with an SI prefix, e.g. "962", "72.0k"
    or "-2.05M"; halves round away from zero.

    Integer arithmetic keeps the rounding exact, as floats would not.
    """
    size = abs(n)
    if size < 1000:
        return str(n)
    dropped = len(str(size)) - 3  # digits rounded away
    digits = str((size + 5 * 10 ** (dropped - 1)) // 10**dropped)
    if len(digits) > 3:  # rounded up to a power of ten: "1000"
        digits, dropped = digits[:3], dropped + 1
    prefix = min((dropped + 2) // 3, len(_PREFIXES))
    whole = dropped + 3 - 3 * prefix  # digits before the decimal point
    if whole < 3:
        digits = f"{digits[:whole]}.{digits[whole:]}"
    else:
        digits += "0" * (whole - 3)
    sign = "-" if n < 0 else ""
    return f"{sign}{digits}{_PREFIXES[prefix - 1]}"


def _tree_lines(tree: TreeStats, style: _Style) -> list[str]:
    """Return a heading, the tree's lines, then its samples and each growth
    window, each set off by a blank line."""
    lines = [
        f"{style.bold(f'Tree {tree.name}')} {style.dim(f'({tree_identity(tree.key)})')}"
    ]
    samples = f"  {style.dim('samples:')} {format_count(tree.samples)}"
    if tree.first_started is not None and tree.last_started is not None:
        samples += f", {_moment(tree.first_started)} to {_moment(tree.last_started)}"
    lines.append(samples)
    errors = describe_errors(tree.current_errors)
    lines.append(
        f"  {style.dim('current:')} {_count(tree.current_dirs, 'directory')}; "
        f"{style.warn(errors) if tree.current_errors else errors}"
    )
    lines.append(
        f"  {style.dim('db space:')} about"
        f" {format_bytes(tree.bytes_on_disk_estimate)} on disk (estimate)"
    )
    lines += _root_lines(tree.root_now, tree.root_first, style)
    hidden = tree.samples - len(tree.recent)
    if tree.recent or hidden:
        lines.append("")
    if tree.recent:
        rows = [_sample_row(s) for s in tree.recent]
        lines += _table(_SAMPLE_COLUMNS, rows, style)
    if hidden:
        noun = "older sample" if tree.recent else "sample"
        lines.append(f"  {style.dim(f'{_count(hidden, noun)} not shown')}")
    for growth in tree.growth:
        lines += ["", *_growth_lines(growth, tree.key, style)]
    return lines


def _growth_lines(growth: Growth, key: TreeKey, style: _Style) -> list[str]:
    """Return a heading and a table of directories, or one line if none grew.

    Each directory shows its own growth, its share of all positive own growth
    and its total growth; directories show as absolute paths in tree KEY's
    filesystem.
    """
    window = growth.window + (" (all history)" if growth.clipped else "")
    heading = "  " + style.bold(
        f"grew most in {window}, since sample {growth.since}"
        f" ({_moment(growth.since_started)}), by own growth:"
    )
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
                _share(d.own, growth.positive_own),
                _signed_bytes(d.rbytes_after - (d.rbytes_before or 0)),
                path + (f" ({', '.join(marks)})" if marks else ""),
            ]
        )
    table = _table(_GROWTH_COLUMNS, rows, style)
    return [heading, *("  " + line for line in table)]


def _share(part: int, whole: int) -> str:
    """Return PART as a percentage of WHOLE rounded down, "<1%" if below 1."""
    percent = part * 100 // whole
    return f"{percent}%" if percent else "<1%"


def _root_lines(
    now: RootTotals | None, first: RootTotals | None, style: _Style
) -> list[str]:
    root = f"  {style.dim('ROOT:')}    "
    if now is None:
        return [f"{root}no rstats in its current row"]
    lines = [
        f"{root}{format_bytes(now.rbytes)}, {_count(now.rfiles, 'file')}, "
        f"{_count(now.rsubdirs, 'directory')}, last changed in sample {now.sample_id}"
    ]
    if first is not None and first.sample_id != now.sample_id:
        lines.append(
            f"  {style.dim('change:')}  {_signed_bytes(now.rbytes - first.rbytes)}, "
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
    """Return e.g. "1 file", "72.0k files" or, if SIGNED, "+0 directories"."""
    if abs(n) != 1:
        noun = noun[:-1] + "ies" if noun.endswith("y") else noun + "s"
    sign = "+" if signed and n >= 0 else ""
    return f"{sign}{format_count(n)} {noun}"


def _sample_row(sample: SampleStats) -> list[str]:
    took = (sample.finished - sample.started).total_seconds()
    return [
        str(sample.id),
        sample.kind,
        _moment(sample.started),
        f"{took:.1f} s",
        *map(
            format_count, (sample.probed, sample.new, sample.modified, sample.deleted)
        ),
        (_Warning if sample.errors else str)(format_count(sample.errors)),
        str(sample.euid),
        "live" if sample.replayed_from is None else "replay",
    ]


def _table(
    columns: Sequence[tuple[str, bool]], rows: Sequence[Sequence[str]], style: _Style
) -> list[str]:
    """Return a dim header and ROWS as indented lines; COLUMNS are (header,
    right). Cells are styled after padding, so styles do not shift columns."""
    header = [name for name, _ in columns]
    aligns = [right for _, right in columns]
    widths = [max(len(row[i]) for row in (header, *rows)) for i in range(len(header))]

    def line(cells: Sequence[str]) -> str:
        padded = []
        for cell, width, right in zip(cells, widths, aligns, strict=True):
            pad = " " * (width - len(cell))
            text = style.warn(cell) if isinstance(cell, _Warning) else cell
            padded.append(pad + text if right else text + pad)
        return "  ".join(padded).rstrip()

    return ["  " + style.dim(line(header)), *("  " + line(row) for row in rows)]


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
