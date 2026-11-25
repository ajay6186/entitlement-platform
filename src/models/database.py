"""Async SQLAlchemy engine/session. SQLite locally; the schema is PostgreSQL-compatible."""
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def make_engine(url: str) -> AsyncEngine:
    if url.startswith("sqlite") and ":///" in url and ":memory:" not in url:
        Path(url.split(":///", 1)[1]).parent.mkdir(parents=True, exist_ok=True)
    return create_async_engine(url, future=True)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    from src.models import tables  # noqa: F401  (register tables)
    # Production would use Alembic migrations; create_all keeps local start-up to one command.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session
