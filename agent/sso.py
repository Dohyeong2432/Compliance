"""SSO/JWT verification and SessionContext derivation.

This module is the single choke point through which a department code can
enter the system. build_session_context() is the only function that
produces a SessionContext, and the dept it carries always comes from the
verified 'dept' claim inside a signed token — never from a request body,
header, query string, or anything the LLM decides to pass a tool. That is
what makes the RBAC/Chinese-wall guarantee hold: nobody downstream (agent
harness, tool dispatcher, retriever) can widen their own access, because
none of them ever have a way to set dept themselves.

If SSO is not configured, this module refuses to authenticate anyone
(fail-closed) rather than falling back to an unauthenticated or
default-department session. The API layer turns that into a 501 for every
request rather than silently running open.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import jwt

DEPT_CLAIM = "dept"


class SSOConfigError(RuntimeError):
    """SSO is not configured, or configured with an unsupported algorithm."""


class SSOAuthError(RuntimeError):
    """The presented token failed verification or is missing required claims."""


@dataclass(frozen=True)
class SessionContext:
    user_id: str
    dept: str
    roles: tuple[str, ...] = ()
    claims: dict[str, Any] = field(default_factory=dict, compare=False)


@dataclass
class SSOConfig:
    algorithm: str  # "HS256" or "RS256"
    audience: str | None = None
    issuer: str | None = None
    hs256_secret: str | None = None
    jwks_client: Any = None  # jwt.PyJWKClient, kept as Any to avoid import at class-def time

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "SSOConfig | None":
        env = env if env is not None else os.environ

        algorithm = env.get("SSO_JWT_ALGORITHM")
        if not algorithm:
            return None
        algorithm = algorithm.upper()
        audience = env.get("SSO_JWT_AUDIENCE") or None
        issuer = env.get("SSO_JWT_ISSUER") or None

        if algorithm == "HS256":
            secret = env.get("SSO_JWT_SECRET")
            if not secret:
                return None
            return cls(algorithm="HS256", audience=audience, issuer=issuer, hs256_secret=secret)
        if algorithm == "RS256":
            jwks_url = env.get("SSO_JWT_JWKS_URL")
            if not jwks_url:
                return None
            return cls(algorithm="RS256", audience=audience, issuer=issuer, jwks_client=jwt.PyJWKClient(jwks_url))
        raise SSOConfigError(f"Unsupported SSO_JWT_ALGORITHM: {algorithm}")


def verify_token(token: str, config: SSOConfig) -> dict[str, Any]:
    options: dict[str, Any] = {"require": ["exp", "iat", "sub", DEPT_CLAIM]}
    decode_kwargs: dict[str, Any] = {"algorithms": [config.algorithm]}
    if config.audience:
        decode_kwargs["audience"] = config.audience
    else:
        options["verify_aud"] = False
    if config.issuer:
        decode_kwargs["issuer"] = config.issuer
    decode_kwargs["options"] = options

    try:
        if config.algorithm == "HS256":
            claims = jwt.decode(token, config.hs256_secret, **decode_kwargs)
        elif config.algorithm == "RS256":
            signing_key = config.jwks_client.get_signing_key_from_jwt(token)
            claims = jwt.decode(token, signing_key.key, **decode_kwargs)
        else:
            raise SSOConfigError(f"Unsupported algorithm: {config.algorithm}")
    except jwt.PyJWTError as exc:
        raise SSOAuthError(str(exc)) from exc
    return claims


def build_session_context(token: str, config: SSOConfig | None) -> SessionContext:
    if config is None:
        raise SSOConfigError("SSO is not configured; refusing to authenticate (fail-closed)")

    claims = verify_token(token, config)

    dept = claims.get(DEPT_CLAIM)
    if not dept or not isinstance(dept, str):
        raise SSOAuthError("token is missing a valid 'dept' claim")
    user_id = claims.get("sub")
    if not user_id:
        raise SSOAuthError("token is missing 'sub'")
    roles = tuple(claims.get("roles", []) or ())

    return SessionContext(user_id=user_id, dept=dept, roles=roles, claims=claims)
