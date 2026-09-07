"""Resource budgets for untrusted input (AUD-009).

Nothing here is a security control -- dustpan reads a folder its user chose,
so there is no hostile submitter. These exist because the cost curve is bad:
tokenising a 1 MiB DAX expression peaked at ~278 MiB RSS in an external
audit's stress run, roughly 270x amplification, and a whole-file read has no
ceiling at all. A model with a few pathological expressions could exhaust a
laptop or a CI runner by accident long before anyone did it on purpose.

Exceeding a budget is never silent: the file or expression is skipped and a
material scan note is recorded, so the results say they are incomplete
rather than looking clean.

Every limit can be raised or disabled (set to 0) per run:

    DUSTPAN_MAX_FILE_BYTES=52428800 dustpan scan ./models
"""

from __future__ import annotations

import os

__all__ = ["MAX_EXPRESSION_BYTES", "MAX_FILE_BYTES", "describe", "exceeds"]


def _budget(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    # A negative value is not a request to disable the budget -- 0 is. Treating
    # `-5` as "no limit" would silently remove every cap on a typo, so an
    # out-of-range value falls back to the documented default.
    return value if value >= 0 else default


#: Largest single source file (TMDL, model.bim, report JSON) that is read.
MAX_FILE_BYTES: int = _budget("DUSTPAN_MAX_FILE_BYTES", 16 * 1024 * 1024)

#: Largest single DAX expression that is tokenised. Real measures are a few
#: hundred bytes; anything past this is generated or corrupt.
MAX_EXPRESSION_BYTES: int = _budget("DUSTPAN_MAX_EXPRESSION_BYTES", 256 * 1024)


def exceeds(size: int, budget: int) -> bool:
    """True when `size` is over `budget`. A budget of 0 disables the check."""
    return budget > 0 and size > budget


def describe(size: int, budget: int, what: str, env: str) -> str:
    return (
        f"{what} is {size:,} bytes, over the {budget:,}-byte budget -- skipped. "
        f"Raise or disable it with {env} (0 disables)."
    )
