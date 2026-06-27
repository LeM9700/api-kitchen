"""Endpoints REST pour la gestion des tokens push (device registration).

Routes exposées :
- ``POST   /notifications/devices``      — Enregistre ou met à jour un token.
- ``DELETE /notifications/devices/{id}`` — Supprime un token.
- ``GET    /notifications/devices``      — Liste les tokens de l'utilisateur courant.

Toutes les routes nécessitent un utilisateur authentifié (``get_current_user``).
La route POST est rate-limitée à 10 req/min par IP pour limiter les abus.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import select

from app.core.database import get_tenant_session
from app.core.http.deps import get_current_user
from app.core.http.errors import AppError
from app.core.http.limiter import limiter
from app.modules.notifications.models import DeviceToken
from app.modules.notifications.schemas import DeviceTokenCreate, DeviceTokenResponse

router = APIRouter()


@router.post(
    "/devices",
    response_model=DeviceTokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Enregistrer un token push",
)
@limiter.limit("10/minute")
async def register_device(
    request: Request,
    body: DeviceTokenCreate,
    current_user: dict = Depends(get_current_user),
) -> DeviceTokenResponse:
    """Enregistre ou réactive un token push pour l'utilisateur courant.

    Si le couple ``(user_id, token)`` existe déjà, effectue un upsert :
    ``is_active=True`` et ``last_used_at`` mis à jour. Sinon, crée un nouvel
    enregistrement.

    Args:
        request: Requête FastAPI (requis par SlowAPI pour le rate-limit).
        body: Payload validé par Pydantic (platform, token, device_name).
        current_user: Utilisateur extrait du JWT.

    Returns:
        DeviceTokenResponse de l'enregistrement créé ou mis à jour.
    """
    user_id = int(current_user["id"])
    tenant_slug = current_user["tenant_slug"]

    async with get_tenant_session(tenant_slug) as session:
        # Upsert : si le token existe déjà, on le réactive.
        existing = await session.scalar(
            select(DeviceToken).where(
                DeviceToken.user_id == user_id,
                DeviceToken.token == body.token,
            )
        )

        if existing:
            existing.is_active = True
            existing.last_used_at = datetime.now(timezone.utc)
            if body.device_name is not None:
                existing.device_name = body.device_name
            await session.commit()
            await session.refresh(existing)
            return DeviceTokenResponse.model_validate(existing)

        device = DeviceToken(
            user_id=user_id,
            platform=body.platform,
            token=body.token,
            device_name=body.device_name,
            is_active=True,
        )
        session.add(device)
        await session.commit()
        await session.refresh(device)
        return DeviceTokenResponse.model_validate(device)


@router.delete(
    "/devices/{device_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Supprimer un token push",
)
async def delete_device(
    device_id: int,
    current_user: dict = Depends(get_current_user),
) -> None:
    """Supprime un token push enregistré par l'utilisateur courant.

    [🔒 SÉCURITÉ] Vérifie que le token appartient bien à l'utilisateur courant
    avant suppression.

    Args:
        device_id: Identifiant du DeviceToken à supprimer.
        current_user: Utilisateur extrait du JWT.

    Raises:
        AppError: DEVICE_NOT_FOUND (404) si le token n'appartient pas à cet user.
    """
    user_id = int(current_user["id"])
    tenant_slug = current_user["tenant_slug"]

    async with get_tenant_session(tenant_slug) as session:
        device = await session.get(DeviceToken, device_id)
        if device is None or device.user_id != user_id:
            raise AppError("DEVICE_NOT_FOUND", "Device token not found", 404)

        await session.delete(device)
        await session.commit()


@router.get(
    "/devices",
    response_model=list[DeviceTokenResponse],
    summary="Lister mes tokens push",
)
async def list_devices(
    current_user: dict = Depends(get_current_user),
) -> list[DeviceTokenResponse]:
    """Retourne tous les tokens push enregistrés par l'utilisateur courant.

    Utile pour le debug et la gestion multi-device côté client.

    Args:
        current_user: Utilisateur extrait du JWT.

    Returns:
        Liste des DeviceTokenResponse (tokens actifs et inactifs).
    """
    user_id = int(current_user["id"])
    tenant_slug = current_user["tenant_slug"]

    async with get_tenant_session(tenant_slug) as session:
        result = await session.execute(
            select(DeviceToken)
            .where(DeviceToken.user_id == user_id)
            .order_by(DeviceToken.created_at.desc())
        )
        devices = list(result.scalars())

    return [DeviceTokenResponse.model_validate(d) for d in devices]
