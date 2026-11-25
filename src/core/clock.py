"""Two clocks on purpose: business time (pinnable for reproducible evals) and wall time (transport, audit)."""
from datetime import UTC, datetime

from src.config import get_settings


def business_now() -> datetime:
    fixed = get_settings().fixed_now
    if fixed is not None:
        return fixed if fixed.tzinfo else fixed.replace(tzinfo=UTC)
    return datetime.now(UTC)


def wall_now() -> datetime:
    return datetime.now(UTC)
