"""Tenant self-service configuration service."""
import json
from datetime import date, datetime, timedelta

import bleach
import pytz
from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.tenants.models import (
    BusinessHours,
    ExceptionalClosure,
    TenantConfig,
    TenantConfigAudit,
)
from app.modules.admin.tenants.schemas import (
    BusinessHoursCreate,
    ExceptionalClosureCreate,
    TenantBrandingResponse,
    TenantBrandingUpdate,
    TenantPrintConfigResponse,
    TenantPrintConfigUpdate,
    TenantScheduledClosureRequest,
    TenantConfigUpdate,
    TenantStatusResponse,
)
from app.modules.orders.models import Order

_ACTIVE_ORDER_STATUSES = ("confirmed", "in_preparation")
_DAY_NAMES_FR = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]

# [🔒 SÉCURITÉ] Whitelist strict — aucun tag HTML autorisé dans les messages de fermeture.
_ALLOWED_TAGS: list[str] = []
_ALLOWED_ATTRS: dict = {}


# ---------------------------------------------------------------------------
# Helpers privés — sanitisation & audit
# ---------------------------------------------------------------------------


def _sanitize_message(text: str | None) -> str | None:
    """Sanitize un message de fermeture pour prévenir les injections XSS.

    [🔒 SÉCURITÉ] Aucun tag HTML n'est autorisé ; bleach strip tous les tags
    présents plutôt que de les échapper, retournant du texte brut.

    Args:
        text: Message brut issu de l'input admin.

    Returns:
        Texte plain-text sanitisé, ou None si l'entrée est None.
    """
    if text is None:
        return None
    return bleach.clean(text, tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRS, strip=True)


async def _write_audit(
    session: AsyncSession,
    user_id: int,
    field_name: str,
    old_value: str | None,
    new_value: str | None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    user_email: str | None = None,
) -> None:
    """Insère une entrée d'audit pour une modification de configuration.

    [🔒 SÉCURITÉ] À appeler AVANT le commit de la modification principale
    pour garantir l'atomicité (même transaction).

    Args:
        session: Session SQLAlchemy active.
        user_id: Identifiant de l'utilisateur effectuant la modification.
        field_name: Nom du champ modifié.
        old_value: Ancienne valeur JSON-sérialisée.
        new_value: Nouvelle valeur JSON-sérialisée.
        ip_address: Adresse IP de la requête (optionnel).
        user_agent: User-Agent de la requête (optionnel).
        user_email: Email de l'utilisateur au moment de la modification (optionnel).
    """
    audit = TenantConfigAudit(
        changed_by_user_id=user_id,
        field_name=field_name,
        old_value=old_value,
        new_value=new_value,
        ip_address=ip_address,
        user_agent=user_agent,
        user_email=user_email,
    )
    session.add(audit)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


async def get_or_create_config(session: AsyncSession) -> TenantConfig:
    """Retourne la configuration tenant, en la créant avec les valeurs par défaut si absente.

    Args:
        session: Session SQLAlchemy active sur le schema tenant courant.

    Returns:
        Instance ``TenantConfig`` persistée.
    """
    config = await session.scalar(select(TenantConfig))
    if config is None:
        config = TenantConfig()
        session.add(config)
        await session.commit()
        await session.refresh(config)
    return config


async def update_config(
    session: AsyncSession,
    data: TenantConfigUpdate,
    user_id: int | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    user_email: str | None = None,
    arq_pool=None,
    tenant_slug: str | None = None,
) -> TenantConfig:
    """Met à jour la configuration tenant avec les champs fournis.

    [🔒 SÉCURITÉ] Sanitise les champs message avant persistance (XSS).
    Insère une entrée d'audit pour chaque champ effectivement modifié.

    Args:
        session: Session SQLAlchemy active sur le schema tenant courant.
        data: Champs à mettre à jour (PATCH sémantique — None ignorés).
        user_id: Identifiant de l'admin effectuant la modification (audit).
        ip_address: Adresse IP de la requête (audit).
        user_agent: User-Agent de la requête (audit).
        user_email: Email de l'utilisateur au moment de la modification (audit).
        arq_pool: Pool ARQ pour les notifications asynchrones (Task 4, non utilisé ici).
        tenant_slug: Slug du tenant pour les notifications (Task 4, non utilisé ici).

    Returns:
        Instance ``TenantConfig`` mise à jour.
    """
    config = await get_or_create_config(session)
    updates = data.model_dump(exclude_none=True)

    # [🔒 DOS] Cooldown 2 min sur is_temporarily_closed pour éviter le spam open/close.
    if "is_temporarily_closed" in updates:
        from datetime import timezone as _tz
        from sqlalchemy import desc as _desc
        last_toggle = await session.scalar(
            select(TenantConfigAudit)
            .where(TenantConfigAudit.field_name == "is_temporarily_closed")
            .order_by(_desc(TenantConfigAudit.changed_at))
        )
        if last_toggle is not None:
            age_seconds = (
                datetime.now(_tz.utc) - last_toggle.changed_at.replace(tzinfo=_tz.utc)
            ).total_seconds()
            if age_seconds < 120:
                raise HTTPException(
                    status_code=429,
                    detail="Trop de changements de statut. Attendez 2 minutes avant de modifier is_temporarily_closed.",
                )

    # [🔒 SÉCURITÉ] Sanitisation XSS avant toute persistance.
    for msg_field in ("temporary_closure_message", "default_closure_message"):
        if msg_field in updates:
            updates[msg_field] = _sanitize_message(updates[msg_field])

    # Audit — comparer valeurs avant/après et tracer chaque champ modifié.
    if user_id is not None:
        for field, new_val in updates.items():
            old_val = getattr(config, field, None)
            if old_val != new_val:
                await _write_audit(
                    session,
                    user_id=user_id,
                    field_name=field,
                    old_value=json.dumps(old_val, default=str),
                    new_value=json.dumps(new_val, default=str),
                    ip_address=ip_address,
                    user_agent=user_agent,
                    user_email=user_email,
                )

    for field, value in updates.items():
        setattr(config, field, value)

    await session.commit()
    await session.refresh(config)

    # Notifier les admins/staff si is_temporarily_closed a changé.
    if (
        arq_pool is not None
        and tenant_slug is not None
        and "is_temporarily_closed" in updates
    ):
        try:
            await arq_pool.enqueue_job(
                "notify_config_change",
                tenant_slug=tenant_slug,
                is_closed=config.is_temporarily_closed,
            )
        except Exception:
            pass  # Notification non critique — ne pas bloquer la réponse.

    return config


async def get_print_config(session: AsyncSession) -> TenantPrintConfigResponse:
    config = await get_or_create_config(session)
    return TenantPrintConfigResponse(
        print_enabled=config.print_enabled,
        print_config=config.print_config,
    )


async def update_print_config(
    session: AsyncSession,
    data: TenantPrintConfigUpdate,
    *,
    user_id: int,
    user_email: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> TenantPrintConfigResponse:
    config = await get_or_create_config(session)
    updates = data.model_dump(exclude_none=True)

    for field, new_value in updates.items():
        old_value = getattr(config, field, None)
        if old_value != new_value:
            await _write_audit(
                session,
                user_id=user_id,
                field_name=field,
                old_value=json.dumps(old_value, default=str),
                new_value=json.dumps(new_value, default=str),
                ip_address=ip_address,
                user_agent=user_agent,
                user_email=user_email,
            )
            setattr(config, field, new_value)

    await session.commit()
    await session.refresh(config)
    return TenantPrintConfigResponse(
        print_enabled=config.print_enabled,
        print_config=config.print_config,
    )


async def schedule_closure(
    session: AsyncSession,
    data: TenantScheduledClosureRequest,
    user_id: int,
    ip_address: str | None = None,
    user_agent: str | None = None,
    user_email: str | None = None,
) -> TenantConfig:
    """Planifie ou annule une fermeture automatique du tenant.

    ``scheduled_close_at=None`` annule uniquement la planification. Le message
    de fermeture est mis a jour seulement quand un message explicite est fourni.
    """
    config = await get_or_create_config(session)

    old_scheduled_close_at = config.scheduled_close_at
    new_scheduled_close_at = data.scheduled_close_at
    if old_scheduled_close_at != new_scheduled_close_at:
        await _write_audit(
            session,
            user_id=user_id,
            field_name="scheduled_close_at",
            old_value=json.dumps(old_scheduled_close_at, default=str),
            new_value=json.dumps(new_scheduled_close_at, default=str),
            ip_address=ip_address,
            user_agent=user_agent,
            user_email=user_email,
        )
        config.scheduled_close_at = new_scheduled_close_at

    if data.temporary_closure_message is not None:
        new_message = _sanitize_message(data.temporary_closure_message)
        if config.temporary_closure_message != new_message:
            await _write_audit(
                session,
                user_id=user_id,
                field_name="temporary_closure_message",
                old_value=json.dumps(config.temporary_closure_message, default=str),
                new_value=json.dumps(new_message, default=str),
                ip_address=ip_address,
                user_agent=user_agent,
                user_email=user_email,
            )
            config.temporary_closure_message = new_message

    await session.commit()
    await session.refresh(config)
    return config


# ---------------------------------------------------------------------------
# Business hours
# ---------------------------------------------------------------------------


async def get_business_hours(session: AsyncSession) -> list[BusinessHours]:
    """Retourne tous les créneaux horaires triés par jour puis index.

    Args:
        session: Session SQLAlchemy active sur le schema tenant courant.

    Returns:
        Liste de ``BusinessHours`` triée par ``(day_of_week, slot_index)``.
    """
    result = await session.execute(
        select(BusinessHours).order_by(BusinessHours.day_of_week, BusinessHours.slot_index)
    )
    return list(result.scalars().all())


async def upsert_business_hours(
    session: AsyncSession,
    day_of_week: int,
    slots: list[BusinessHoursCreate],
    user_id: int | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    user_email: str | None = None,
) -> list[BusinessHours]:
    """Remplace tous les créneaux d'un jour par les nouveaux créneaux fournis.

    Supprime les anciens créneaux du jour puis insère les nouveaux.
    Valide que les créneaux ne se chevauchent pas avant toute écriture.

    [⚠️ PROD] Acquiert un verrou pessimiste sur TenantConfig pour sérialiser
    les écritures concurrentes et éviter les race conditions.

    Args:
        session: Session SQLAlchemy active sur le schema tenant courant.
        day_of_week: Jour de la semaine (0=lundi, 6=dimanche).
        slots: Nouveaux créneaux à persister pour ce jour.
        user_id: Identifiant de l'admin effectuant la modification (audit).
        ip_address: Adresse IP de la requête (audit).
        user_agent: User-Agent de la requête (audit).
        user_email: Email de l'utilisateur au moment de la modification (audit).

    Returns:
        Liste des ``BusinessHours`` créés, triée par ``slot_index``.

    Raises:
        HTTPException: 422 si deux créneaux du même jour se chevauchent.
        HTTPException: 422 si ``day_of_week`` est hors intervalle [0, 6].
    """
    if not (0 <= day_of_week <= 6):
        raise HTTPException(
            status_code=422,
            detail="Le jour de la semaine doit être compris entre 0 (lundi) et 6 (dimanche).",
        )

    # Validation des chevauchements avant toute écriture.
    _validate_no_overlap(slots, day_of_week)

    # [⚠️ PROD] get_or_create_config AVANT le lock : garantit l'existence de la ligne
    # TenantConfig pour que le SELECT FOR UPDATE trouve une cible non nulle.
    await get_or_create_config(session)

    # [⚠️ PROD] Lock pessimiste — sérialise les écritures concurrentes sur les horaires.
    # Deux admins modifiant simultanément le même jour attendront chacun leur tour.
    await session.execute(select(TenantConfig).with_for_update())

    # Capturer les anciens créneaux pour l'audit avant le DELETE.
    old_result = await session.execute(
        select(BusinessHours)
        .where(BusinessHours.day_of_week == day_of_week)
        .order_by(BusinessHours.slot_index)
    )
    old_slots = list(old_result.scalars().all())

    # DELETE + INSERT atomique.
    await session.execute(
        delete(BusinessHours).where(BusinessHours.day_of_week == day_of_week)
    )

    new_hours: list[BusinessHours] = []
    for slot in slots:
        bh = BusinessHours(
            day_of_week=day_of_week,
            slot_index=slot.slot_index,
            opens_at=slot.opens_at,
            closes_at=slot.closes_at,
            is_active=True,
        )
        session.add(bh)
        new_hours.append(bh)

    # Audit — log du remplacement complet des créneaux du jour.
    if user_id is not None:
        old_json = json.dumps(
            [
                {"slot_index": h.slot_index, "opens_at": str(h.opens_at), "closes_at": str(h.closes_at)}
                for h in old_slots
            ]
        )
        new_json = json.dumps(
            [
                {"slot_index": s.slot_index, "opens_at": str(s.opens_at), "closes_at": str(s.closes_at)}
                for s in slots
            ]
        )
        await _write_audit(
            session,
            user_id=user_id,
            field_name=f"business_hours_day_{day_of_week}",
            old_value=old_json,
            new_value=new_json,
            ip_address=ip_address,
            user_agent=user_agent,
            user_email=user_email,
        )

    await session.commit()
    for bh in new_hours:
        await session.refresh(bh)

    return sorted(new_hours, key=lambda h: h.slot_index)


async def delete_business_hours_day(session: AsyncSession, day_of_week: int) -> None:
    """Supprime tous les créneaux d'un jour de la semaine.

    Args:
        session: Session SQLAlchemy active sur le schema tenant courant.
        day_of_week: Jour de la semaine (0=lundi, 6=dimanche).
    """
    await session.execute(
        delete(BusinessHours).where(BusinessHours.day_of_week == day_of_week)
    )
    await session.commit()


def _validate_no_overlap(slots: list[BusinessHoursCreate], day_of_week: int) -> None:
    """Vérifie que les créneaux ne se chevauchent pas.

    Args:
        slots: Créneaux à valider.
        day_of_week: Jour concerné (utilisé dans le message d'erreur uniquement).

    Raises:
        HTTPException: 422 si au moins deux créneaux se chevauchent.
    """
    sorted_slots = sorted(slots, key=lambda s: s.opens_at)
    for i in range(len(sorted_slots) - 1):
        current = sorted_slots[i]
        nxt = sorted_slots[i + 1]
        if current.closes_at > nxt.opens_at:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Chevauchement détecté pour le jour {day_of_week} : "
                    f"{current.opens_at}–{current.closes_at} "
                    f"chevauche {nxt.opens_at}–{nxt.closes_at}."
                ),
            )


# ---------------------------------------------------------------------------
# Exceptional closures
# ---------------------------------------------------------------------------


async def get_exceptional_closures(session: AsyncSession) -> list[ExceptionalClosure]:
    """Retourne toutes les fermetures exceptionnelles triées par date.

    Args:
        session: Session SQLAlchemy active sur le schema tenant courant.

    Returns:
        Liste de ``ExceptionalClosure`` triée par ``closure_date`` croissant.
    """
    result = await session.execute(
        select(ExceptionalClosure).order_by(ExceptionalClosure.closure_date)
    )
    return list(result.scalars().all())


async def add_exceptional_closure(
    session: AsyncSession,
    data: ExceptionalClosureCreate,
) -> ExceptionalClosure:
    """Crée une fermeture exceptionnelle pour la date donnée.

    Args:
        session: Session SQLAlchemy active sur le schema tenant courant.
        data: Données de la fermeture à créer.

    Returns:
        Instance ``ExceptionalClosure`` persistée.

    Raises:
        HTTPException: 422 si la date est dans le passé.
        HTTPException: 409 si une fermeture existe déjà pour cette date.
    """
    # [⚠️ PROD] Comparaison date locale selon timezone configurée, pas UTC.
    config = await get_or_create_config(session)
    today = datetime.now(pytz.timezone(config.timezone)).date()
    if data.closure_date <= today:
        raise HTTPException(
            status_code=422,
            detail="La date de fermeture doit être dans le futur.",
        )

    existing = await session.scalar(
        select(ExceptionalClosure).where(ExceptionalClosure.closure_date == data.closure_date)
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Une fermeture exceptionnelle existe déjà pour le {data.closure_date}.",
        )

    closure = ExceptionalClosure(
        closure_date=data.closure_date,
        # Si use_default_message=True, on ne stocke pas de custom_message.
        # [🔒 SÉCURITÉ] Sanitisation XSS du message personnalisé.
        custom_message=None if data.use_default_message else _sanitize_message(data.custom_message),
        use_default_message=data.use_default_message,
    )
    session.add(closure)
    await session.commit()
    await session.refresh(closure)
    return closure


async def delete_exceptional_closure(session: AsyncSession, closure_id: int) -> None:
    """Supprime une fermeture exceptionnelle par son identifiant.

    Args:
        session: Session SQLAlchemy active sur le schema tenant courant.
        closure_id: Identifiant de la fermeture à supprimer.

    Raises:
        HTTPException: 404 si la fermeture est introuvable.
    """
    closure = await session.get(ExceptionalClosure, closure_id)
    if closure is None:
        raise HTTPException(status_code=404, detail="Fermeture exceptionnelle introuvable.")
    await session.delete(closure)
    await session.commit()


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


async def get_audit_log(
    session: AsyncSession,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[TenantConfigAudit], int]:
    """Retourne les entrées d'audit paginées, triées par date décroissante.

    Args:
        session: Session SQLAlchemy active sur le schema tenant courant.
        limit: Nombre maximum d'entrées à retourner.
        offset: Décalage pour la pagination.

    Returns:
        Tuple (liste d'entrées d'audit, total d'entrées).
    """
    total = await session.scalar(select(func.count(TenantConfigAudit.id))) or 0
    result = await session.execute(
        select(TenantConfigAudit)
        .order_by(TenantConfigAudit.changed_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all()), total


# ---------------------------------------------------------------------------
# Active orders count
# ---------------------------------------------------------------------------


async def get_active_orders_count(session: AsyncSession) -> int:
    """Compte les commandes actives (statuts 'confirmed' et 'in_preparation').

    Args:
        session: Session SQLAlchemy active sur le schema tenant courant.

    Returns:
        Nombre de commandes actives.
    """
    result = await session.scalar(
        select(func.count(Order.id)).where(Order.status.in_(_ACTIVE_ORDER_STATUSES))
    )
    return result or 0


# ---------------------------------------------------------------------------
# Tenant status — vue publique
# ---------------------------------------------------------------------------


async def get_tenant_status(session: AsyncSession) -> TenantStatusResponse:
    """Calcule et retourne le statut opérationnel courant du restaurant.

    Logique d'ouverture (priorité décroissante) :
    1. Fermeture manuelle (``is_temporarily_closed``) → fermé.
    2. Fermeture exceptionnelle du jour → fermé.
    3. Créneaux horaires du jour → ouvert si l'heure actuelle est dans un créneau actif.

    Le temps de préparation estimé tient compte du nombre de commandes actives
    si ``auto_calc_prep_time`` est activé.

    Args:
        session: Session SQLAlchemy active sur le schema tenant courant.

    Returns:
        Instance ``TenantStatusResponse`` prête à sérialiser.
    """
    config = await get_or_create_config(session)
    active_count = await get_active_orders_count(session)

    # [⚠️ PROD] Toujours résoudre l'heure en timezone configurée, jamais datetime.now().
    now_paris = datetime.now(pytz.timezone(config.timezone))
    today = now_paris.date()
    current_time = now_paris.time()

    # --- Calcul du temps de préparation ---
    prep_time = _compute_prep_time(config, active_count)

    # --- Vérification fermeture manuelle ---
    if config.is_temporarily_closed:
        next_opening = await _compute_next_opening(session, today, current_time, skip_today=True)
        return TenantStatusResponse(
            is_open=False,
            estimated_prep_time_minutes=prep_time,
            message=config.temporary_closure_message or config.default_closure_message,
            next_opening=next_opening,
            active_orders_count=active_count,
        )

    # --- Vérification fermeture exceptionnelle du jour ---
    exc_today = await session.scalar(
        select(ExceptionalClosure).where(ExceptionalClosure.closure_date == today)
    )
    if exc_today is not None:
        message = _resolve_exceptional_message(exc_today, config)
        next_opening = await _compute_next_opening(session, today, current_time, skip_today=True)
        return TenantStatusResponse(
            is_open=False,
            estimated_prep_time_minutes=prep_time,
            message=message,
            next_opening=next_opening,
            active_orders_count=active_count,
        )

    # --- Vérification des créneaux horaires du jour ---
    today_weekday = today.weekday()  # 0=lundi, 6=dimanche
    result = await session.execute(
        select(BusinessHours)
        .where(BusinessHours.day_of_week == today_weekday, BusinessHours.is_active.is_(True))
        .order_by(BusinessHours.slot_index)
    )
    today_hours = list(result.scalars().all())

    is_open = any(h.opens_at <= current_time <= h.closes_at for h in today_hours)

    if is_open:
        return TenantStatusResponse(
            is_open=True,
            estimated_prep_time_minutes=prep_time,
            message=None,
            next_opening=None,
            active_orders_count=active_count,
        )

    # Fermé en dehors des horaires habituels — pas de message particulier.
    next_opening = await _compute_next_opening(session, today, current_time, skip_today=False)
    return TenantStatusResponse(
        is_open=False,
        estimated_prep_time_minutes=prep_time,
        message=None,
        next_opening=next_opening,
        active_orders_count=active_count,
    )


async def get_next_opening(session: AsyncSession) -> str | None:
    """Retourne le prochain horaire d'ouverture sans calculer le statut complet.

    Args:
        session: Session SQLAlchemy active sur le schema tenant courant.

    Returns:
        Chaîne lisible ou None si aucun créneau dans les 7 prochains jours.
    """
    config = await get_or_create_config(session)
    now_paris = datetime.now(pytz.timezone(config.timezone))
    return await _compute_next_opening(
        session, now_paris.date(), now_paris.time(), skip_today=False
    )


# ---------------------------------------------------------------------------
# Helpers privés
# ---------------------------------------------------------------------------


def _compute_prep_time(config: TenantConfig, active_count: int) -> int:
    """Calcule le temps de préparation estimé selon la charge active.

    Args:
        config: Configuration tenant courante.
        active_count: Nombre de commandes actives.

    Returns:
        Temps estimé en minutes.
    """
    if not config.auto_calc_prep_time:
        return config.prep_time_normal_minutes

    if active_count >= config.peak_orders_threshold:
        return (
            config.prep_time_peak_minutes
            + (active_count - config.peak_orders_threshold) * config.overhead_per_order_minutes
        )
    return config.prep_time_normal_minutes + active_count * config.overhead_per_order_minutes


def _resolve_exceptional_message(exc: ExceptionalClosure, config: TenantConfig) -> str:
    """Retourne le message à afficher pour une fermeture exceptionnelle.

    Args:
        exc: Fermeture exceptionnelle du jour.
        config: Configuration tenant pour le message par défaut.

    Returns:
        Message de fermeture résolu.
    """
    if exc.use_default_message or not exc.custom_message:
        return config.default_closure_message
    return exc.custom_message


def _format_opening_time(h: BusinessHours) -> str:
    """Formate l'heure d'ouverture d'un créneau en français (ex: "18h30", "9h00").

    Args:
        h: Créneau horaire.

    Returns:
        Chaîne formatée.
    """
    return f"{h.opens_at.hour}h{h.opens_at.minute:02d}"


async def _compute_next_opening(
    session: AsyncSession,
    today: date,
    current_time,
    *,
    skip_today: bool,
) -> str | None:
    """Parcourt les 7 prochains jours pour trouver le premier créneau disponible.

    Filtre les fermetures exceptionnelles. Ne tient pas compte de
    ``is_temporarily_closed`` (logique appelante).

    Args:
        session: Session SQLAlchemy active.
        today: Date du jour (timezone Paris).
        current_time: Heure courante (timezone Paris).
        skip_today: Si True, ignore complètement les créneaux d'aujourd'hui
            (cas fermeture manuelle ou fermeture exceptionnelle).

    Returns:
        Chaîne lisible ("Aujourd'hui 18h30", "Demain 11h00", "Mardi 11h00")
        ou None si aucun créneau dans les 7 prochains jours.
    """
    # Récupération en masse pour éviter N+1.
    all_hours_result = await session.execute(
        select(BusinessHours)
        .where(BusinessHours.is_active.is_(True))
        .order_by(BusinessHours.day_of_week, BusinessHours.opens_at)
    )
    all_hours = list(all_hours_result.scalars().all())
    hours_by_day: dict[int, list[BusinessHours]] = {}
    for h in all_hours:
        hours_by_day.setdefault(h.day_of_week, []).append(h)

    # Fermetures exceptionnelles sur les 7 prochains jours.
    future_dates = [today + timedelta(days=i) for i in range(8)]
    exc_result = await session.execute(
        select(ExceptionalClosure.closure_date).where(
            ExceptionalClosure.closure_date.in_(future_dates)
        )
    )
    exc_dates: set[date] = {row[0] for row in exc_result}

    for delta in range(8):
        check_date = today + timedelta(days=delta)

        if delta == 0 and skip_today:
            continue
        if check_date in exc_dates:
            continue

        day_of_week = check_date.weekday()
        day_hours = hours_by_day.get(day_of_week, [])

        for h in sorted(day_hours, key=lambda x: x.opens_at):
            # Pour aujourd'hui, seuls les créneaux encore à venir comptent.
            if delta == 0 and h.opens_at <= current_time:
                continue

            time_str = _format_opening_time(h)
            if delta == 0:
                return f"Aujourd'hui {time_str}"
            if delta == 1:
                return f"Demain {time_str}"
            return f"{_DAY_NAMES_FR[day_of_week]} {time_str}"

# ---------------------------------------------------------------------------
# Branding public (Plan 02)
# ---------------------------------------------------------------------------


async def get_branding(session: AsyncSession) -> TenantBrandingResponse:
    """Retourne les données de branding du tenant.

    [⚠️ PROD] Endpoint public — ne retourne que TenantBrandingResponse,
    jamais l'objet TenantConfig complet qui contient des données opérationnelles.

    Args:
        session: Session async sur le schema du tenant concerné.

    Returns:
        TenantBrandingResponse avec les 5 champs branding (tous nullable).
    """
    config = await get_or_create_config(session)
    return TenantBrandingResponse.model_validate(config)


async def update_branding(
    session: AsyncSession,
    data: TenantBrandingUpdate,
    *,
    user_id: int,
    user_email: str | None,
    ip_address: str | None,
    user_agent: str,
) -> TenantBrandingResponse:
    """Met à jour partiellement les champs branding du tenant.

    Seuls les champs non-None dans `data` sont modifiés (patch sémantique).
    Chaque modification est tracée dans TenantConfigAudit.

    Args:
        session: Session async sur le schema du tenant.
        data: TenantBrandingUpdate avec les champs à modifier.
        user_id: ID de l'admin effectuant la modification.
        user_email: Email de l'admin (dénormalisé pour l'audit).
        ip_address: IP de la requête.
        user_agent: User-Agent de la requête.

    Returns:
        TenantBrandingResponse après mise à jour.
    """
    config = await get_or_create_config(session)

    branding_fields = ("display_name", "logo_url", "primary_color", "secondary_color", "font_family")
    updates = data.model_dump(exclude_none=True)

    for field in branding_fields:
        if field not in updates:
            continue
        old_value = getattr(config, field)
        new_value = updates[field]
        setattr(config, field, new_value)
        await _write_audit(
            session,
            user_id=user_id,
            field_name=f"branding.{field}",
            old_value=str(old_value) if old_value is not None else None,
            new_value=str(new_value),
            ip_address=ip_address,
            user_agent=user_agent,
            user_email=user_email,
        )

    await session.commit()
    await session.refresh(config)
    return TenantBrandingResponse.model_validate(config)
