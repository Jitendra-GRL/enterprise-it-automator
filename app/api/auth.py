"""API key auth for endpoints that submit tickets, decide approvals, or expose
approval/audit detail. Checked via a FastAPI dependency against the
`X-API-Key` header.

If `API_KEY` is left unset in config (e.g. quick local demo runs), auth is
disabled and a startup log warns loudly — this must be set before the API is
reachable from anywhere but localhost.
"""

import logging

from fastapi import Header, HTTPException

from app.config import get_settings

logger = logging.getLogger(__name__)


async def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    settings = get_settings()
    if not settings.api_key:
        return
    if x_api_key != settings.api_key:
        raise HTTPException(401, "Missing or invalid API key")
