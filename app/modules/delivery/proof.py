"""Preuve de remise : code a 4 chiffres affiche au client, saisi par le livreur.

Le code n'est **jamais stocke** : il se recalcule (HMAC-SHA256 du secret serveur, du tenant, de
la commande et d'une graine aleatoire portee par la commande). Une fuite de la base ne donne donc
aucun code, et changer la graine (relivraison apres un echec) invalide l'ancien code.

Un code a 4 chiffres ne protege que s'il est limite en essais : au bout de ``MAX_FAILED_ATTEMPTS``
essais ratés sur une livraison, elle est verrouillee (seul un administrateur peut la conclure, sans
code, avec un motif). Chaque essai est journalise.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.http.errors import AppError
from app.modules.delivery.models import DeliveryCodeAttempt
from app.modules.orders.models import Order

CODE_LENGTH = 4
MAX_FAILED_ATTEMPTS = 5
_CODE_RE = re.compile(rf"^\d{{{CODE_LENGTH}}}$")


def derive_code(tenant_slug: str, order_id: int, nonce: str) -> str:
    key = f"{settings.jwt_secret}|delivery-code".encode()
    digest = hmac.new(key, f"{tenant_slug}:{order_id}:{nonce}".encode(), hashlib.sha256).digest()
    return f"{int.from_bytes(digest[:8], 'big') % (10 ** CODE_LENGTH):0{CODE_LENGTH}d}"


async def ensure_nonce(session: AsyncSession, order_id: int) -> str:
    """Graine de la commande, creee au premier besoin (atomique : deux demandes simultanees
    obtiennent la meme)."""
    result = await session.execute(
        update(Order)
        .where(Order.id == order_id, Order.delivery_code_nonce.is_(None))
        .values(delivery_code_nonce=secrets.token_hex(8))
    )
    if result.rowcount:
        await session.commit()
    nonce = await session.scalar(select(Order.delivery_code_nonce).where(Order.id == order_id))
    if nonce is None:
        raise AppError("ORDER_NOT_FOUND", "Order not found", 404)
    return nonce


async def get_code(session: AsyncSession, tenant_slug: str, order_id: int) -> str:
    return derive_code(tenant_slug, order_id, await ensure_nonce(session, order_id))


async def regenerate_nonce(session: AsyncSession, order_id: int) -> None:
    """Invalide le code actuel (relivraison). Ne commit pas : fait partie de la transaction appelante."""
    await session.execute(
        update(Order).where(Order.id == order_id).values(delivery_code_nonce=secrets.token_hex(8))
    )


async def failed_attempts(session: AsyncSession, delivery_id: int) -> int:
    return int(
        await session.scalar(
            select(func.count(DeliveryCodeAttempt.id)).where(
                DeliveryCodeAttempt.delivery_id == delivery_id, DeliveryCodeAttempt.success.is_(False)
            )
        )
        or 0
    )


async def verify_code(
    session: AsyncSession,
    *,
    tenant_slug: str,
    delivery_id: int,
    order_id: int,
    user_id: int | None,
    submitted: str | None,
) -> None:
    """Verifie le code saisi par le livreur. Un essai rate est enregistre et **committe** avant de
    lever l'erreur (sinon le rollback de l'appelant effacerait le compteur)."""
    if await failed_attempts(session, delivery_id) >= MAX_FAILED_ATTEMPTS:
        raise AppError(
            "DELIVERY_CODE_LOCKED",
            "Trop d'essais : contactez le restaurant, un administrateur peut conclure cette livraison.",
            423,
            "code",
        )
    code = (submitted or "").strip()
    if not _CODE_RE.match(code):
        raise AppError("DELIVERY_CODE_INVALID", f"Le code comporte {CODE_LENGTH} chiffres.", 422, "code")
    expected = await get_code(session, tenant_slug, order_id)
    ok = hmac.compare_digest(code.encode(), expected.encode())
    session.add(DeliveryCodeAttempt(delivery_id=delivery_id, user_id=user_id, success=ok))
    await session.commit()
    if not ok:
        left = max(0, MAX_FAILED_ATTEMPTS - await failed_attempts(session, delivery_id))
        raise AppError(
            "DELIVERY_CODE_INVALID",
            f"Code incorrect. {left} essai(s) restant(s).",
            422,
            "code",
        )
