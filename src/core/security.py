"""Authentication (OAuth 2.0 bearer JWT) and scope-based authorization.

Locally, tokens are HS256 JWTs minted by /dev/token or scripts/mint_token.py. In production the same
claims would come from the corporate IdP (OIDC), validated as RS256 against its JWKS; only
`decode_token` changes.

Identity propagation for AI agents follows RFC 8693 token exchange: the *user* stays the subject
(`sub`) and the agent appears as the actor (`act.sub`). Authorization is always evaluated against the
user's scopes, so an agent can never do more than the human it acts for.
"""
import time
import uuid
from dataclasses import dataclass, field

import jwt
from fastapi import Request

from src.config import get_settings
from src.core.errors import forbidden, unauthenticated

SCOPES = {
    "cases:read": "Search and read cases.",
    "cases:comment": "Add comments to cases.",
    "cases:escalate": "Escalate cases (support leads only).",
    "customers:read": "Read aggregated customer entitlement and usage.",
    "customers:financials": "See financial fields such as ARR (Finance only).",
    "webhooks:manage": "Manage webhook subscriptions and inspect deliveries.",
    "audit:read": "Read audit records.",
}

# Demo personas. In production these are IdP groups mapped to scopes.
PERSONAS: dict[str, dict] = {
    "support_agent": {"sub": "u-support-agent-01", "name": "Sam Support",
                      "scopes": ["cases:read", "cases:comment", "customers:read"]},
    "support_lead": {"sub": "u-support-lead-01", "name": "Lee Lead",
                     "scopes": ["cases:read", "cases:comment", "cases:escalate", "customers:read"]},
    "finance_analyst": {"sub": "u-finance-01", "name": "Fran Finance",
                        "scopes": ["customers:read", "customers:financials"]},
    "platform_admin": {"sub": "u-platform-admin-01", "name": "Pat Platform",
                       "scopes": ["webhooks:manage", "audit:read", "cases:read"]},
}


@dataclass(frozen=True)
class Principal:
    sub: str
    scopes: frozenset[str] = field(default_factory=frozenset)
    agent_id: str | None = None  # set when an AI agent acts on the user's behalf
    name: str | None = None

    def has(self, scope: str) -> bool:
        return scope in self.scopes

    @property
    def rate_key(self) -> str:
        return f"{self.sub}|{self.agent_id or '-'}"


def issue_token(sub: str, scopes: list[str], *, name: str | None = None, agent_id: str | None = None,
                ttl_seconds: int = 3600) -> str:
    s = get_settings()
    now = int(time.time())
    claims = {"iss": s.jwt_issuer, "aud": s.jwt_audience, "sub": sub, "scope": " ".join(sorted(scopes)),
              "iat": now, "exp": now + ttl_seconds, "jti": uuid.uuid4().hex}
    if name:
        claims["name"] = name
    if agent_id:
        claims["act"] = {"sub": agent_id}
    return jwt.encode(claims, s.jwt_secret, algorithm="HS256")


def issue_persona_token(persona: str, agent_id: str | None = None, ttl_seconds: int = 3600) -> str:
    p = PERSONAS[persona]
    return issue_token(p["sub"], p["scopes"], name=p["name"], agent_id=agent_id, ttl_seconds=ttl_seconds)


def decode_token(token: str) -> Principal:
    s = get_settings()
    try:
        claims = jwt.decode(token, s.jwt_secret, algorithms=["HS256"], audience=s.jwt_audience,
                            issuer=s.jwt_issuer, options={"require": ["exp", "sub", "iss", "aud"]})
    except jwt.ExpiredSignatureError:
        raise unauthenticated("The access token has expired.")
    except jwt.PyJWTError:
        raise unauthenticated("The access token is invalid.")
    act = claims.get("act") or {}
    return Principal(sub=claims["sub"], scopes=frozenset(claims.get("scope", "").split()),
                     agent_id=act.get("sub"), name=claims.get("name"))


async def get_principal(request: Request) -> Principal:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise unauthenticated("Send an OAuth 2.0 bearer token in the Authorization header.")
    principal = decode_token(token.strip())
    request.state.principal = principal
    request.app.state.rate_limiter.check(request, principal)
    return principal


def require(principal: Principal, scope: str) -> None:
    if not principal.has(scope):
        raise forbidden(f"This operation requires the '{scope}' scope.")
