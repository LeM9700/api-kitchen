from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.http.errors import AppError
from app.modules.haccp import order_gate


@pytest.mark.asyncio
async def test_order_gate_is_inactive_until_establishment_is_strict_enabled():
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=SimpleNamespace(id=11, is_active=True)),
        get=AsyncMock(return_value=SimpleNamespace(strict_enabled=False)),
    )
    await order_gate.enforce_ordering(session, 11, [100])
    session.get.assert_awaited_once()


@pytest.mark.asyncio
async def test_order_gate_rejects_unvalidated_haccp(monkeypatch):
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=SimpleNamespace(id=11, is_active=True)),
        get=AsyncMock(return_value=SimpleNamespace(strict_enabled=True)),
    )
    monkeypatch.setattr(
        order_gate,
        "decision",
        AsyncMock(
            return_value={
                "daily_haccp_satisfied": False,
                "blockers": [{"code": "HACCP_OPENING_MISSING"}],
            }
        ),
    )
    with pytest.raises(AppError) as error:
        await order_gate.enforce_ordering(session, 11, [100])
    assert error.value.code == "HACCP_ORDERING_BLOCKED"


@pytest.mark.asyncio
async def test_order_gate_rejects_local_stock_failure(monkeypatch):
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=SimpleNamespace(id=11, is_active=True)),
        get=AsyncMock(return_value=SimpleNamespace(strict_enabled=True)),
    )
    monkeypatch.setattr(
        order_gate,
        "decision",
        AsyncMock(return_value={"daily_haccp_satisfied": True, "blockers": []}),
    )
    monkeypatch.setattr(
        order_gate,
        "site_product_availability",
        AsyncMock(
            return_value={
                100: {
                    "product_id": 100,
                    "available": False,
                    "limiting_ingredient": "Tomate",
                    "reason": "expired_or_insufficient_site_stock",
                }
            }
        ),
    )
    with pytest.raises(AppError) as error:
        await order_gate.enforce_ordering(session, 11, [100])
    assert error.value.code == "HACCP_STOCK_UNAVAILABLE"
