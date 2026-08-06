"""Example external reviewer implementation.

Copy this file to ``realname_reviewer.py`` and implement the integration with
your own verification service locally. Keep credentials out of this file.
"""

from __future__ import annotations

from typing import Any


def review_application(application: dict[str, Any]) -> dict[str, str]:
    """Return ``approve``, ``reject`` or ``timeout`` for one application."""

    del application
    return {
        "status": "timeout",
        "reason": "Example reviewer is not connected to a verification service",
    }
