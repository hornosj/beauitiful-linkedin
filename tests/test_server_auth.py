"""Testes do controle de acesso por conta do sidecar (JWT/Supabase).

Sem rede: geramos um par de chaves RSA local e injetamos um resolver de chave
no ``TokenVerifier``, validando tokens assinados localmente como se viessem do
Supabase (sem bater no JWKS real).
"""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from beautiful_linkedin.server.app import build_app
from beautiful_linkedin.server.auth import (
    AuthConfig,
    AuthError,
    TokenVerifier,
    load_auth_config,
)

ISSUER = "https://proj.supabase.co/auth/v1"
AUDIENCE = "authenticated"


@pytest.fixture(scope="module")
def rsa_keys() -> tuple[bytes, bytes]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


def _make_token(private_pem: bytes, *, exp_offset: int = 3600, **overrides) -> str:
    now = int(time.time())
    claims = {
        "sub": "user-123",
        "aud": AUDIENCE,
        "iss": ISSUER,
        "iat": now,
        "exp": now + exp_offset,
        "role": "authenticated",
        "email": "colaborador@empresa.com",
    }
    claims.update(overrides)
    return jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": "test"})


def _verifier(public_pem: bytes, *, leeway: int = 60) -> TokenVerifier:
    config = AuthConfig(
        enabled=True,
        jwks_url="https://proj.supabase.co/auth/v1/.well-known/jwks.json",
        issuer=ISSUER,
        audience=AUDIENCE,
        leeway_seconds=leeway,
    )
    # Resolver injetado: ignora o JWKS de rede e devolve a chave pública local.
    return TokenVerifier(config, signing_key_resolver=lambda _token, _alg: public_pem)


def _client(tmp_path, **build_kwargs) -> TestClient:
    app = build_app(saved_leads_path=str(tmp_path / "leads.sqlite"), **build_kwargs)
    return TestClient(app)


# --------------------------------------------------------------------------- #
# load_auth_config
# --------------------------------------------------------------------------- #


def test_config_disabled_without_env() -> None:
    config = load_auth_config(env={})
    assert config.enabled is False
    assert config.jwks_url is None


def test_config_derives_jwks_and_issuer_from_supabase_url() -> None:
    config = load_auth_config(env={"SUPABASE_URL": "https://abc.supabase.co/"})
    assert config.enabled is True
    assert config.jwks_url == "https://abc.supabase.co/auth/v1/.well-known/jwks.json"
    assert config.issuer == "https://abc.supabase.co/auth/v1"
    assert config.audience == "authenticated"


def test_config_require_auth_without_verifier_raises() -> None:
    with pytest.raises(RuntimeError):
        load_auth_config(env={"BEAUTIFUL_LINKEDIN_REQUIRE_AUTH": "1"})


def test_config_require_auth_false_disables_even_with_url() -> None:
    config = load_auth_config(
        env={
            "SUPABASE_URL": "https://abc.supabase.co",
            "BEAUTIFUL_LINKEDIN_REQUIRE_AUTH": "0",
        }
    )
    assert config.enabled is False


# --------------------------------------------------------------------------- #
# TokenVerifier
# --------------------------------------------------------------------------- #


def test_verifier_accepts_valid_token(rsa_keys) -> None:
    private_pem, public_pem = rsa_keys
    verifier = _verifier(public_pem)
    claims = verifier.verify(_make_token(private_pem))
    assert claims["sub"] == "user-123"
    assert claims["email"] == "colaborador@empresa.com"


def test_verifier_rejects_expired_token(rsa_keys) -> None:
    private_pem, public_pem = rsa_keys
    verifier = _verifier(public_pem, leeway=0)
    with pytest.raises(AuthError):
        verifier.verify(_make_token(private_pem, exp_offset=-10))


def test_verifier_rejects_wrong_audience(rsa_keys) -> None:
    private_pem, public_pem = rsa_keys
    verifier = _verifier(public_pem)
    with pytest.raises(AuthError):
        verifier.verify(_make_token(private_pem, aud="anon"))


def test_verifier_rejects_wrong_issuer(rsa_keys) -> None:
    private_pem, public_pem = rsa_keys
    verifier = _verifier(public_pem)
    with pytest.raises(AuthError):
        verifier.verify(_make_token(private_pem, iss="https://evil.example/auth/v1"))


def test_verifier_rejects_garbage(rsa_keys) -> None:
    _private_pem, public_pem = rsa_keys
    verifier = _verifier(public_pem)
    with pytest.raises(AuthError):
        verifier.verify("not-a-jwt")


# --------------------------------------------------------------------------- #
# Middleware end-to-end (FastAPI)
# --------------------------------------------------------------------------- #


def test_health_open_when_auth_disabled(tmp_path) -> None:
    client = _client(tmp_path, auth_config=AuthConfig(enabled=False))
    assert client.get("/health").status_code == 200
    # Taxonomias acessíveis sem token quando a auth está desligada.
    assert client.get("/taxonomies").status_code == 200


def test_protected_route_401_without_token(tmp_path, rsa_keys) -> None:
    _private_pem, public_pem = rsa_keys
    client = _client(tmp_path, token_verifier=_verifier(public_pem))
    # /health continua aberto (Electron usa para detectar o boot do sidecar).
    assert client.get("/health").status_code == 200
    # Rota protegida sem Authorization → 401.
    resp = client.get("/taxonomies")
    assert resp.status_code == 401


def test_protected_route_ok_with_valid_token(tmp_path, rsa_keys) -> None:
    private_pem, public_pem = rsa_keys
    client = _client(tmp_path, token_verifier=_verifier(public_pem))
    token = _make_token(private_pem)
    resp = client.get("/taxonomies", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200


def test_protected_route_401_with_expired_token(tmp_path, rsa_keys) -> None:
    private_pem, public_pem = rsa_keys
    client = _client(tmp_path, token_verifier=_verifier(public_pem, leeway=0))
    token = _make_token(private_pem, exp_offset=-120)
    resp = client.get(
        "/taxonomies", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 401
