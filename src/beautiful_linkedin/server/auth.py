"""Autenticação JWT do sidecar local (controle de acesso por conta).

O app desktop entrega o sidecar para a máquina do usuário final, então NÃO
podemos embutir nenhum segredo de assinatura. Validamos os access tokens
emitidos pelo Supabase usando o JWKS **público** do projeto (chaves
assimétricas ES256/RS256) — a chave privada nunca sai do Supabase. Assim, mesmo
que alguém abra o binário, não consegue forjar um token válido.

Existe um caminho legado HS256 (segredo simétrico compartilhado) apenas para
implantações SERVIDOR (sidecar rodando num servidor que você controla) — ele
nunca deve ser usado num build desktop distribuído, pois exigiria embarcar o
segredo no cliente.

A autenticação é opt-in: sem ``SUPABASE_URL``/``SUPABASE_JWKS_URL`` configurado,
o verificador fica desligado e toda requisição passa (padrão de dev/teste).
Defina ``BEAUTIFUL_LINKEDIN_REQUIRE_AUTH=1`` para *fail-closed* — o sidecar se
recusa a subir sem login configurado, evitando deixar a API aberta por engano.

Revogação: como o token de acesso é curto (~1h, configurável no Supabase),
banir/deletar o usuário no painel faz o refresh falhar; em no máximo ~1h o
token expira e o sidecar passa a recusar (401). É o caminho de "cortar acesso
de quem saiu da empresa".
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Mapping

import jwt
from jwt import PyJWKClient
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}

# Rotas que o Electron precisa alcançar SEM token. ``/health`` é consultado pelo
# processo principal do Electron para detectar que o sidecar subiu — isso
# acontece ANTES de qualquer login, então precisa ficar aberto.
EXEMPT_PATHS = frozenset(
    {"/health", "/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect"}
)

# Algoritmos assimétricos aceitos (chave pública via JWKS). Supabase usa ES256
# (ECC P-256) por padrão nos projetos com JWT Signing Keys; RS256 é aceito para
# projetos migrados de RSA.
ASYMMETRIC_ALGS = ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "EdDSA"]


class AuthError(Exception):
    """Token ausente/inválido. Mapeado para HTTP 401 pela middleware."""


@dataclass(frozen=True)
class AuthConfig:
    """Configuração resolvida a partir do ambiente (ver :func:`load_auth_config`)."""

    enabled: bool
    jwks_url: str | None = None
    issuer: str | None = None
    audience: str = "authenticated"
    hs256_secret: str | None = None
    leeway_seconds: int = 60


def _norm(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def load_auth_config(env: Mapping[str, str] | None = None) -> AuthConfig:
    """Lê as variáveis de ambiente do Supabase e decide se a auth fica ativa.

    Variáveis:
      - ``SUPABASE_URL``: base do projeto (ex.: ``https://abc.supabase.co``).
        A partir dela derivamos o JWKS e o issuer automaticamente.
      - ``SUPABASE_JWKS_URL``: sobrescreve a URL do JWKS (opcional).
      - ``SUPABASE_JWT_ISSUER`` / ``SUPABASE_JWT_AUD``: sobrescritas (opcional).
      - ``SUPABASE_JWT_SECRET``: APENAS para sidecar em servidor (HS256). NÃO use
        em build desktop distribuído.
      - ``BEAUTIFUL_LINKEDIN_REQUIRE_AUTH``: ``1`` força exigir login (fail-closed);
        ``0`` força desligar; ausente = liga se houver verificador configurado.
    """
    env = env if env is not None else os.environ
    supabase_url = _norm(env.get("SUPABASE_URL"))
    jwks_url = _norm(env.get("SUPABASE_JWKS_URL"))
    issuer = _norm(env.get("SUPABASE_JWT_ISSUER"))
    audience = _norm(env.get("SUPABASE_JWT_AUD")) or "authenticated"
    hs256_secret = _norm(env.get("SUPABASE_JWT_SECRET"))

    if supabase_url:
        base = supabase_url.rstrip("/")
        jwks_url = jwks_url or f"{base}/auth/v1/.well-known/jwks.json"
        issuer = issuer or f"{base}/auth/v1"

    has_verifier = bool(jwks_url or hs256_secret)

    require_raw = _norm(env.get("BEAUTIFUL_LINKEDIN_REQUIRE_AUTH"))
    require_lower = require_raw.lower() if require_raw else None
    if require_lower in _TRUTHY:
        if not has_verifier:
            raise RuntimeError(
                "BEAUTIFUL_LINKEDIN_REQUIRE_AUTH está ativo, mas faltam "
                "SUPABASE_URL/SUPABASE_JWKS_URL (ou SUPABASE_JWT_SECRET). "
                "Configure o Supabase ou desligue a exigência de login."
            )
        enabled = True
    elif require_lower in _FALSY:
        enabled = False
    else:
        enabled = has_verifier

    leeway_raw = _norm(env.get("BEAUTIFUL_LINKEDIN_AUTH_LEEWAY"))
    try:
        leeway = int(leeway_raw) if leeway_raw else 60
    except ValueError:
        leeway = 60

    return AuthConfig(
        enabled=enabled,
        jwks_url=jwks_url,
        issuer=issuer,
        audience=audience,
        hs256_secret=hs256_secret,
        leeway_seconds=leeway,
    )


# (token, alg) -> key material (str para HS256, objeto de chave para assimétrico)
SigningKeyResolver = Callable[[str, str], Any]


class TokenVerifier:
    """Valida JWTs do Supabase. A rede (JWKS) só é tocada num *cache miss* de chave.

    O ``signing_key_resolver`` é injetável para testes — assim os testes geram um
    par de chaves local e validam tokens sem bater na rede.
    """

    def __init__(
        self,
        config: AuthConfig,
        signing_key_resolver: SigningKeyResolver | None = None,
    ) -> None:
        self.config = config
        self._resolver = signing_key_resolver or self._default_resolver
        self._jwk_client: PyJWKClient | None = None

    def _client(self) -> PyJWKClient:
        if self._jwk_client is None:
            if not self.config.jwks_url:
                raise AuthError("Verificação assimétrica sem JWKS configurado.")
            # PyJWKClient mantém cache em memória e renova quando vê um kid novo.
            self._jwk_client = PyJWKClient(self.config.jwks_url, lifespan=3600)
        return self._jwk_client

    def _default_resolver(self, token: str, alg: str) -> Any:
        if alg.startswith("HS"):
            if not self.config.hs256_secret:
                raise AuthError("Token HS256, mas nenhum segredo está configurado.")
            return self.config.hs256_secret
        try:
            return self._client().get_signing_key_from_jwt(token).key
        except AuthError:
            raise
        except Exception as exc:  # rede / kid desconhecido / token malformado
            raise AuthError(f"Não foi possível obter a chave de verificação: {exc}")

    def verify(self, token: str) -> dict:
        """Valida assinatura, expiração, audience e issuer. Devolve as claims."""
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise AuthError(f"Cabeçalho do token inválido: {exc}")
        alg = header.get("alg", "")
        if not alg:
            raise AuthError("Token sem algoritmo no cabeçalho.")
        algorithms = [alg] if alg.startswith("HS") else ASYMMETRIC_ALGS
        key = self._resolver(token, alg)
        kwargs: dict[str, Any] = {
            "algorithms": algorithms,
            "audience": self.config.audience,
            "leeway": self.config.leeway_seconds,
            "options": {"require": ["exp"]},
        }
        if self.config.issuer:
            kwargs["issuer"] = self.config.issuer
        try:
            claims = jwt.decode(token, key, **kwargs)
        except jwt.ExpiredSignatureError:
            raise AuthError("Sessão expirada. Faça login novamente.")
        except jwt.PyJWTError as exc:
            raise AuthError(f"Token inválido: {exc}")
        return claims


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip() or None
    return None


def install_config_lock(app: Any, message: str) -> None:
    """Trava a API quando a auth é EXIGIDA mas está mal configurada.

    Em vez de derrubar o sidecar (o cliente veria apenas "Sidecar offline", sem
    causa), subimos o processo travado: ``/health`` (e demais rotas isentas)
    respondem para o Electron detectar que o backend está vivo, mas toda rota
    protegida devolve 503 com a causa da má configuração. É *fail-closed* (a API
    continua inacessível) E diagnosticável (a mensagem aparece em vez do erro
    opaco). Registrada ANTES do CORS, igual à :func:`install_auth`.
    """

    @app.middleware("http")
    async def _config_lock(request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.method == "OPTIONS" or request.url.path in EXEMPT_PATHS:
            return await call_next(request)
        return JSONResponse(
            {"detail": f"Login obrigatório, mas mal configurado: {message}"},
            status_code=503,
        )


def install_auth(app: Any, verifier: TokenVerifier) -> None:
    """Registra a middleware de auth.

    DEVE ser chamada ANTES de ``app.add_middleware(CORSMiddleware, ...)`` para que
    o CORS permaneça a camada mais externa — assim até as respostas 401 carregam
    os cabeçalhos CORS e o renderer consegue lê-las (em vez de ver um erro de
    rede opaco).
    """

    @app.middleware("http")
    async def _verify_auth(request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.method == "OPTIONS" or request.url.path in EXEMPT_PATHS:
            return await call_next(request)
        token = _bearer_token(request.headers.get("authorization"))
        if not token:
            return JSONResponse({"detail": "Não autenticado."}, status_code=401)
        try:
            claims = await run_in_threadpool(verifier.verify, token)
        except AuthError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=401)
        request.state.user = claims
        return await call_next(request)
