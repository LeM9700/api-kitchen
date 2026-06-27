import asyncio
import sys
from logging.config import fileConfig

# asyncpg is incompatible with Windows ProactorEventLoop (Python 3.8+).
# Force SelectorEventLoop for migrations on Windows.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
from app.core.database import Base
from app.modules.auth import models as auth_models  # noqa: F401
from app.modules.catalog.allergen import allergen_models as catalog_allergen_models  # noqa: F401
from app.modules.catalog.image import image_model as catalog_image_model  # noqa: F401
from app.modules.catalog import models as catalog_models  # noqa: F401
from app.modules.delivery import models as delivery_models  # noqa: F401
from app.modules.loyalty.account import models as loyalty_account_models  # noqa: F401
from app.modules.loyalty.config import models as loyalty_config_models  # noqa: F401
from app.modules.orders import models as orders_models  # noqa: F401
from app.modules.payments import models as payments_models  # noqa: F401
from app.modules.promotions import models as promotions_models  # noqa: F401
from app.modules.stock import models as stock_models  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = create_async_engine(settings.database_url)
    async with connectable.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: context.configure(
                connection=sync_connection,
                target_metadata=target_metadata,
            )
        )
        await connection.run_sync(lambda _: context.run_migrations())
    await connectable.dispose()


def run() -> None:
    if context.is_offline_mode():
        run_migrations_offline()
    else:
        asyncio.run(run_migrations_online())


run()
