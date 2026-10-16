"""Text shared by the commands: the fatal-error type and message formatting."""

import textwrap
from collections import Counter

WRAP_WIDTH = 79


class FatalError(Exception):
    """Stops the run; the message is printed as one ERROR: paragraph."""


def paragraph(text: str) -> str:
    """Wrap TEXT to WRAP_WIDTH columns, with a final newline."""
    wrapped = textwrap.fill(
        text, WRAP_WIDTH, break_long_words=False, break_on_hyphens=False
    )
    return wrapped + "\n"


def describe_errors(errors: Counter[tuple[str, str]]) -> str:
    """Return "none had errors" or "N had errors: open EACCES 3, ..."."""
    if not errors:
        return "none had errors"
    counts = ", ".join(
        f"{stage} {code} {n}" for (stage, code), n in sorted(errors.items())
    )
    return f"{errors.total()} had errors: {counts}"
