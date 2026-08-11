import time

import jwt
import pytest

from agent.sso import SSOAuthError, SSOConfig, SSOConfigError, build_session_context

SECRET = "test-secret-key-that-is-long-enough-1234"


def _claims(**overrides):
    base = {"sub": "u1", "dept": "RETAIL", "iat": int(time.time()), "exp": int(time.time()) + 3600}
    base.update(overrides)
    return base


def test_valid_token_produces_session_context():
    config = SSOConfig(algorithm="HS256", hs256_secret=SECRET)
    token = jwt.encode(_claims(roles=["employee"]), SECRET, algorithm="HS256")

    session = build_session_context(token, config)

    assert session.user_id == "u1"
    assert session.dept == "RETAIL"
    assert session.roles == ("employee",)


def test_tampered_signature_is_rejected():
    config = SSOConfig(algorithm="HS256", hs256_secret=SECRET)
    token = jwt.encode(_claims(dept="IB"), "wrong-secret-entirely-different", algorithm="HS256")

    with pytest.raises(SSOAuthError):
        build_session_context(token, config)


def test_missing_dept_claim_is_rejected():
    config = SSOConfig(algorithm="HS256", hs256_secret=SECRET)
    claims = _claims()
    del claims["dept"]
    token = jwt.encode(claims, SECRET, algorithm="HS256")

    with pytest.raises(SSOAuthError):
        build_session_context(token, config)


def test_missing_sub_claim_is_rejected():
    config = SSOConfig(algorithm="HS256", hs256_secret=SECRET)
    claims = _claims()
    del claims["sub"]
    token = jwt.encode(claims, SECRET, algorithm="HS256")

    with pytest.raises(SSOAuthError):
        build_session_context(token, config)


def test_expired_token_is_rejected():
    config = SSOConfig(algorithm="HS256", hs256_secret=SECRET)
    token = jwt.encode(_claims(exp=int(time.time()) - 10), SECRET, algorithm="HS256")

    with pytest.raises(SSOAuthError):
        build_session_context(token, config)


def test_no_config_fails_closed():
    token = jwt.encode(_claims(), SECRET, algorithm="HS256")

    with pytest.raises(SSOConfigError):
        build_session_context(token, None)


def test_dept_cannot_be_overridden_by_unsigned_claims():
    """A dept claim can only ever come from a token that verifies against
    the configured secret -- there is no code path that reads dept from
    anywhere else."""
    config = SSOConfig(algorithm="HS256", hs256_secret=SECRET)
    forged = jwt.encode(_claims(dept="IB"), "attacker-controlled-secret", algorithm="HS256")

    with pytest.raises(SSOAuthError):
        build_session_context(forged, config)


def test_from_env_returns_none_when_unconfigured():
    assert SSOConfig.from_env({}) is None


def test_from_env_hs256_requires_secret():
    assert SSOConfig.from_env({"SSO_JWT_ALGORITHM": "HS256"}) is None
    config = SSOConfig.from_env({"SSO_JWT_ALGORITHM": "HS256", "SSO_JWT_SECRET": SECRET})
    assert config is not None
    assert config.algorithm == "HS256"


def test_from_env_rs256_requires_jwks_url():
    assert SSOConfig.from_env({"SSO_JWT_ALGORITHM": "RS256"}) is None


def test_from_env_rejects_unsupported_algorithm():
    with pytest.raises(SSOConfigError):
        SSOConfig.from_env({"SSO_JWT_ALGORITHM": "NONE"})
