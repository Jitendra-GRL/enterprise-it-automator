"""Tests for OIDC-verified reviewer identity (app/api/oidc.py + the
require_reviewer combined dependency in app/api/auth.py).

Fully offline: a real RSA keypair is generated per test module and the JWKS
network boundary (app.api.oidc._fetch_jwks) is monkeypatched to return its
public half — signature verification, issuer/audience/expiry checks, and
the auth fallback logic all run for real; only the IdP HTTP fetch is faked.
"""

import datetime as dt

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select

from app.api import oidc as oidc_module
from app.config import get_settings
from app.db import session as db_session_module
from app.db.models import Approval, ApprovalStatus, Reviewer, ReviewerRole, Ticket, TicketStatus

ISSUER = "https://idp.test/realms/it-automator"
AUDIENCE = "it-automator-api"
KID = "test-key-1"

_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks() -> dict:
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(_PRIVATE_KEY.public_key(), as_dict=True)
    jwk["kid"] = KID
    jwk["use"] = "sig"
    jwk["alg"] = "RS256"
    return {"keys": [jwk]}


def _mint_token(
    username: str = "mchen",
    *,
    issuer: str = ISSUER,
    audience: str = AUDIENCE,
    kid: str = KID,
    expires_in: int = 300,
    key=None,
    algorithm: str = "RS256",
    extra_claims: dict | None = None,
) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    claims = {
        "iss": issuer,
        "aud": audience,
        "sub": f"idp-subject-{username}",
        "preferred_username": username,
        "iat": now,
        "exp": now + dt.timedelta(seconds=expires_in),
    }
    claims.update(extra_claims or {})
    return jwt.encode(claims, key or _PRIVATE_KEY, algorithm=algorithm, headers={"kid": kid})


@pytest.fixture
async def oidc_client(monkeypatch, tmp_path):
    """App client with OIDC configured, the JWKS fetch faked, and one
    registered it_admin reviewer (mchen) with a legacy token as well.
    """
    db_path = tmp_path / "oidc_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("API_KEY", "test-api-key")
    monkeypatch.setenv("OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("OIDC_AUDIENCE", AUDIENCE)
    get_settings.cache_clear()
    db_session_module._engine = None
    db_session_module._session_factory = None
    oidc_module.reset_caches()

    async def fake_fetch_jwks() -> dict:
        return _jwks()

    monkeypatch.setattr(oidc_module, "_fetch_jwks", fake_fetch_jwks)

    import app.api.main as main_module

    await db_session_module.init_db()
    await main_module._ensure_bootstrap_admin_client()
    async with db_session_module.session_scope() as session:
        session.add(Reviewer(username="mchen", role=ReviewerRole.IT_ADMIN, token="legacy-token-mchen"))
        session.add(
            Ticket(id=1, requester="hr@x.com", subject="s", body="b", status=TicketStatus.AWAITING_APPROVAL)
        )
        session.add(
            Approval(
                id=1, ticket_id=1, tool_name="identity_disable_user",
                tool_args={"username": "jsmith"}, status=ApprovalStatus.PENDING,
            )
        )

    transport = httpx.ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"X-API-Key": "test-api-key"}
    ) as ac:
        yield ac

    db_session_module._engine = None
    db_session_module._session_factory = None
    oidc_module.reset_caches()
    get_settings.cache_clear()


def _reject_body() -> dict:
    # approve=False avoids resume_ticket_run (no graph/MCP machinery needed
    # to prove the auth layer worked) while still exercising the full
    # authorization + provenance-recording path.
    return {"approve": False}


async def test_valid_oidc_bearer_decides_approval(oidc_client):
    resp = await oidc_client.post(
        "/approvals/1/decide",
        json=_reject_body(),
        headers={"Authorization": f"Bearer {_mint_token('mchen')}"},
    )
    assert resp.status_code == 200, resp.text

    async with db_session_module.session_scope() as session:
        approval = await session.get(Approval, 1)
        assert approval.status == ApprovalStatus.REJECTED
        assert approval.reviewer == "mchen"
        assert approval.reviewer_auth_method == "oidc"
        assert approval.reviewer_oidc_subject == "idp-subject-mchen"


async def test_legacy_reviewer_token_still_works_with_oidc_enabled(oidc_client):
    resp = await oidc_client.post(
        "/approvals/1/decide",
        json=_reject_body(),
        headers={"X-Reviewer-Token": "legacy-token-mchen"},
    )
    assert resp.status_code == 200, resp.text
    async with db_session_module.session_scope() as session:
        approval = await session.get(Approval, 1)
        assert approval.reviewer_auth_method == "token"
        assert approval.reviewer_oidc_subject is None


async def test_wrong_audience_rejected(oidc_client):
    token = _mint_token("mchen", audience="some-other-api")
    resp = await oidc_client.post(
        "/approvals/1/decide", json=_reject_body(), headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 401


async def test_wrong_issuer_rejected(oidc_client):
    token = _mint_token("mchen", issuer="https://evil.test/realms/it-automator")
    resp = await oidc_client.post(
        "/approvals/1/decide", json=_reject_body(), headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 401


async def test_expired_token_rejected(oidc_client):
    token = _mint_token("mchen", expires_in=-60)
    resp = await oidc_client.post(
        "/approvals/1/decide", json=_reject_body(), headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 401


async def test_token_signed_by_unknown_key_rejected(oidc_client):
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = _mint_token("mchen", key=other_key, kid="rogue-kid")
    resp = await oidc_client.post(
        "/approvals/1/decide", json=_reject_body(), headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 401


async def test_garbage_bearer_rejected(oidc_client):
    resp = await oidc_client.post(
        "/approvals/1/decide", json=_reject_body(), headers={"Authorization": "Bearer not.a.jwt"}
    )
    assert resp.status_code == 401


async def test_verified_but_unregistered_username_is_403(oidc_client):
    token = _mint_token("stranger")
    resp = await oidc_client.post(
        "/approvals/1/decide", json=_reject_body(), headers={"Authorization": f"Bearer {token}"}
    )
    # Authentication succeeded (IdP vouched for them) — authorization failed.
    assert resp.status_code == 403


async def test_bad_bearer_does_not_fall_back_to_valid_reviewer_token(oidc_client):
    """A presented-but-invalid JWT must be authoritative — never silently
    ignored in favor of an accompanying legacy token (see require_reviewer's
    precedence note).
    """
    token = _mint_token("mchen", expires_in=-60)
    resp = await oidc_client.post(
        "/approvals/1/decide",
        json=_reject_body(),
        headers={
            "Authorization": f"Bearer {token}",
            "X-Reviewer-Token": "legacy-token-mchen",
        },
    )
    assert resp.status_code == 401
    async with db_session_module.session_scope() as session:
        approval = await session.get(Approval, 1)
        assert approval.status == ApprovalStatus.PENDING  # untouched


@pytest.fixture
async def no_oidc_client(monkeypatch, tmp_path):
    """App client with OIDC NOT configured — the backward-compat baseline."""
    db_path = tmp_path / "no_oidc_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("API_KEY", "test-api-key")
    monkeypatch.delenv("OIDC_ISSUER", raising=False)
    monkeypatch.delenv("OIDC_AUDIENCE", raising=False)
    get_settings.cache_clear()
    db_session_module._engine = None
    db_session_module._session_factory = None
    oidc_module.reset_caches()

    import app.api.main as main_module

    await db_session_module.init_db()
    await main_module._ensure_bootstrap_admin_client()
    async with db_session_module.session_scope() as session:
        session.add(Reviewer(username="mchen", role=ReviewerRole.IT_ADMIN, token="legacy-token-mchen"))
        session.add(
            Ticket(id=1, requester="hr@x.com", subject="s", body="b", status=TicketStatus.AWAITING_APPROVAL)
        )
        session.add(
            Approval(
                id=1, ticket_id=1, tool_name="identity_disable_user",
                tool_args={"username": "jsmith"}, status=ApprovalStatus.PENDING,
            )
        )

    transport = httpx.ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"X-API-Key": "test-api-key"}
    ) as ac:
        yield ac

    db_session_module._engine = None
    db_session_module._session_factory = None
    get_settings.cache_clear()


async def test_oidc_disabled_ignores_bearer_and_requires_reviewer_token(no_oidc_client):
    """With OIDC unconfigured, a Bearer header is inert — the request is
    evaluated exactly as before OIDC existed (X-Reviewer-Token required).
    """
    resp = await no_oidc_client.post(
        "/approvals/1/decide",
        json=_reject_body(),
        headers={"Authorization": f"Bearer {_mint_token('mchen')}"},
    )
    assert resp.status_code == 401  # missing X-Reviewer-Token, bearer ignored

    resp = await no_oidc_client.post(
        "/approvals/1/decide",
        json=_reject_body(),
        headers={"X-Reviewer-Token": "legacy-token-mchen"},
    )
    assert resp.status_code == 200


async def test_settings_fail_closed_without_audience(monkeypatch):
    monkeypatch.setenv("OIDC_ISSUER", ISSUER)
    monkeypatch.delenv("OIDC_AUDIENCE", raising=False)
    get_settings.cache_clear()
    try:
        assert get_settings().oidc_enabled is False
    finally:
        get_settings.cache_clear()


async def test_reviewer_lookup_uses_username_claim(oidc_client):
    """The reviewer resolved from the token's claim, not any header/body
    value — a token for mchen decides as mchen regardless of what else the
    request carries.
    """
    token = _mint_token("mchen")
    await oidc_client.post(
        "/approvals/1/decide", json=_reject_body(), headers={"Authorization": f"Bearer {token}"}
    )
    async with db_session_module.session_scope() as session:
        reviewer = await session.scalar(select(Reviewer).where(Reviewer.username == "mchen"))
        assert reviewer is not None
        approval = await session.get(Approval, 1)
        assert approval.reviewer == "mchen"
