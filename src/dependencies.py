"""Builds upstream clients/services once per app and exposes them as FastAPI dependencies."""
from dataclasses import dataclass

import httpx
from fastapi import Request

from src.clients.base import CircuitBreaker
from src.clients.case_client import CaseClient
from src.clients.entitlement_client import EntitlementClient
from src.clients.usage_client import UsageClient
from src.config import Settings
from src.services.case_service import CaseService
from src.services.customer_service import CustomerService


@dataclass
class Container:
    upstream_http: httpx.AsyncClient
    breakers: dict[str, CircuitBreaker]
    case_client: CaseClient
    entitlement_client: EntitlementClient
    usage_client: UsageClient
    customer_service: CustomerService
    case_service: CaseService


def build_container(settings: Settings, upstream_http: httpx.AsyncClient) -> Container:
    breakers = {name: CircuitBreaker(name, settings.breaker_failure_threshold, settings.breaker_reset_seconds)
                for name in ("case_system", "entitlement_system", "usage_warehouse")}
    cases = CaseClient(upstream_http, settings, breakers["case_system"])
    ents = EntitlementClient(upstream_http, settings, breakers["entitlement_system"])
    usage = UsageClient(upstream_http, settings, breakers["usage_warehouse"])
    return Container(upstream_http, breakers, cases, ents, usage,
                     CustomerService(ents, usage), CaseService(cases, ents))


def get_container(request: Request) -> Container:
    return request.app.state.container
