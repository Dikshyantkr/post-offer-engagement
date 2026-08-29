"""Shared router dependencies.

Auth is out of scope for this app entirely: a recruiter-switcher dropdown in
the header sets who's acting, passed as the X-Actor header, defaulting to
"system" when absent (e.g. calls made without the header, or by automation).
"""

from __future__ import annotations

from fastapi import Header


def get_actor(x_actor: str | None = Header(default=None, alias="X-Actor")) -> str:
    return x_actor or "system"
