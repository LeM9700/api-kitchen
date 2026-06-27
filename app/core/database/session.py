from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings
from app.core.http.errors import AppError

engine = create_async_engine(settings.database_url, echo=False)
public_session_factory = async_sessionmaker(engine, expire_on_commit=False)

TENANT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}[a-z0-9]$|^[a-z0-9]$")


class Base(DeclarativeBase):
    pass


def tenant_schema_name(tenant_slug: str) -> str:
    if not TENANT_SLUG_RE.fullmatch(tenant_slug):
        raise AppError("INVALID_SLUG", "Invalid tenant slug", 400, "tenant_slug")
    return f"tenant_{tenant_slug}"


@asynccontextmanager
async def get_public_session() -> AsyncIterator[AsyncSession]:
    async with public_session_factory() as session:
        yield session


@asynccontextmanager
async def get_tenant_session(tenant_slug: str) -> AsyncIterator[AsyncSession]:
    schema = tenant_schema_name(tenant_slug)
    async with public_session_factory() as session:
        await session.execute(text(f'SET search_path TO "{schema}", public'))
        yield session
