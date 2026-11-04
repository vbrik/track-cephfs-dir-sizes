"""The daemon's config file: TOML, validated into frozen dataclasses.

Only `trackdir daemon` reads it. Unknown keys are errors, so a misspelt
optional key cannot silently fall back to its default. Limits and defaults
are the CLI options' constants, so a tree means the same in both places.
"""

import logging
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trackdir.dbcli import DSN_ENV
from trackdir.live import DEFAULT_JOBS, DEFAULT_MAX_DIRS
from trackdir.messages import FatalError
from trackdir.model import MAX_DEPTH_LIMIT, MAX_DIRS_LIMIT

_INTERVAL = re.compile(r"([1-9][0-9]*)([smhd])")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
# Longest interval in days; far beyond any use, and within what setitimer,
# timedelta and sleep accept.
MAX_INTERVAL_DAYS = 365
_LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}
_TOP_KEYS = {"interval", "db", "log_level", "tree"}
_TREE_KEYS = {"root", "max_depth", "max_dirs", "jobs"}


@dataclass(frozen=True, slots=True)
class Interval:
    """A cycle length: seconds, and the text it was written as, for messages."""

    seconds: float
    text: str


@dataclass(frozen=True, slots=True)
class TreeConfig:
    """One [[tree]] entry; the fields mean what `trackdir sample` options do."""

    root: Path
    max_depth: int
    max_dirs: int = DEFAULT_MAX_DIRS
    jobs: int = DEFAULT_JOBS


@dataclass(frozen=True, slots=True)
class DaemonConfig:
    """A validated config file."""

    path: Path  # the file, for messages
    interval: Interval
    dsn: str
    log_level: int  # a logging level
    trees: tuple[TreeConfig, ...]  # sampled in this order


def parse_interval(text: str) -> Interval:
    """Return the Interval TEXT (e.g. "6h") denotes; raises ValueError."""
    match = _INTERVAL.fullmatch(text)
    if match is None:
        raise ValueError(
            "interval must be <integer><unit> with unit s, m, h or d, "
            f'e.g. "6h": {text!r}'
        )
    number, unit = match.groups()
    seconds = int(number) * _UNIT_SECONDS[unit]
    if seconds > MAX_INTERVAL_DAYS * _UNIT_SECONDS["d"]:
        raise ValueError(f"interval must be at most {MAX_INTERVAL_DAYS}d: {text!r}")
    return Interval(seconds, text)


def load(path: Path) -> DaemonConfig:
    """Read and validate the config file PATH; raises FatalError."""
    try:
        data = tomllib.loads(path.read_bytes().decode())
    except OSError as e:
        raise FatalError(f"cannot read the config {path}: {e.strerror}.") from e
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise FatalError(f"{path}: {e}.") from e
    _check_keys(path, "", data, _TOP_KEYS)
    interval = _interval(path, _required(path, "", data, "interval"))
    trees = data.get("tree")
    if (
        not isinstance(trees, list)
        or not trees
        or not all(isinstance(tree, dict) for tree in trees)
    ):
        raise FatalError(
            f"{path}: tree must be a list of [[tree]] tables with at least one entry."
        )
    return DaemonConfig(
        path=path,
        interval=interval,
        dsn=_dsn(path, data),
        log_level=_log_level(path, data.get("log_level", "INFO")),
        trees=tuple(_tree(path, n, tree) for n, tree in enumerate(trees, 1)),
    )


def _tree(path: Path, n: int, data: Mapping[str, Any]) -> TreeConfig:
    where = f"tree {n}: "
    _check_keys(path, where, data, _TREE_KEYS)
    root = _required(path, where, data, "root")
    if not isinstance(root, str):
        raise FatalError(f"{path}: {where}root must be a string: {root!r}.")
    if "\0" in root:
        raise FatalError(f"{path}: {where}root must not contain a NUL character.")
    if not os.path.isabs(root):
        raise FatalError(f"{path}: {where}root must be an absolute path: {root!r}.")
    max_depth = _required(path, where, data, "max_depth")
    return TreeConfig(
        root=Path(root),
        max_depth=_integer(path, where, "max_depth", max_depth, 0, MAX_DEPTH_LIMIT),
        max_dirs=_integer(
            path,
            where,
            "max_dirs",
            data.get("max_dirs", DEFAULT_MAX_DIRS),
            1,
            MAX_DIRS_LIMIT,
        ),
        jobs=_integer(path, where, "jobs", data.get("jobs", DEFAULT_JOBS), 1),
    )


def _check_keys(
    path: Path, where: str, data: Mapping[str, Any], allowed: set[str]
) -> None:
    for key in data:
        if key not in allowed:
            raise FatalError(f"{path}: {where}unknown key {key!r}.")


def _required(path: Path, where: str, data: Mapping[str, Any], key: str) -> Any:
    try:
        return data[key]
    except KeyError:
        raise FatalError(f"{path}: {where}{key} is required.") from None


def _integer(
    path: Path,
    where: str,
    key: str,
    value: Any,
    minimum: int,
    maximum: int | None = None,
) -> int:
    # bool is an int subclass; TOML's true must not pass as 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise FatalError(f"{path}: {where}{key} must be an integer: {value!r}.")
    if value < minimum:
        raise FatalError(f"{path}: {where}{key} must be at least {minimum}: {value}.")
    if maximum is not None and value > maximum:
        raise FatalError(f"{path}: {where}{key} must be at most {maximum}: {value}.")
    return value


def _interval(path: Path, value: Any) -> Interval:
    if not isinstance(value, str):
        raise FatalError(f'{path}: interval must be a string such as "6h": {value!r}.')
    try:
        return parse_interval(value)
    except ValueError as e:
        raise FatalError(f"{path}: {e}.") from None


def _dsn(path: Path, data: Mapping[str, Any]) -> str:
    dsn = data.get("db")
    if dsn is not None and not isinstance(dsn, str):
        raise FatalError(f"{path}: db must be a string: {dsn!r}.")
    dsn = dsn or os.environ.get(DSN_ENV)
    if not dsn:
        raise FatalError(f"{path}: no database given: set db or {DSN_ENV}.")
    return dsn


def _log_level(path: Path, value: Any) -> int:
    try:
        return _LOG_LEVELS[value]
    except (KeyError, TypeError):
        names = ", ".join(_LOG_LEVELS)
        raise FatalError(
            f"{path}: log_level must be one of {names}: {value!r}."
        ) from None
