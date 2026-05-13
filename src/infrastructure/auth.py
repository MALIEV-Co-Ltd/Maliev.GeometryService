import base64
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

from src.core.config import settings

logger = logging.getLogger(__name__)


class ServiceAccountTokenProvider:
    """Generates service-account JWTs for authenticating with other Maliev services.

    Signing strategy (matches Maliev.Aspire.ServiceDefaults):
    - If ``JWT_PRIVATE_KEY`` is set (Base64-encoded RSA PEM), signs with RS256.
      All services accept RS256 tokens validated against the shared public key.
    - Otherwise falls back to HS256 using ``JWT_SECURITY_KEY`` only in
      Development or Testing.

    Tokens are cached and refreshed automatically 60 s before expiry.
    """

    _TOKEN_EXPIRY_SECONDS: int = 3600
    _REFRESH_BUFFER_SECONDS: int = 60

    def __init__(self) -> None:
        self._cached_token: str | None = None
        self._token_expiry: float = 0.0

    def get_token(self) -> str:
        """Return a valid service-account JWT, regenerating when near expiry."""
        if self._cached_token and time.monotonic() < self._token_expiry:
            return self._cached_token

        self._cached_token = self._generate_token()
        self._token_expiry = (
            time.monotonic() + self._TOKEN_EXPIRY_SECONDS - self._REFRESH_BUFFER_SECONDS
        )
        return self._cached_token

    def _generate_token(self) -> str:
        """Generate a new signed JWT matching the Maliev service-account format."""
        now = datetime.now(timezone.utc)
        payload: dict[str, Any] = {
            "sub": "system:service:geometry",
            "service_name": "GeometryService",
            "user_type": "service",
            "role": "service-account",
            "purpose": "artifact-upload",
            "permissions": "*",
            "iss": settings.JWT_ISSUER,
            "aud": settings.JWT_AUDIENCE,
            "iat": now,
            "nbf": now,
            "exp": now + timedelta(seconds=self._TOKEN_EXPIRY_SECONDS),
        }

        # Prefer RSA — accepted by all services regardless of symmetric key config
        if settings.JWT_PRIVATE_KEY:
            return self._sign_rs256(payload)

        if settings.JWT_SECURITY_KEY and self._symmetric_signing_allowed():
            return self._sign_hs256(payload)

        if settings.JWT_SECURITY_KEY:
            raise RuntimeError(
                "JWT_PRIVATE_KEY is required for GeometryService service-account "
                "tokens outside Development or Testing."
            )

        raise RuntimeError(
            "Neither JWT_PRIVATE_KEY nor JWT_SECURITY_KEY is configured. "
            "At least one is required for service-to-service authentication."
        )

    def _sign_rs256(self, payload: dict[str, Any]) -> str:
        """Sign with RSA private key (RS256)."""
        # JWT_PRIVATE_KEY is Base64-encoded PEM (matches Jwt:PrivateKey)
        pem_bytes = base64.b64decode(settings.JWT_PRIVATE_KEY)
        pem = pem_bytes.decode("utf-8")
        token = jwt.encode(payload, pem, algorithm="RS256")
        logger.debug("Generated RS256 service-account JWT for GeometryService")
        return token

    def _sign_hs256(self, payload: dict[str, Any]) -> str:
        """Sign with HMAC-SHA256 symmetric key (HS256)."""
        token = jwt.encode(payload, settings.JWT_SECURITY_KEY, algorithm="HS256")
        logger.debug("Generated HS256 service-account JWT for GeometryService")
        return token

    def _symmetric_signing_allowed(self) -> bool:
        """Return whether HS256 signing is allowed for this environment."""
        environment = (
            settings.ASPNETCORE_ENVIRONMENT or settings.ENVIRONMENT
        ).lower()
        return environment in {"development", "testing", "test"}
