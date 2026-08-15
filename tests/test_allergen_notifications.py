from unittest.mock import AsyncMock, MagicMock


async def test_manual_allergen_notification_uses_current_tenant_slug(monkeypatch):
    from app.modules.catalog.allergen import allergen_service
    from app.modules.notifications import notification_service

    seen: list[dict] = []

    async def fake_notify_user(**kwargs):
        seen.append(kwargs)

    monkeypatch.setattr(notification_service, "notify_user", fake_notify_user)

    session = AsyncMock()
    session.get = AsyncMock(return_value=None)
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    session.commit = AsyncMock()
    session.execute = AsyncMock(return_value=_ScalarRows([42]))

    await allergen_service.set_product_allergen_manual(
        session,
        product_id=10,
        allergen_id=3,
        level="present",
        user_id=1,
        tenant_slug="pizza_test",
    )

    assert seen
    assert seen[0]["tenant_slug"] == "pizza_test"
    assert seen[0]["user_id"] == 42
    assert seen[0]["event"] == "allergen_update"


class _ScalarRows:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return self._values
