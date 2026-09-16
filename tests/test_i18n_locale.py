"""Tests locale FR/EN — resolution Accept-Language + wrapper gettext (t())."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.i18n.locale import (
    DEFAULT_LOCALE,
    get_locale,
    resolve_locale_from_accept_language,
    set_locale,
)
from app.core.i18n.translate import t


# ---------------------------------------------------------------------------
# resolve_locale_from_accept_language() — fonction pure, pas de header reel
# necessaire (le middleware app.main._resolve_locale ne fait que deleguer ici).
# ---------------------------------------------------------------------------


def test_resolve_locale_prefers_first_supported_language():
    assert resolve_locale_from_accept_language("en-US,en;q=0.9,fr;q=0.8") == "en"


def test_resolve_locale_handles_region_subtag():
    assert resolve_locale_from_accept_language("fr-FR,fr;q=0.9") == "fr"


def test_resolve_locale_defaults_without_header():
    assert resolve_locale_from_accept_language("") == DEFAULT_LOCALE


def test_resolve_locale_falls_back_for_unsupported_language():
    assert resolve_locale_from_accept_language("de-DE,de;q=0.9") == DEFAULT_LOCALE


def test_resolve_locale_skips_unsupported_before_supported():
    assert resolve_locale_from_accept_language("de-DE,en;q=0.8") == "en"


# ---------------------------------------------------------------------------
# ContextVar set_locale()/get_locale()
# ---------------------------------------------------------------------------


def test_set_locale_then_get_locale_roundtrip():
    set_locale("en")
    try:
        assert get_locale() == "en"
    finally:
        set_locale(DEFAULT_LOCALE)


def test_set_locale_rejects_unsupported_falls_back_to_default():
    set_locale("de")
    try:
        assert get_locale() == DEFAULT_LOCALE
    finally:
        set_locale(DEFAULT_LOCALE)


# ---------------------------------------------------------------------------
# t() — traduction + interpolation
# ---------------------------------------------------------------------------


def test_t_translates_known_key_fr():
    set_locale("fr")
    try:
        assert t("Ready for pickup") == "Prête à récupérer"
    finally:
        set_locale(DEFAULT_LOCALE)


def test_t_translates_known_key_en():
    set_locale("en")
    try:
        assert t("Ready for pickup") == "Ready for pickup"
    finally:
        set_locale(DEFAULT_LOCALE)


def test_t_supports_interpolation():
    set_locale("fr")
    try:
        assert t("Your order #{order_id} is ready!", order_id=42) == "Votre commande #42 est prête !"
    finally:
        set_locale(DEFAULT_LOCALE)


def test_t_explicit_locale_overrides_context_var():
    set_locale("en")
    try:
        assert t("Order confirmed", locale="fr") == "Commande confirmée"
    finally:
        set_locale(DEFAULT_LOCALE)


def test_t_unknown_key_returns_message_unchanged():
    set_locale("fr")
    try:
        assert t("This msgid does not exist in any catalog") == "This msgid does not exist in any catalog"
    finally:
        set_locale(DEFAULT_LOCALE)


# ---------------------------------------------------------------------------
# Integration : une transition de commande localisee via set_locale("en")
# produit une notification client en anglais (order.service._notif_map).
# ---------------------------------------------------------------------------


async def test_order_ready_notification_uses_english_when_locale_is_en():
    from app.modules.orders import service
    from app.modules.orders.models import Order

    order = Order(id=1, status="preparing", payment_status="paid", total=10, user_id=7)
    session = AsyncMock()
    session.get = AsyncMock(return_value=order)
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    set_locale("en")
    try:
        with patch.object(service, "notify_user", new=AsyncMock()) as mock_notify_user:
            await service.update_status(session, 1, "ready", tenant_slug="acme")
    finally:
        set_locale(DEFAULT_LOCALE)

    mock_notify_user.assert_awaited_once()
    kwargs = mock_notify_user.await_args.kwargs
    assert kwargs["title"] == "Ready for pickup"
    assert kwargs["body"] == "Your order #1 is ready!"


async def test_order_ready_notification_uses_french_by_default():
    from app.modules.orders import service
    from app.modules.orders.models import Order

    order = Order(id=1, status="preparing", payment_status="paid", total=10, user_id=7)
    session = AsyncMock()
    session.get = AsyncMock(return_value=order)
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    set_locale(DEFAULT_LOCALE)
    with patch.object(service, "notify_user", new=AsyncMock()) as mock_notify_user:
        await service.update_status(session, 1, "ready", tenant_slug="acme")

    mock_notify_user.assert_awaited_once()
    kwargs = mock_notify_user.await_args.kwargs
    assert kwargs["title"] == "Prête à récupérer"
    assert kwargs["body"] == "Votre commande #1 est prête !"


# ---------------------------------------------------------------------------
# Integration : erreurs client-facing traduites (cancel_my_order).
# ---------------------------------------------------------------------------


async def test_cancel_my_order_not_pending_error_translated_to_english():
    from app.core.http.errors import AppError
    from app.modules.orders import service
    from app.modules.orders.models import Order

    order = Order(id=1, status="confirmed", user_id=7)
    session = AsyncMock()
    session.get = AsyncMock(return_value=order)

    set_locale("en")
    try:
        with pytest.raises(AppError) as exc_info:
            await service.cancel_my_order(session, 1, user_id=7, tenant_slug="acme")
    finally:
        set_locale(DEFAULT_LOCALE)

    assert exc_info.value.code == "ORDER_CANCEL_NOT_ALLOWED"
    assert exc_info.value.detail == "Only pending orders can be cancelled by customer"


async def test_cancel_my_order_not_found_error_translated_to_french_by_default():
    from app.core.http.errors import AppError
    from app.modules.orders import service

    session = AsyncMock()
    session.get = AsyncMock(return_value=None)

    set_locale(DEFAULT_LOCALE)
    with pytest.raises(AppError) as exc_info:
        await service.cancel_my_order(session, 1, user_id=7, tenant_slug="acme")

    assert exc_info.value.code == "ORDER_NOT_FOUND"
    assert exc_info.value.detail == "Commande introuvable"
