"""Reglages de livraison par etablissement : mode d'attribution, plafond, regles d'echec.

Sans ligne pour un etablissement : attribution au comptoir, plafond de 3 livraisons en cours par livreur, regles
d'echec du reglage general du tenant. Chaque ecriture est auditee (table d'audit des reglages de livraison).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.http.errors import AppError
from app.modules.delivery.models import (
    EstablishmentDispatchSettings,
    RestaurantDeliverySettings,
    RestaurantDeliverySettingsAudit,
)
from app.modules.hr.models import Establishment

DISPATCH_MODES = ("counter", "self_assign")
DEFAULT_MODE = "counter"
DEFAULT_MAX_ACTIVE = 3
_FIELDS = ("dispatch_mode", "max_active_deliveries", "failure_min_wait_minutes", "failure_min_call_attempts")


async def _tenant_rules(session: AsyncSession) -> tuple[int, int]:
    row = await session.scalar(select(RestaurantDeliverySettings).order_by(RestaurantDeliverySettings.id).limit(1))
    if row is None:
        return 5, 1
    return int(row.failure_min_wait_minutes), int(row.failure_min_call_attempts)


async def get_effective(session: AsyncSession, establishment_id: int) -> dict:
    """Reglages effectifs d'un etablissement (valeurs heritees comprises). Lecture seule."""
    row = await session.scalar(
        select(EstablishmentDispatchSettings).where(EstablishmentDispatchSettings.establishment_id == establishment_id)
    )
    tenant_wait, tenant_calls = await _tenant_rules(session)
    return {
        "establishment_id": establishment_id,
        "dispatch_mode": row.dispatch_mode if row else DEFAULT_MODE,
        "max_active_deliveries": int(row.max_active_deliveries) if row else DEFAULT_MAX_ACTIVE,
        "failure_min_wait_minutes": (
            int(row.failure_min_wait_minutes) if row and row.failure_min_wait_minutes is not None else tenant_wait
        ),
        "failure_min_call_attempts": (
            int(row.failure_min_call_attempts) if row and row.failure_min_call_attempts is not None else tenant_calls
        ),
        # True quand la valeur vient d'un reglage propre a l'etablissement, False si elle est heritee.
        "failure_rules_overridden": bool(
            row and (row.failure_min_wait_minutes is not None or row.failure_min_call_attempts is not None)
        ),
        "version": int(row.version) if row else 0,
    }


def _validate(values: dict) -> None:
    mode = values.get("dispatch_mode")
    if mode is not None and mode not in DISPATCH_MODES:
        raise AppError("DISPATCH_MODE_INVALID", "Mode d'attribution inconnu.", 422, "dispatch_mode")
    cap = values.get("max_active_deliveries")
    if cap is not None and not 1 <= cap <= 10:
        raise AppError("MAX_ACTIVE_INVALID", "Le plafond doit etre compris entre 1 et 10.", 422, "max_active_deliveries")
    wait = values.get("failure_min_wait_minutes")
    if wait is not None and not 0 <= wait <= 60:
        raise AppError("FAILURE_WAIT_INVALID", "L'attente doit etre comprise entre 0 et 60 minutes.", 422, "failure_min_wait_minutes")
    calls = values.get("failure_min_call_attempts")
    if calls is not None and not 0 <= calls <= 5:
        raise AppError("FAILURE_CALLS_INVALID", "Le nombre d'appels doit etre compris entre 0 et 5.", 422, "failure_min_call_attempts")


async def update(
    session: AsyncSession,
    establishment_id: int,
    values: dict,
    *,
    expected_version: int,
    user_id: int,
    user_email: str | None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> dict:
    """Ecrit les champs fournis (``values`` ne contient que ceux a changer ; ``None`` explicite = revenir au
    reglage general pour les regles d'echec). Concurrence optimiste sur ``version`` (0 = pas encore de ligne)."""
    _validate(values)
    if await session.get(Establishment, establishment_id) is None:
        raise AppError("ESTABLISHMENT_NOT_FOUND", "Etablissement introuvable", 404)
    row = await session.scalar(
        select(EstablishmentDispatchSettings)
        .where(EstablishmentDispatchSettings.establishment_id == establishment_id)
        .with_for_update()
    )
    current_version = int(row.version) if row else 0
    if current_version != expected_version:
        raise AppError(
            "DISPATCH_SETTINGS_CONFLICT",
            "Les reglages ont ete modifies entre-temps, rechargez la page.",
            409,
        )
    if row is None:
        row = EstablishmentDispatchSettings(establishment_id=establishment_id)
        session.add(row)
        await session.flush()
    changed = False
    for field in _FIELDS:
        if field not in values:
            continue
        # Un champ obligatoire ne peut pas etre efface ; seules les regles d'echec heritent via NULL.
        if values[field] is None and field in ("dispatch_mode", "max_active_deliveries"):
            continue
        old = getattr(row, field)
        new = values[field]
        if old == new or (old is None and new is None):
            continue
        session.add(
            RestaurantDeliverySettingsAudit(
                changed_by_user_id=user_id,
                user_email=user_email,
                field_name=f"establishment_{establishment_id}.{field}",
                old_value=None if old is None else str(old).lower(),
                new_value=None if new is None else str(new).lower(),
                ip_address=(ip_address or "")[:45] or None,
                user_agent=user_agent,
            )
        )
        setattr(row, field, new)
        changed = True
    if changed or current_version == 0:
        row.version = current_version + 1
    await session.commit()
    return await get_effective(session, establishment_id)
