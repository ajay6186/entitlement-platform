"""Customer aggregation: entitlement (required) + usage (optional, degrades gracefully)."""
import asyncio
import logging

from src.clients.base import UpstreamUnavailable
from src.clients.entitlement_client import EntitlementClient
from src.clients.usage_client import UsageClient
from src.core.clock import business_now
from src.core.errors import not_found, upstream_unavailable
from src.core.security import Principal

log = logging.getLogger(__name__)


class CustomerService:
    def __init__(self, entitlements: EntitlementClient, usage: UsageClient):
        self.entitlements, self.usage = entitlements, usage

    async def get_customer(self, customer_id: str, principal: Principal) -> dict:
        # Fetch both sources concurrently; the slower one bounds latency, not the sum.
        ent, usage = await asyncio.gather(self.entitlements.get(customer_id), self.usage.get(customer_id),
                                          return_exceptions=True)
        if isinstance(ent, UpstreamUnavailable):
            raise upstream_unavailable("entitlement system")  # no entitlement => no meaningful customer view
        if isinstance(ent, BaseException):
            raise ent
        if ent is None:
            raise not_found("Customer", customer_id)

        degraded_sources: list[str] = []
        if isinstance(usage, BaseException):
            log.warning("usage unavailable, returning degraded customer view",
                        extra={"customer_id": customer_id, "error": str(usage)})
            degraded_sources.append("usage")
            usage = None

        view = {
            "customer_id": ent.account_id, "name": ent.name, "support_tier": ent.support_tier,
            "contract_start": ent.contract_start, "renewal_date": ent.contract_end,
            "days_until_renewal": (ent.contract_end - business_now().date()).days,
            "usage": usage.model_dump() if usage else None,
            "degraded": bool(degraded_sources), "degraded_sources": degraded_sources, "redacted_fields": [],
        }
        # Field-level authorization: financial data only for Finance.
        if principal.has("customers:financials"):
            view["arr_usd"] = ent.arr_usd
        else:
            view["redacted_fields"].append("arr_usd")
        return view
