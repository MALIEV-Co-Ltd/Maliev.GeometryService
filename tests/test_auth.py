"""
Tests for src/infrastructure/auth.py — ServiceAccountTokenProvider.
Verifies JWT generation (RS256 + HS256), caching, claim correctness, and error handling.
"""

import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from src.infrastructure.auth import ServiceAccountTokenProvider


# ── Helpers ──────────────────────────────────────────────────────────────────

SECURITY_KEY = "a-very-long-test-secret-key-at-least-32-chars!"
ISSUER = "https://api.test.maliev.com"
AUDIENCE = "https://api.test.maliev.com"


def _generate_test_rsa_keypair() -> tuple[str, str]:
    """Return (private_key_b64_pem, public_key_pem) for test RS256 signing."""
    import base64

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )

    return base64.b64encode(private_pem).decode("utf-8"), public_pem


RSA_PRIVATE_B64, RSA_PUBLIC_PEM = _generate_test_rsa_keypair()


def _reload_provider(
    monkeypatch: pytest.MonkeyPatch,
    *,
    security_key: str = "",
    private_key_b64: str = "",
    issuer: str = ISSUER,
    audience: str = AUDIENCE,
) -> ServiceAccountTokenProvider:
    """Reload settings and auth module with the given env vars, return fresh provider."""
    monkeypatch.setenv("JWT_SECURITY_KEY", security_key)
    monkeypatch.setenv("JWT_PRIVATE_KEY", private_key_b64)
    monkeypatch.setenv("JWT_ISSUER", issuer)
    monkeypatch.setenv("JWT_AUDIENCE", audience)

    import importlib

    import src.core.config as cfg
    import src.infrastructure.auth as auth_module

    importlib.reload(cfg)
    importlib.reload(auth_module)
    return auth_module.ServiceAccountTokenProvider()


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def hs256_provider(monkeypatch: pytest.MonkeyPatch) -> ServiceAccountTokenProvider:
    """Provider configured for HS256 signing (no RSA key)."""
    return _reload_provider(monkeypatch, security_key=SECURITY_KEY)


@pytest.fixture
def rs256_provider(monkeypatch: pytest.MonkeyPatch) -> ServiceAccountTokenProvider:
    """Provider configured for RS256 signing."""
    return _reload_provider(monkeypatch, private_key_b64=RSA_PRIVATE_B64)


# ── HS256 token generation ────────────────────────────────────────────────────


def test_hs256_get_token_returns_string(
    hs256_provider: ServiceAccountTokenProvider,
) -> None:
    assert isinstance(hs256_provider.get_token(), str)
    assert len(hs256_provider.get_token()) > 0


def test_hs256_token_uses_correct_algorithm(
    hs256_provider: ServiceAccountTokenProvider,
) -> None:
    header = jwt.get_unverified_header(hs256_provider.get_token())
    assert header["alg"] == "HS256"


def test_hs256_token_is_verifiable(hs256_provider: ServiceAccountTokenProvider) -> None:
    token = hs256_provider.get_token()
    decoded = jwt.decode(token, SECURITY_KEY, algorithms=["HS256"], audience=AUDIENCE)
    assert decoded["sub"] == "system:service:geometry"


def test_hs256_token_contains_required_claims(
    hs256_provider: ServiceAccountTokenProvider,
) -> None:
    token = hs256_provider.get_token()
    decoded = jwt.decode(token, SECURITY_KEY, algorithms=["HS256"], audience=AUDIENCE)
    assert decoded["service_name"] == "GeometryService"
    assert decoded["user_type"] == "service"
    assert decoded["role"] == "service-account"
    assert decoded["purpose"] == "artifact-upload"
    assert decoded["permissions"] == "*"
    assert decoded["iss"] == ISSUER
    assert decoded["aud"] == AUDIENCE


def test_hs256_token_expiry_is_one_hour(
    hs256_provider: ServiceAccountTokenProvider,
) -> None:
    token = hs256_provider.get_token()
    decoded = jwt.decode(token, SECURITY_KEY, algorithms=["HS256"], audience=AUDIENCE)
    assert decoded["exp"] - decoded["iat"] == 3600


# ── RS256 token generation ────────────────────────────────────────────────────


def test_rs256_get_token_returns_string(
    rs256_provider: ServiceAccountTokenProvider,
) -> None:
    assert isinstance(rs256_provider.get_token(), str)


def test_rs256_token_uses_correct_algorithm(
    rs256_provider: ServiceAccountTokenProvider,
) -> None:
    header = jwt.get_unverified_header(rs256_provider.get_token())
    assert header["alg"] == "RS256"


def test_rs256_token_is_verifiable_with_public_key(
    rs256_provider: ServiceAccountTokenProvider,
) -> None:
    token = rs256_provider.get_token()
    decoded = jwt.decode(token, RSA_PUBLIC_PEM, algorithms=["RS256"], audience=AUDIENCE)
    assert decoded["sub"] == "system:service:geometry"


def test_rs256_token_contains_required_claims(
    rs256_provider: ServiceAccountTokenProvider,
) -> None:
    token = rs256_provider.get_token()
    decoded = jwt.decode(token, RSA_PUBLIC_PEM, algorithms=["RS256"], audience=AUDIENCE)
    assert decoded["service_name"] == "GeometryService"
    assert decoded["user_type"] == "service"
    assert decoded["role"] == "service-account"
    assert decoded["permissions"] == "*"


def test_rs256_not_verifiable_with_wrong_key(
    rs256_provider: ServiceAccountTokenProvider,
) -> None:
    """RS256 token must not verify with a different public key."""
    _, wrong_public = _generate_test_rsa_keypair()
    token = rs256_provider.get_token()
    with pytest.raises(jwt.exceptions.InvalidSignatureError):
        jwt.decode(token, wrong_public, algorithms=["RS256"], audience=AUDIENCE)


# ── RS256 preferred over HS256 ────────────────────────────────────────────────


def test_rs256_preferred_when_both_keys_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When both keys are set, RS256 is used (matches Maliev platform preference)."""
    provider = _reload_provider(
        monkeypatch, private_key_b64=RSA_PRIVATE_B64, security_key=SECURITY_KEY
    )
    header = jwt.get_unverified_header(provider.get_token())
    assert header["alg"] == "RS256"


# ── Caching ───────────────────────────────────────────────────────────────────


def test_token_is_cached(hs256_provider: ServiceAccountTokenProvider) -> None:
    assert hs256_provider.get_token() == hs256_provider.get_token()


def test_token_refreshes_when_expired(
    hs256_provider: ServiceAccountTokenProvider,
) -> None:
    hs256_provider.get_token()
    hs256_provider._token_expiry = time.monotonic() - 1
    hs256_provider.get_token()
    assert hs256_provider._token_expiry > time.monotonic()


def test_token_refresh_buffer_is_respected(
    hs256_provider: ServiceAccountTokenProvider,
) -> None:
    hs256_provider.get_token()
    assert hs256_provider._token_expiry > time.monotonic() + 3500


# ── Error handling ────────────────────────────────────────────────────────────


def test_raises_when_no_key_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """RuntimeError raised when neither JWT_PRIVATE_KEY nor JWT_SECURITY_KEY is set."""
    provider = _reload_provider(monkeypatch, security_key="", private_key_b64="")
    with pytest.raises(
        RuntimeError, match="Neither JWT_PRIVATE_KEY nor JWT_SECURITY_KEY"
    ):
        provider.get_token()
