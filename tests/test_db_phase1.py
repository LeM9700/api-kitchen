"""Phase 1 — DB & Migrations test stubs.

All tests in this file start RED. Each implementation task in Phase 1
turns its corresponding test(s) GREEN.

Requirement coverage:
  DB-01 → test_check_constraint_rejects_negative_qty
  DB-02 → test_stock_movement_user_id_nullable, test_stock_movement_model_has_user_id
  DB-03 → test_extra_ingredient_importable
  DB-04 → test_tenant_config_cooldown_default
  DB-05 → test_product_stock_removed
"""

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

# ---------------------------------------------------------------------------
# DB-01: CHECK constraint on ingredients.current_qty >= 0
# ---------------------------------------------------------------------------


async def test_check_constraint_rejects_negative_qty(db_session):
    """Inserting current_qty = -1 must raise an IntegrityError at commit time.

    RED until migration 0024 is applied.
    """
    with pytest.raises(IntegrityError):
        await db_session.execute(
            sa.text(
                "INSERT INTO ingredients (name, unit, current_qty, alert_threshold) "
                "VALUES ('test_neg', 'kg', -1, 0)"
            )
        )
        await db_session.commit()


# ---------------------------------------------------------------------------
# DB-02: StockMovement.user_id nullable column
# ---------------------------------------------------------------------------


def test_stock_movement_model_has_user_id():
    """StockMovement ORM class must have a user_id mapped attribute.

    RED until app/modules/stock/models.py adds user_id column.
    """
    from app.modules.stock import models as stock_models

    movement_cls = stock_models.StockMovement
    assert hasattr(movement_cls, "user_id"), (
        "StockMovement.user_id attribute missing — run migration 0025 and update ORM model"
    )


async def test_stock_movement_user_id_nullable(db_session):
    """StockMovement rows can be inserted with user_id=None and user_id=42.

    RED until migration 0025 is applied and ORM model is updated.
    """
    from app.modules.stock import models as stock_models

    # Insert an ingredient first (FK requirement)
    result = await db_session.execute(
        sa.text(
            "INSERT INTO ingredients (name, unit, current_qty, alert_threshold) "
            "VALUES ('test_mov', 'kg', 10, 0) RETURNING id"
        )
    )
    ing_id = result.scalar_one()

    # Insert with user_id=None
    m1 = stock_models.StockMovement(
        ingredient_id=ing_id,
        quantity_delta=1.0,
        reason="loss",
        user_id=None,
    )
    db_session.add(m1)
    await db_session.flush()

    # Insert with user_id=42
    m2 = stock_models.StockMovement(
        ingredient_id=ing_id,
        quantity_delta=2.0,
        reason="breakage",
        user_id=42,
    )
    db_session.add(m2)
    await db_session.flush()

    assert m1.user_id is None
    assert m2.user_id == 42


# ---------------------------------------------------------------------------
# DB-03: ExtraIngredient accessible from stock module context
# ---------------------------------------------------------------------------


def test_extra_ingredient_importable():
    """ExtraIngredient must be importable for use by the stock module.

    Acceptable import paths:
      - from app.modules.catalog.models import ExtraIngredient  (direct)
      - from app.modules.stock.models import ExtraIngredient    (re-export)

    RED until DB-03 code work is done (re-export added to stock/models.py).
    """
    # Prefer the re-export path so stock module is self-contained
    from app.modules.stock import models as stock_models

    assert hasattr(stock_models, "ExtraIngredient"), (
        "ExtraIngredient not accessible via stock.models — "
        "add: from app.modules.catalog.models import ExtraIngredient  # noqa: F401"
    )


# ---------------------------------------------------------------------------
# DB-04: TenantConfig.stock_alert_cooldown_hours defaults to 4
# ---------------------------------------------------------------------------


def test_tenant_config_cooldown_default():
    """TenantConfig must have stock_alert_cooldown_hours with default=4.

    RED until migration 0026 is applied and tenant_models.py is updated.
    """
    from app.modules.admin.tenants.models import TenantConfig

    assert hasattr(TenantConfig, "stock_alert_cooldown_hours"), (
        "TenantConfig.stock_alert_cooldown_hours missing — update tenant_models.py"
    )

    # Inspect the column default via SQLAlchemy column property
    col = TenantConfig.__table__.c.stock_alert_cooldown_hours
    # server_default should be "4" (string representation)
    assert col.server_default is not None, "server_default must be set to '4'"


# ---------------------------------------------------------------------------
# DB-05: ProductStock removed from stock module code
# ---------------------------------------------------------------------------


def test_product_stock_removed():
    """ProductStock class must NOT be present in app.modules.stock.models.

    RED until the class is deleted from stock/models.py.
    """
    from app.modules.stock import models as stock_models

    assert not hasattr(stock_models, "ProductStock"), (
        "ProductStock still defined in stock/models.py — remove lines 50-55"
    )
