"""Replace names and ids in a capture with stable keyed tokens (HMAC-SHA256).

Unkeyed hashes of guessable names (users, projects, years) can be reversed by
hashing guesses; the secret key prevents that. The same key always yields the
same tokens, so captures taken with one key line up. Each path component is
tokenized separately, which keeps the tree shape and keeps repeated names
recognizable as repeats.
"""

import hmac
import os
import shlex
import stat
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from trackdir.capture import CaptureHeader
from trackdir.model import ROOT, DirProbe, tree_key

KEY_MIN_BYTES = 32
NAME_TOKEN_HEX = 16  # 64-bit name tokens
FSID_TOKEN_HEX = 32
# Fake ids are 1 .. 2**32 - 2: 0 (root) is kept, and 2**32 - 1 means "no id".
_ID_SPACE = 2**32 - 2


class AnonymizationError(Exception):
    """Two different names or ids would get the same token."""


def key_recipe(path: str | os.PathLike) -> str:
    """Return a shell command that creates a private key file at PATH."""
    return (
        f"(umask 077; head -c {KEY_MIN_BYTES} /dev/urandom > {shlex.quote(str(path))})"
    )


def load_key(path: Path) -> bytes:
    """Return the key stored in PATH.

    Raises ValueError, with the remedy in the message, if PATH is not a regular
    file, group or others can access it, or it holds fewer than KEY_MIN_BYTES
    bytes. Raises OSError if it cannot be read.
    """
    with open(path, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(f"key file {path} is not a regular file")
        # Like ssh with private keys: whoever reads the key can reverse tokens.
        if st.st_mode & 0o077:
            raise ValueError(
                f"key file {path} is accessible by group or others; "
                f"run: chmod 600 {shlex.quote(str(path))}"
            )
        key = f.read()
    if len(key) < KEY_MIN_BYTES:
        raise ValueError(
            f"key file {path} has {len(key)} bytes; it needs at least "
            f"{KEY_MIN_BYTES} random bytes. Create one with: {key_recipe(path)}"
        )
    return key


class Anonymizer:
    """Keyed, deterministic replacements for names, ids and fsids."""

    def __init__(self, key: bytes) -> None:
        self._key = key

    def _mac(self, domain: bytes, data: bytes) -> bytes:
        return hmac.digest(self._key, domain + b"\0" + data, "sha256")

    def name(self, name: bytes) -> bytes:
        """Return the token for one path component."""
        return self._mac(b"name", name).hex()[:NAME_TOKEN_HEX].encode("ascii")

    def path(self, path: bytes) -> bytes:
        """Return PATH with every component tokenized; ROOT stays ROOT."""
        if path == ROOT:
            return ROOT
        return b"/".join(self.name(part) for part in path.split(b"/"))

    def numeric_id(self, n: int) -> int:
        """Return the fake uid/gid for N; 0 stays 0."""
        if n == 0:
            return 0
        digest = self._mac(b"id", str(n).encode("ascii"))
        return 1 + int.from_bytes(digest[:4]) % _ID_SPACE

    def fsid(self, fsid: str) -> str:
        """Return the token for a cluster fsid."""
        return self._mac(b"fsid", fsid.encode("ascii")).hex()[:FSID_TOKEN_HEX]

    def abs_path(self, path: str) -> str:
        """Return absolute PATH with every component tokenized."""
        parts = [p for p in os.fsencode(path).split(b"/") if p]
        return "/" + "/".join(self.name(p).decode("ascii") for p in parts)


def anonymize(
    header: CaptureHeader, probes: Sequence[DirProbe], key: bytes
) -> tuple[CaptureHeader, list[DirProbe]]:
    """Return HEADER and PROBES with names, uids, gids and fsid replaced.

    The probes are returned in tree order. Raises AnonymizationError if two
    sibling names or two ids map to the same token.
    """
    an = Anonymizer(key)
    ids = {header.euid}
    ids.update(p.uid for p in probes if p.uid is not None)
    ids.update(p.gid for p in probes if p.gid is not None)
    fake_ids = {n: an.numeric_id(n) for n in ids}
    if len(set(fake_ids.values())) != len(fake_ids):
        raise AnonymizationError(
            "two uids or gids map to the same fake id; use another key"
        )
    anonymized = [
        replace(
            p,
            path=an.path(p.path),
            uid=None if p.uid is None else fake_ids[p.uid],
            gid=None if p.gid is None else fake_ids[p.gid],
            children=None
            if p.children is None
            else tuple(sorted(map(an.name, p.children))),
        )
        for p in probes
    ]
    # Distinct paths can only coincide if two siblings got the same token.
    if len({p.path for p in anonymized}) != len(anonymized):
        raise AnonymizationError(
            "two sibling names map to the same token; use another key"
        )
    anonymized.sort(key=lambda p: tree_key(p.path))
    new_header = replace(
        header,
        root=an.abs_path(header.root),
        cluster_fsid=None
        if header.cluster_fsid is None
        else an.fsid(header.cluster_fsid),
        euid=fake_ids[header.euid],
        host=None,
        anonymized=True,
    )
    return new_header, anonymized
