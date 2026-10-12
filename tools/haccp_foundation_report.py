"""Read-only migration diagnostic: python tools/haccp_foundation_report.py --tenant SLUG."""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import engine, tenant_schema_name
from app.modules.haccp.foundation_diagnostics import inspect_foundations


async def main(slug: str) -> None:
    schema = tenant_schema_name(slug)
    try:
        async with engine.connect() as connection:
            async with connection.begin():
                await connection.execute(text("SET TRANSACTION READ ONLY"))
                exists = await connection.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM public.tenants WHERE slug=:slug)"
                    ),
                    {"slug": slug},
                )
                if not exists:
                    raise ValueError("Unknown tenant")
                quoted = connection.dialect.identifier_preparer.quote_schema(schema)
                await connection.execute(
                    text("SELECT set_config('search_path', :path, true)"),
                    {"path": quoted},
                )
                async with AsyncSession(bind=connection) as session:
                    report = await inspect_foundations(session)
                    print(report.model_dump_json(indent=2))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", required=True)
    args = parser.parse_args()
    asyncio.run(main(args.tenant))
