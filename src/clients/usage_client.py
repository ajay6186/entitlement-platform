"""Usage warehouse (Snowflake-like) client."""
from src.clients.base import BaseUpstreamClient
from src.domain import Usage


class UsageClient(BaseUpstreamClient):
    system = "usage_warehouse"
    display_name = "usage warehouse"

    async def get(self, account_id: str) -> Usage | None:
        resp = await self.request("GET", f"/usage-warehouse/usage/{account_id}")
        resp.raise_for_status()
        rows = resp.json()["data"]
        if not rows:
            return None
        r = rows[0]
        return Usage(protected_tb=r["protected_tb"], api_calls=r["api_calls"], active_users=r["active_users"],
                     as_of=r["metric_date"])
