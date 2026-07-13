"""OIDC-verified reviewer identity (Roadmap Stage 4.1, right-sized).

Verifies RS256-signed JWTs from any spec-compliant OIDC provider (Keycloak,
Auth0, Entra ID, Okta — anything publishing a discovery document) so an
approval decision can be bound to an IdP-verified identity instead of a
locally-issued shared secret. Deliberately the resource-server HALF of
OIDC only: this app never runs a login flow, never issues tokens, and never
talks to the IdP beyond fetching its public signing keys — the reviewer
obtains their token however the IdP says (SSO portal, CLI, Postman), and
this app just verifies what's presented. That's the piece that makes the
audit trail mean something ("IdP X vouched for subject Y at time Z"), and
it's small enough to stay honest about — full OAuth login flows, sessions,
and SCIM sync remain explicitly out of scope (see ROADMAP.md's trap notes).

Fail-closed by construction: OIDC is enabled ONLY when both OIDC_ISSUER and
OIDC_AUDIENCE are configured (Settings.oidc_enabled). Signature, issuer,
audience, and expiry are all verified — a token failing ANY check is a 401,
never a fallthrough to weaker checks. When disabled (the default), every
call path is byte-for-byte the pre-OIDC behavior: X-Reviewer-Token only.

Key handling: the IdP's JWKS is fetched via its discovery document
(https://<issuer>/.well-known/openid-configuration -> jwks_uri, the one URL
shape every compliant IdP shares — Keycloak and Auth0 disagree about the
JWKS path itself, but not about discovery), cached in-process, and refetched
at most once per _JWKS_MIN_REFRESH_SECONDS when an unknown `kid` appears
(IdP key rotation) — so a burst of tokens signed by a forged kid can't be
turned into a JWKS-fetch flood against the IdP either.
"""

import logging
import time
from typing import Any

import httpx
import jwt

from app.config import get_settings

logger = logging.getLogger(__name__)

_JWKS_MIN_REFRESH_SECONDS = 60.0
_DISCOVERY_CACHE_TTL_SECONDS = 3600.0

_jwks_keys: dict[str, Any] = {}  # kid -> PyJWT-ready public key object
_jwks_fetched_at: float = 0.0
_jwks_url_cached: str = ""
_jwks_url_fetched_at: float = 0.0


class OIDCVerificationError(Exception):
    """Any reason the presented bearer token can't be accepted — invalid
    signature, wrong issuer/audience, expired, unknown key, IdP unreachable.
    Collapsed into one exception type on purpose: the HTTP layer must return
    the same 401 shape for all of them (a granular 'signature bad' vs
    'expired' distinction helps an attacker more than a legitimate user).
    """


def reset_caches() -> None:
    """Clears the module-level JWKS/discovery caches — needed by tests that
    reconfigure the issuer between cases, and harmless everywhere else.
    """
    global _jwks_keys, _jwks_fetched_at, _jwks_url_cached, _jwks_url_fetched_at
    _jwks_keys = {}
    _jwks_fetched_at = 0.0
    _jwks_url_cached = ""
    _jwks_url_fetched_at = 0.0


async def _fetch_json(url: str) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


async def _get_jwks_url() -> str:
    """Resolves the JWKS URL: an explicit OIDC_JWKS_URL setting wins;
    otherwise the issuer's discovery document is consulted (and cached —
    the discovery document changes ~never, but a cap avoids one HTTP
    round-trip per verification forever).
    """
    global _jwks_url_cached, _jwks_url_fetched_at
    settings = get_settings()
    if settings.oidc_jwks_url:
        return settings.oidc_jwks_url
    now = time.monotonic()
    if _jwks_url_cached and (now - _jwks_url_fetched_at) < _DISCOVERY_CACHE_TTL_SECONDS:
        return _jwks_url_cached
    discovery_url = settings.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration"
    try:
        document = await _fetch_json(discovery_url)
    except Exception as exc:
        raise OIDCVerificationError(f"OIDC discovery document unreachable: {exc}") from exc
    jwks_uri = document.get("jwks_uri")
    if not jwks_uri:
        raise OIDCVerificationError("OIDC discovery document has no jwks_uri")
    _jwks_url_cached = jwks_uri
    _jwks_url_fetched_at = now
    return jwks_uri


async def _fetch_jwks() -> dict:
    """Fetches the raw JWKS document. Isolated into its own function so
    tests can monkeypatch exactly the network boundary and nothing else.
    """
    return await _fetch_json(await _get_jwks_url())


async def _refresh_jwks_keys() -> None:
    global _jwks_keys, _jwks_fetched_at
    jwks = await _fetch_jwks()
    keys: dict[str, Any] = {}
    for entry in jwks.get("keys", []):
        kid = entry.get("kid")
        if not kid:
            continue
        try:
            keys[kid] = jwt.PyJWK(entry).key
        except Exception:  # unsupported kty/alg entries are skipped, not fatal
            logger.warning("Skipping unparseable JWKS entry kid=%r", kid)
    _jwks_keys = keys
    _jwks_fetched_at = time.monotonic()


async def _get_signing_key(kid: str) -> Any:
    if kid in _jwks_keys:
        return _jwks_keys[kid]
    # Unknown kid: allow ONE refetch per _JWKS_MIN_REFRESH_SECONDS window
    # (legitimate rotation introduces a new kid exactly once; a flood of
    # forged kids must not become a flood of IdP fetches).
    if time.monotonic() - _jwks_fetched_at >= _JWKS_MIN_REFRESH_SECONDS or not _jwks_keys:
        try:
            await _refresh_jwks_keys()
        except OIDCVerificationError:
            raise
        except Exception as exc:
            raise OIDCVerificationError(f"JWKS fetch failed: {exc}") from exc
    key = _jwks_keys.get(kid)
    if key is None:
        raise OIDCVerificationError(f"Token signed with unknown key id {kid!r}")
    return key


async def verify_oidc_token(token: str) -> dict:
    """Verifies an OIDC bearer JWT end to end (signature via the IdP's
    published keys, issuer, audience, expiry) and returns its claims.
    Raises OIDCVerificationError on any failure. Callers must treat the
    returned claims as authenticated identity ONLY — authorization (is this
    identity a registered reviewer, what may they decide) stays in
    app/api/auth.py and app/api/rbac.py.
    """
    settings = get_settings()
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as exc:
        raise OIDCVerificationError(f"Malformed JWT: {exc}") from exc

    kid = header.get("kid")
    if not kid:
        raise OIDCVerificationError("JWT header has no kid — cannot select a verification key")

    key = await _get_signing_key(kid)
    try:
        claims = jwt.decode(
            token,
            key=key,
            algorithms=["RS256"],  # pinned — an attacker-chosen alg (e.g. HS256
            # keyed with the PUBLIC key, or alg=none) is the classic JWT
            # verification bypass, so the accepted list is never taken from
            # the token itself.
            audience=settings.oidc_audience,
            issuer=settings.oidc_issuer,
        )
    except jwt.InvalidTokenError as exc:
        raise OIDCVerificationError(str(exc)) from exc
    return claims
