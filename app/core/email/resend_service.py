"""Service d'envoi d'emails via l'API Resend.

[⚠️ PROD] Définir RESEND_API_KEY dans les variables d'environnement.
Si RESEND_API_KEY est vide, l'envoi est silencieusement ignoré (graceful
degradation) — l'application continue de fonctionner sans email.

Usage :
    from app.core.email.resend_service import send_email, send_tenant_suspended

    await send_tenant_suspended(
        admin_email="admin@restaurant.com",
        tenant_name="Pizza Roma",
        reason="Non-paiement",
    )
"""

import logging
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_RESEND_ENDPOINT = "https://api.resend.com/emails"


async def send_email(
    to: str,
    subject: str,
    html: str,
    reply_to: str | None = None,
) -> bool:
    """Envoie un email via l'API Resend.

    [⚠️ PROD] Ne jamais laisser une erreur d'email bloquer un flux métier —
    toujours appeler de façon non-bloquante (try/except au niveau appelant).

    Args:
        to: Adresse email du destinataire.
        subject: Objet de l'email.
        html: Corps HTML de l'email.
        reply_to: Adresse de réponse optionnelle.

    Returns:
        True si envoi réussi, False sinon (clé absente, erreur réseau, etc.).
    """
    if not settings.resend_api_key:
        logger.debug("RESEND_API_KEY absent — email non envoyé à %s", to)
        return False

    payload: dict[str, Any] = {
        "from": settings.resend_from_email,
        "to": [to],
        "subject": subject,
        "html": html,
    }
    if reply_to:
        payload["reply_to"] = reply_to

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                _RESEND_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {settings.resend_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            logger.info("Email envoyé à %s — sujet: %s", to, subject)
            return True
    except httpx.HTTPStatusError as exc:
        logger.error("Resend HTTP error %s → %s", exc.response.status_code, exc.response.text)
    except Exception as exc:
        logger.error("Resend send error: %s", exc)
    return False


# ─── Templates métier ────────────────────────────────────────────────────────


async def send_tenant_suspended(
    admin_email: str,
    tenant_name: str,
    reason: str,
) -> bool:
    """Email envoyé à l'admin du tenant lorsque son accès est suspendu.

    Args:
        admin_email: Email de l'administrateur du restaurant.
        tenant_name: Nom du restaurant.
        reason: Raison de la suspension saisie par le super-admin.

    Returns:
        True si l'email a été envoyé avec succès.
    """
    html = f"""
    <div style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:24px">
      <h2 style="color:#dc2626">Accès suspendu — {tenant_name}</h2>
      <p>Bonjour,</p>
      <p>L'accès à votre restaurant <strong>{tenant_name}</strong> sur la plateforme
      a été <strong>suspendu temporairement</strong>.</p>
      <div style="background:#fef2f2;border:1px solid #fecaca;border-radius:8px;padding:16px;margin:16px 0">
        <p style="margin:0;color:#991b1b"><strong>Raison :</strong> {reason}</p>
      </div>
      <p>Pour toute question, contactez le support plateforme en répondant à cet email.</p>
      <p style="color:#6b7280;font-size:12px;margin-top:32px">
        Cet email a été envoyé automatiquement par la plateforme Pizza Platform.
      </p>
    </div>
    """
    return await send_email(
        to=admin_email,
        subject=f"[Pizza Platform] Accès suspendu — {tenant_name}",
        html=html,
    )


async def send_tenant_unsuspended(
    admin_email: str,
    tenant_name: str,
) -> bool:
    """Email envoyé à l'admin du tenant lorsque son accès est réactivé.

    Args:
        admin_email: Email de l'administrateur du restaurant.
        tenant_name: Nom du restaurant.

    Returns:
        True si l'email a été envoyé avec succès.
    """
    html = f"""
    <div style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:24px">
      <h2 style="color:#16a34a">Accès réactivé — {tenant_name}</h2>
      <p>Bonjour,</p>
      <p>L'accès à votre restaurant <strong>{tenant_name}</strong> sur la plateforme
      a été <strong>réactivé</strong>. Vous pouvez vous connecter normalement.</p>
      <p style="color:#6b7280;font-size:12px;margin-top:32px">
        Cet email a été envoyé automatiquement par la plateforme Pizza Platform.
      </p>
    </div>
    """
    return await send_email(
        to=admin_email,
        subject=f"[Pizza Platform] Accès réactivé — {tenant_name}",
        html=html,
    )


async def send_temp_password_reset(
    user_email: str,
    tenant_name: str,
    temp_password: str,
) -> bool:
    """Email envoyé à un utilisateur tenant dont le mot de passe a été réinitialisé.

    [🔒 SÉCURITÉ] Le mot de passe temporaire est transmis en clair dans l'email —
    l'utilisateur doit le changer à la première connexion (must_change_password=True).

    Args:
        user_email: Email de l'utilisateur.
        tenant_name: Nom du restaurant.
        temp_password: Mot de passe temporaire généré.

    Returns:
        True si l'email a été envoyé avec succès.
    """
    html = f"""
    <div style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:24px">
      <h2 style="color:#4361ee">Réinitialisation de mot de passe — {tenant_name}</h2>
      <p>Bonjour,</p>
      <p>Votre mot de passe pour <strong>{tenant_name}</strong> a été réinitialisé
      par l'administrateur plateforme.</p>
      <div style="background:#f0f4ff;border:1px solid #c7d2fe;border-radius:8px;padding:16px;margin:16px 0">
        <p style="margin:0 0 8px;color:#4338ca;font-size:12px;font-weight:600;text-transform:uppercase">
          Mot de passe temporaire
        </p>
        <code style="font-size:18px;font-weight:700;color:#1e1b4b;letter-spacing:0.05em">
          {temp_password}
        </code>
      </div>
      <p style="color:#dc2626;font-size:13px">
        ⚠️ Vous serez invité à changer ce mot de passe à votre prochaine connexion.
      </p>
      <p style="color:#6b7280;font-size:12px;margin-top:32px">
        Si vous n'avez pas demandé cette réinitialisation, contactez immédiatement le support.
      </p>
    </div>
    """
    return await send_email(
        to=user_email,
        subject=f"[Pizza Platform] Nouveau mot de passe temporaire — {tenant_name}",
        html=html,
    )
