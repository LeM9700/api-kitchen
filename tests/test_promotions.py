from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class _Result:
    def __init__(self, scalars=None, row=None, rows=None):
        self._scalars = scalars if scalars is not None else []
        self._row = row
        self._rows = rows if rows is not None else []

    def scalars(self):
        return self

    def all(self):
        return self._scalars or self._rows

    def one(self):
        return self._row

    def fetchone(self):
        return self._row


async def test_validate_invalid_code_requires_auth(client):
    response = await client.post(
        "/api/v1/promotions/validate",
        json={"code": "NOSUCHCODE", "order_total": 20.0},
    )
    assert response.status_code == 401


async def test_preview_promo_does_not_increment_current_uses():
    from app.modules.promotions import service
    from app.modules.promotions.models import Promotion
    from app.modules.promotions.schemas import PromotionCartItem

    promo = Promotion(
        id=1,
        code="SAVE10",
        discount_type="percent",
        discount_value=10,
        min_order_amount=0,
        is_active=True,
        current_uses=0,
        first_order_only=False,
        is_public=True,
        is_stackable=False,
        email_verified_required=False,
    )
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[_Result([promo]), _Result([]), _Result([])])

    result = await service.preview_promos(
        session,
        ["SAVE10"],
        50,
        [PromotionCartItem(product_id=1, category_id=1, quantity=1, unit_price=50)],
        user_id=7,
        email_verified=True,
    )

    assert result.discount == 5
    assert promo.current_uses == 0
    assert not any("UPDATE promotions" in str(call.args[0]) for call in session.execute.call_args_list)


async def test_apply_promo_consumes_quota_atomically():
    from app.modules.promotions import service
    from app.modules.promotions.models import Promotion

    promo = Promotion(
        id=1,
        code="SAVE5",
        discount_type="fixed",
        discount_value=5,
        min_order_amount=0,
        is_active=True,
        current_uses=0,
        max_uses=1,
        first_order_only=False,
        is_public=True,
        is_stackable=False,
        email_verified_required=False,
    )
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[_Result([promo]), _Result([]), _Result([]), _Result(row=(1,))])

    result = await service.apply_promo(session, "default", "SAVE5", 20, user_id=7)

    assert result == 5
    assert any("UPDATE promotions" in str(call.args[0]) for call in session.execute.call_args_list)


async def test_category_target_discount_only_uses_eligible_lines():
    from app.modules.promotions import service
    from app.modules.promotions.models import Promotion
    from app.modules.promotions.schemas import PromotionCartItem

    promo = Promotion(
        id=1,
        code="PIZZA10",
        discount_type="percent",
        discount_value=10,
        min_order_amount=0,
        is_active=True,
        current_uses=0,
        first_order_only=False,
        is_public=True,
        is_stackable=False,
        email_verified_required=False,
    )
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[_Result([promo]), _Result([5]), _Result([])])

    result = await service.preview_promos(
        session,
        ["PIZZA10"],
        50,
        [
            PromotionCartItem(product_id=1, category_id=5, quantity=1, unit_price=20),
            PromotionCartItem(product_id=2, category_id=6, quantity=1, unit_price=30),
        ],
        user_id=7,
        email_verified=True,
    )

    assert result.discount == 2


async def test_multiple_codes_refused_when_one_is_not_stackable():
    from app.core.http.errors import AppError
    from app.modules.promotions import service
    from app.modules.promotions.models import Promotion

    promo_a = Promotion(
        id=1,
        code="A10",
        discount_type="percent",
        discount_value=10,
        min_order_amount=0,
        is_active=True,
        current_uses=0,
        first_order_only=False,
        is_public=True,
        is_stackable=False,
        email_verified_required=False,
    )
    promo_b = Promotion(
        id=2,
        code="B5",
        discount_type="fixed",
        discount_value=5,
        min_order_amount=0,
        is_active=True,
        current_uses=0,
        first_order_only=False,
        is_public=True,
        is_stackable=True,
        email_verified_required=False,
    )
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_Result([promo_a, promo_b]))

    with pytest.raises(AppError) as exc_info:
        await service.preview_promos(session, ["A10", "B5"], 50, [], user_id=7, email_verified=True)

    assert exc_info.value.code == "INVALID_PROMO"


async def test_bulk_generation_creates_unique_campaign_codes():
    from app.modules.promotions import service
    from app.modules.promotions.models import Promotion, PromotionCampaign
    from app.modules.promotions.schemas import PromotionCampaignCreate, PromotionCreate

    added = []

    def add(obj):
        if isinstance(obj, PromotionCampaign):
            obj.id = 1
            obj.created_at = datetime.now(timezone.utc)
        if isinstance(obj, Promotion):
            obj.id = len([item for item in added if isinstance(item, Promotion)]) + 10
        added.append(obj)

    session = AsyncMock()
    session.add = MagicMock(side_effect=add)
    session.flush = AsyncMock()
    session.scalar = AsyncMock(return_value=None)
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    body = PromotionCampaignCreate(
        name="Summer",
        prefix="SUMMER",
        count=2,
        promotion=PromotionCreate(
            code="IGNORED",
            discount_type="fixed",
            discount_value=3,
        ),
    )
    with patch("app.modules.promotions.service._new_campaign_code", side_effect=["SUMMER-AAAAAA", "SUMMER-BBBBBB"]):
        result = await service.bulk_generate(session, body, created_by_user_id=7)

    assert result.codes == ["SUMMER-AAAAAA", "SUMMER-BBBBBB"]
    assert len({promo.code for promo in added if isinstance(promo, Promotion)}) == 2


async def test_stats_admin_aggregate_usage_and_revenue():
    from app.modules.promotions import service

    session = AsyncMock()
    session.execute = AsyncMock(return_value=_Result(row=(2, 1, 100, 85, 15)))

    stats = await service._stats_for_promo(session, 1)

    assert stats.usage_count == 2
    assert stats.unique_users == 1
    assert stats.revenue_gross == 100
    assert stats.discount_total == 15


def test_public_promotion_schema_does_not_expose_private_fields():
    from app.modules.promotions.schemas import PromotionPublicOut

    fields = set(PromotionPublicOut.model_fields)

    assert "user_id" not in fields
    assert "current_uses" not in fields
    assert "remaining_uses" not in fields
