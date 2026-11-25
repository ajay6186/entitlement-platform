from fastapi import APIRouter, Depends, Path

from src.core.security import Principal, get_principal, require
from src.dependencies import Container, get_container
from src.schemas.api import CustomerOut

router = APIRouter(prefix="/v1/customers", tags=["customers"])


@router.get("/{customer_id}", response_model=CustomerOut, response_model_exclude_unset=True)
async def get_customer(customer_id: str = Path(..., pattern=r"^ACC-[0-9]{3,10}$"),
                       principal: Principal = Depends(get_principal),
                       c: Container = Depends(get_container)):
    """Aggregated customer view: entitlement (required) + usage (optional; degraded when unavailable)."""
    require(principal, "customers:read")
    return await c.customer_service.get_customer(customer_id, principal)
