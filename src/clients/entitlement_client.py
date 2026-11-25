"""Entitlement system (NetSuite-like) client."""
from src.clients.base import BaseUpstreamClient
from src.domain import Entitlement


def _entitlement(raw: dict) -> Entitlement:
    return Entitlement(account_id=raw["account_id"], name=raw["name"], support_tier=raw["support_tier"],
                       contract_start=raw["contract_start"], contract_end=raw["contract_end"],
                       arr_usd=int(float(raw["arr_usd"])))


class EntitlementClient(BaseUpstreamClient):
    system = "entitlement_system"
    display_name = "entitlement system"

    async def get(self, account_id: str) -> Entitlement | None:
        resp = await self.request("GET", f"/entitlement-system/accounts/{account_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return _entitlement(resp.json())

    async def get_many(self, account_ids: list[str]) -> dict[str, Entitlement]:
        if not account_ids:
            return {}
        resp = await self.request("GET", "/entitlement-system/accounts",
                                  params={"ids": ",".join(sorted(set(account_ids)))})
        resp.raise_for_status()
        return {e.account_id: e for e in map(_entitlement, resp.json()["items"])}
