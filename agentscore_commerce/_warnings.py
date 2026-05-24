"""Shared one-shot warning helpers for the SDK.

Module-level state ensures each warning fires at most once per process,
regardless of how many ``Checkout`` / ``compute_first_checkout`` instances
trigger it.
"""

from __future__ import annotations

import logging

_warned_no_api_key = False


def warn_missing_api_key_once(label: str) -> None:
    """Emit a one-time warning when AGENTSCORE_API_KEY is unset.

    Triggered on a settle path that would otherwise enforce wallet OFAC SDN
    sanctions. Both ``Checkout`` and ``compute_first_checkout`` route through
    this so a single multi-surface app sees the warning ONCE, not once per
    surface.

    ``label`` is the caller's identifier for the log message; e.g.
    ``"checkout"`` or the compute-first handler's ``name``.
    """
    global _warned_no_api_key
    if _warned_no_api_key:
        return
    _warned_no_api_key = True
    logging.getLogger(__name__).warning(
        f"[{label}] AGENTSCORE_API_KEY is not set — wallet OFAC SDN sanctions are NOT being enforced. "
        "Set the env var to enable strict-liability protection on settle."
    )


def _reset_warned_no_api_key() -> None:
    """Test-only: reset the warn-once flag."""
    global _warned_no_api_key
    _warned_no_api_key = False
