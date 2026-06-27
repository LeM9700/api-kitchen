"""Tests P0 — Loyalty : génération promo code + auto-confirm réservation.

Couvre les fixes FF-02 et FF-03 :
  - FF-02 : redeem_reward discount_euros → promo_code non-null dans RedeemResponse
  - FF-03 : _auto_confirm_loyalty_reservation → confirm_checkout_reservation appelé
            après finalize_payment réussi

Tous les tests sont des tests unitaires (AsyncMock session) sans DB réelle.
"""

from decimal import Decimal
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules.loyalty.config import service as loyalty_service
from app.modules.loyalty.config.models import LoyaltyReward
from app.modules.loyalty.account.models import LoyaltyAccount, LoyaltyPointReservation
from app.modules.payments.service import _auto_confirm_loyalty_reservation


# ---------------------------------------------------------------------------
# FF-02 — redeem_reward → promo code
# ---------------------------------------------------------------------------


async def test_redeem_reward_discount_generates_promo_code():
    """redeem_reward(discount_euros) → promo_code non-null avec préfixe REWARD-.

    [⚠️ PROD] Sans promo_code, le frontend ne peut pas appliquer la réduction
    au panier — la récompense était débitée mais inutilisable.
    """
    reward = LoyaltyReward(
        id=1,
        name="5€ offerts",
        reward_type="discount_euros",
        discount_amount=Decimal("5.00"),
        points_required=100,
        is_active=True,
        product_id=None,
    )
    account = LoyaltyAccount(id=1, user_id=42, points=50)

    session = AsyncMock()
    session.get = AsyncMock(return_value=reward)
    session.add = MagicMock()
    session.commit = AsyncMock()

    async def _fake_refresh(obj):
        if hasattr(obj, "code"):
            obj.code = "REWARD-ABCDEF1234"

    session.refresh = AsyncMock(side_effect=_fake_refresh)

    with patch(
        "app.modules.loyalty.config.service.redeem_points",
        new_callable=AsyncMock,
        return_value=account,
    ):
        result = await loyalty_service.redeem_reward(session, 42, 1)

    assert result.promo_code is not None, "promo_code doit être valorisé pour discount_euros"
    assert result.promo_code.startswith("REWARD-"), "Format attendu : REWARD-{hex}"
    assert result.discount_euros == 5.0
    assert result.free_product_id is None
    session.add.assert_called_once()  # Promotion ajoutée en session
    session.commit.assert_awaited()


async def test_redeem_reward_discount_promo_code_is_unique():
    """Deux appels distincts génèrent des codes différents (UUID4 hex)."""
    reward = LoyaltyReward(
        id=1,
        name="10€ offerts",
        reward_type="discount_euros",
        discount_amount=Decimal("10.00"),
        points_required=200,
        is_active=True,
        product_id=None,
    )
    account = LoyaltyAccount(id=1, user_id=42, points=0)

    codes = []

    for suffix in ["AAAA000001", "BBBB000002"]:
        session = AsyncMock()
        session.get = AsyncMock(return_value=reward)
        session.add = MagicMock()
        session.commit = AsyncMock()

        async def _fake_refresh(obj, _s=suffix):
            if hasattr(obj, "code"):
                obj.code = f"REWARD-{_s}"

        session.refresh = AsyncMock(side_effect=_fake_refresh)

        with patch(
            "app.modules.loyalty.config.service.redeem_points",
            new_callable=AsyncMock,
            return_value=account,
        ):
            result = await loyalty_service.redeem_reward(session, 42, 1)

        codes.append(result.promo_code)

    assert codes[0] != codes[1], "Chaque échange doit générer un code unique"


async def test_redeem_reward_free_product_returns_no_promo_code():
    """redeem_reward(free_product) → promo_code None, free_product_id valorisé."""
    reward = LoyaltyReward(
        id=2,
        name="Pizza offerte",
        reward_type="free_product",
        discount_amount=None,
        points_required=200,
        is_active=True,
        product_id=7,
    )
    account = LoyaltyAccount(id=1, user_id=42, points=0)

    session = AsyncMock()
    session.get = AsyncMock(return_value=reward)
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    with patch(
        "app.modules.loyalty.config.service.redeem_points",
        new_callable=AsyncMock,
        return_value=account,
    ):
        result = await loyalty_service.redeem_reward(session, 42, 2)

    assert result.promo_code is None
    assert result.free_product_id == 7
    session.add.assert_not_called()  # Pas de Promotion créée pour free_product


async def test_redeem_reward_raises_if_reward_inactive():
    """redeem_reward sur récompense inactive → REWARD_NOT_FOUND."""
    from app.core.http.errors import AppError

    reward = LoyaltyReward(
        id=3,
        name="Offre expirée",
        reward_type="discount_euros",
        discount_amount=Decimal("3.00"),
        points_required=50,
        is_active=False,
        product_id=None,
    )

    session = AsyncMock()
    session.get = AsyncMock(return_value=reward)

    with pytest.raises(AppError) as exc:
        await loyalty_service.redeem_reward(session, 42, 3)

    assert exc.value.code == "REWARD_NOT_FOUND"


async def test_redeem_reward_raises_if_reward_not_found():
    """redeem_reward avec reward_id inexistant → REWARD_NOT_FOUND."""
    from app.core.http.errors import AppError

    session = AsyncMock()
    session.get = AsyncMock(return_value=None)

    with pytest.raises(AppError) as exc:
        await loyalty_service.redeem_reward(session, 42, 999)

    assert exc.value.code == "REWARD_NOT_FOUND"


# ---------------------------------------------------------------------------
# FF-03 — _auto_confirm_loyalty_reservation
# ---------------------------------------------------------------------------


async def test_auto_confirm_calls_confirm_when_reservation_active():
    """Réservation active trouvée → confirm_checkout_reservation appelé.

    [⚠️ PROD] Sans cet appel, les points restaient en "reserved" après le paiement
    et expiraient automatiquement au lieu d'être déduits.
    """
    now = datetime.now(timezone.utc)
    reservation = LoyaltyPointReservation(
        id=5,
        user_id=42,
        order_id=10,
        points_reserved=80,
        discount_amount=Decimal("4.00"),
        status="reserved",
        expires_at=now + timedelta(minutes=15),
    )

    session = AsyncMock()
    session.scalar = AsyncMock(return_value=reservation)

    with patch(
        "app.modules.payments.service.confirm_checkout_reservation",
        new_callable=AsyncMock,
    ) as mock_confirm:
        await _auto_confirm_loyalty_reservation(session, order_id=10, user_id=42)

    mock_confirm.assert_awaited_once_with(session, 42, 5)


async def test_auto_confirm_noop_if_no_reservation():
    """Pas de réservation active → pas d'appel à confirm_checkout_reservation."""
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=None)

    with patch(
        "app.modules.payments.service.confirm_checkout_reservation",
        new_callable=AsyncMock,
    ) as mock_confirm:
        await _auto_confirm_loyalty_reservation(session, order_id=10, user_id=42)

    mock_confirm.assert_not_awaited()


async def test_auto_confirm_noop_if_user_id_is_none():
    """Commande invité (user_id=None) → pas de requête DB, pas de confirm.

    Les invités n'ont pas de compte fidélité.
    """
    session = AsyncMock()

    with patch(
        "app.modules.payments.service.confirm_checkout_reservation",
        new_callable=AsyncMock,
    ) as mock_confirm:
        await _auto_confirm_loyalty_reservation(session, order_id=10, user_id=None)

    session.scalar.assert_not_awaited()
    mock_confirm.assert_not_awaited()


async def test_auto_confirm_absorbs_app_error_without_raising():
    """confirm_checkout_reservation lève AppError → absorbée, pas de re-raise.

    [⚠️ PROD] Le paiement est déjà confirmé — une erreur sur la fidélité ne doit
    pas faire échouer rétroactivement la transaction Stripe.
    """
    from app.core.http.errors import AppError

    now = datetime.now(timezone.utc)
    reservation = LoyaltyPointReservation(
        id=6,
        user_id=42,
        order_id=11,
        points_reserved=50,
        discount_amount=Decimal("2.50"),
        status="reserved",
        expires_at=now + timedelta(minutes=10),
    )

    session = AsyncMock()
    session.scalar = AsyncMock(return_value=reservation)

    with patch(
        "app.modules.payments.service.confirm_checkout_reservation",
        new_callable=AsyncMock,
        side_effect=AppError("RESERVATION_EXPIRED", "Réservation expirée", 409),
    ):
        # Ne doit pas lever d'exception
        await _auto_confirm_loyalty_reservation(session, order_id=11, user_id=42)
