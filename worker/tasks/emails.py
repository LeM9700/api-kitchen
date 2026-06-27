import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.core.config import settings
from app.core.database import get_tenant_session
from app.modules.auth.models import User
from worker.tasks.worker_utils import with_dead_letter

logger = logging.getLogger(__name__)


def _send_smtp(to: str, subject: str, body: str) -> None:
    """Envoie un email via SMTP synchrone.

    Utilise STARTTLS sur le port configure. Cette fonction est synchrone ;
    dans le contexte ARQ (worker separe) l'appel bloquant est acceptable.

    [PROD] Pour un worker haute frequence, envisager asyncio.get_event_loop().run_in_executor.

    Args:
        to: Adresse email du destinataire.
        subject: Sujet du message.
        body: Corps du message en texte plain.
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from
    msg["To"] = to
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password)
        server.sendmail(settings.smtp_from, [to], msg.as_string())


@with_dead_letter
async def send_email(ctx, to: str, subject: str, body: str) -> None:
    """Task ARQ generique d'envoi d'email.

    Envoie via SMTP si settings.smtp_host est configure, sinon logue
    (graceful degradation pour les environnements sans SMTP).

    Args:
        ctx: Contexte ARQ injecte automatiquement.
        to: Adresse email du destinataire.
        subject: Sujet du message.
        body: Corps du message (texte plain).
    """
    if not settings.smtp_host:
        logger.info("EMAIL (SMTP non configure) to=%s subject=%s", to, subject)
        return

    try:
        _send_smtp(to, subject, body)
        logger.info("EMAIL envoye to=%s subject=%s", to, subject)
    except Exception as exc:
        logger.error("EMAIL echec to=%s subject=%s error=%s", to, subject, exc)
        raise  # Propage pour que ARQ puisse retry


@with_dead_letter
async def send_verification_email(ctx, tenant_slug: str, user_id: int, token: str) -> None:
    """Task ARQ : envoie le lien de verification d'adresse email a un nouvel utilisateur.

    Construit l'URL de verification, recupere l'email depuis la base tenant,
    puis envoie via SMTP (ou logue si SMTP non configure).

    Args:
        ctx: Contexte ARQ injecte automatiquement.
        tenant_slug: Slug du tenant (pour router vers le bon schema).
        user_id: Identifiant de l'utilisateur a verifier.
        token: Token UUID4 de verification genere a l'inscription.
    """
    verify_url = (
        f"{settings.app_base_url}/api/v1/auth/verify-email"
        f"?token={token}&tenant_slug={tenant_slug}"
    )

    async with get_tenant_session(tenant_slug) as session:
        user = await session.get(User, user_id)
        if user is None:
            logger.warning(
                "send_verification_email: user_id=%s introuvable dans tenant=%s",
                user_id,
                tenant_slug,
            )
            return
        to_email = user.email

    subject = "Verifiez votre adresse email"
    body = (
        "Bonjour,\n\n"
        "Cliquez sur le lien ci-dessous pour verifier votre compte (valide 24h) :\n\n"
        f"{verify_url}\n\n"
        "Si vous n'avez pas cree de compte, ignorez cet email."
    )

    if not settings.smtp_host:
        logger.info(
            "VERIFICATION EMAIL (SMTP non configure) to=%s url=%s",
            to_email,
            verify_url,
        )
        return

    try:
        _send_smtp(to_email, subject, body)
        logger.info("VERIFICATION EMAIL envoye to=%s user_id=%s", to_email, user_id)
    except Exception as exc:
        logger.error(
            "VERIFICATION EMAIL echec to=%s user_id=%s error=%s",
            to_email,
            user_id,
            exc,
        )
        raise  # Propage pour que ARQ puisse retry


@with_dead_letter
async def send_stock_alert_email(
    ctx,
    tenant_slug: str,
    ingredient_id: int,
    ingredient_name: str,
    current_qty: float,
) -> None:
    """Task ARQ : envoie l'email d'alerte stock a l'administrateur du tenant.

    Meme pattern que send_verification_email : SMTP reel si configure, sinon log.

    Args:
        ctx: Contexte ARQ injecte automatiquement.
        tenant_slug: Slug du tenant concerne.
        ingredient_id: Cle primaire de l'ingredient en alerte.
        ingredient_name: Nom de l'ingredient pour le corps du message.
        current_qty: Quantite courante au moment de l'alerte.
    """
    subject = f"[STOCK ALERT] {ingredient_name} stock bas - {tenant_slug}"
    body = (
        f"Alerte stock pour le tenant {tenant_slug} :\n\n"
        f"Ingredient : {ingredient_name} (id={ingredient_id})\n"
        f"Quantite actuelle : {current_qty}\n\n"
        "Veuillez reapprovisionner cet ingredient."
    )

    if not settings.smtp_host:
        logger.warning(
            "[%s] STOCK ALERT EMAIL (SMTP non configure): %s (id=%s) = %s",
            tenant_slug,
            ingredient_name,
            ingredient_id,
            current_qty,
        )
        return

    try:
        _send_smtp(settings.smtp_from, subject, body)
        logger.info(
            "STOCK ALERT EMAIL envoye tenant=%s ingredient=%s",
            tenant_slug,
            ingredient_name,
        )
    except Exception as exc:
        logger.error(
            "STOCK ALERT EMAIL echec tenant=%s ingredient=%s error=%s",
            tenant_slug,
            ingredient_name,
            exc,
        )
        raise


@with_dead_letter
async def send_password_reset_email(ctx, tenant_slug: str, user_id: int, token: str) -> None:
    """Task ARQ : envoie le code de réinitialisation de mot de passe a l'utilisateur.

    Recupere l'email depuis la base tenant, puis envoie le code via SMTP
    (ou logue si SMTP non configure).

    Args:
        ctx: Contexte ARQ injecte automatiquement.
        tenant_slug: Slug du tenant (pour router vers le bon schema).
        user_id: Identifiant de l'utilisateur.
        token: Code de reinitialisation en plaintext (8 caracteres).
    """
    async with get_tenant_session(tenant_slug) as session:
        user = await session.get(User, user_id)
        if user is None:
            logger.warning(
                "send_password_reset_email: user_id=%s introuvable dans tenant=%s",
                user_id,
                tenant_slug,
            )
            return
        to_email = user.email

    subject = "Réinitialisation de votre mot de passe"
    body = (
        f"Votre code de réinitialisation de mot de passe est : {token}\n\n"
        f"Ce code expire dans 30 minutes.\n"
        f"Si vous n'avez pas demandé de réinitialisation, ignorez cet email."
    )

    if not settings.smtp_host:
        logger.info(
            "PASSWORD RESET EMAIL (SMTP non configure) to=%s user_id=%s code=%s",
            to_email,
            user_id,
            token,
        )
        return

    try:
        _send_smtp(to_email, subject, body)
        logger.info("PASSWORD RESET EMAIL envoye to=%s user_id=%s", to_email, user_id)
    except Exception as exc:
        logger.error(
            "PASSWORD RESET EMAIL echec to=%s user_id=%s error=%s",
            to_email,
            user_id,
            exc,
        )
        raise  # Propage pour que ARQ puisse retry


@with_dead_letter
async def notify_config_change(ctx, *, tenant_slug: str, is_closed: bool) -> None:
    """Task ARQ : notifie les admins et le staff d'un changement de statut de fermeture.

    Envoie un email a tous les admins du tenant ET une notification push a tous
    les tokens staff/admin actifs.

    Args:
        ctx: Contexte ARQ injecte automatiquement.
        tenant_slug: Slug du tenant concerne.
        is_closed: True si le restaurant vient de fermer, False s'il rouvre.
    """
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.config import settings as _settings
    from app.core.database import tenant_schema_name
    from app.modules.auth.models import User
    from app.modules.notifications.notification_service import notify_staff

    status_label = "FERME" if is_closed else "OUVERT"
    subject = f"[{tenant_slug}] Statut restaurant modifie"
    body = (
        f"Le restaurant {tenant_slug!r} est maintenant {status_label}.\n\n"
        "Ce message est automatique suite a une modification de configuration."
    )

    engine = create_async_engine(_settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    schema = tenant_schema_name(tenant_slug)

    try:
        async with session_factory() as session:
            await session.execute(text(f'SET search_path TO "{schema}", public'))

            # Email aux admins.
            admin_result = await session.execute(
                select(User).where(User.role == "admin", User.is_active.is_(True))
            )
            admins = list(admin_result.scalars().all())

            for admin in admins:
                if not _settings.smtp_host:
                    logger.info(
                        "notify_config_change (SMTP non configure): %s -> %s",
                        admin.email,
                        subject,
                    )
                else:
                    try:
                        _send_smtp(admin.email, subject, body)
                    except Exception as exc:
                        logger.error(
                            "notify_config_change email echec to=%s: %s",
                            admin.email,
                            exc,
                        )

            # Push staff + admin.
            try:
                await notify_staff(
                    session=session,
                    tenant_slug=tenant_slug,
                    event="tenant.status_changed",
                    title="Statut restaurant",
                    body=f"Le restaurant est maintenant {status_label}",
                    data={"is_closed": is_closed},
                )
            except Exception as exc:
                logger.error(
                    "notify_config_change push echec tenant=%s: %s", tenant_slug, exc
                )

    finally:
        await engine.dispose()
