"""Thin REST client used by the MCP tools. It always sends the *user's* token: no service super-user exists."""
import httpx
import jwt

from mcp_server.guardrails import ToolError


class PlatformClient:
    def __init__(self, http: httpx.AsyncClient, access_token: str):
        self.http = http
        self.token = access_token
        claims = jwt.decode(access_token, options={"verify_signature": False})  # for audit labels only;
        self.user_id = claims.get("sub", "unknown")                             # the API verifies the token
        self.agent_id = (claims.get("act") or {}).get("sub")
        self.scopes = set(claims.get("scope", "").split())

    async def _call(self, method: str, path: str, **kwargs) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.token}", **kwargs.pop("headers", {})}
        try:
            resp = await self.http.request(method, path, headers=headers, timeout=10.0, **kwargs)
        except httpx.HTTPError as exc:
            raise ToolError("platform_unreachable", f"The platform API could not be reached ({type(exc).__name__}).")
        if resp.status_code >= 400:
            problem = resp.json() if resp.headers.get("content-type", "").startswith("application/") else {}
            slug = problem.get("type", "").rsplit("/", 1)[-1] or f"http_{resp.status_code}"
            raise ToolError(slug.replace("-", "_"), problem.get("detail") or problem.get("title") or "Request failed.",
                            status=resp.status_code)
        return resp

    async def search_cases(self, params: dict) -> dict:
        return (await self._call("GET", "/v1/cases", params=params)).json()

    async def get_case(self, case_id: str) -> tuple[dict, str]:
        resp = await self._call("GET", f"/v1/cases/{case_id}")
        return resp.json(), resp.headers.get("etag", "")

    async def get_customer(self, customer_id: str) -> dict:
        return (await self._call("GET", f"/v1/customers/{customer_id}")).json()

    async def add_comment(self, case_id: str, body: str, idempotency_key: str) -> tuple[dict, bool]:
        resp = await self._call("POST", f"/v1/cases/{case_id}/comments", json={"body": body},
                                headers={"Idempotency-Key": idempotency_key})
        return resp.json(), resp.headers.get("idempotent-replayed") == "true"

    async def escalate(self, case_id: str, reason: str, if_match: str) -> dict:
        return (await self._call("POST", f"/v1/cases/{case_id}/escalate", json={"reason": reason},
                                 headers={"If-Match": if_match})).json()
