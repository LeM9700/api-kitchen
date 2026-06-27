"""Utilitaires ARQ : dead-letter handler et decorator with_dead_letter.

Pattern :
- with_dead_letter wraps chaque task et intercepte l'exception sur la derniere
  tentative (job_try >= WorkerSettings.max_tries).
- Sur echec definitif : insertion dans MongoDB failed_jobs_{tenant_slug} +
  log JSON structure.
- dead_letter_handler est aussi enregistre comme task ARQ pour etre appelable
  manuellement depuis d'autres contextes.
"""

import functools
import json
import logging
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import settings

logger = logging.getLogger(__name__)

# Doit correspondre a WorkerSettings.max_tries (worker/main.py).
_MAX_TRIES: int = 3


async def _write_dead_letter(
    ctx: dict,
    func_name: str,
    args: tuple,
    kwargs: dict,
    error: Exception,
) -> None:
    """Insere un job echoue dans la collection MongoDB failed_jobs_{tenant_slug}.

    Args:
        ctx: Contexte ARQ du job en echec.
        func_name: Nom qualifie de la fonction du job.
        args: Arguments positionnels du job.
        kwargs: Arguments nommes du job.
        error: Exception levee lors du dernier essai.
    """
    tenant_slug = kwargs.get("tenant_slug", "unknown")
    job_id = ctx.get("job_id", "unknown")

    document = {
        "job_id": str(job_id),
        "function": func_name,
        "args": _safe_serialize(args),
        "kwargs": _safe_serialize(kwargs),
        "error": str(error),
        "error_type": type(error).__name__,
        "job_try": ctx.get("job_try", _MAX_TRIES),
        "failed_at": datetime.now(timezone.utc).isoformat(),
        "tenant_slug": tenant_slug,
    }

    logger.error(
        "DEAD_LETTER job_id=%s function=%s tenant=%s error=%s",
        job_id,
        func_name,
        tenant_slug,
        error,
        extra={"dead_letter": document},
    )

    client: AsyncIOMotorClient | None = None
    try:
        client = AsyncIOMotorClient(settings.mongo_url)
        collection = client[settings.mongo_db][f"failed_jobs_{tenant_slug}"]
        await collection.insert_one(document)
    except Exception as mongo_exc:
        # Le dead-letter handler ne doit jamais lever -- on logue et on continue.
        logger.error("DEAD_LETTER: echec ecriture MongoDB: %s", mongo_exc)
    finally:
        if client is not None:
            client.close()


def _safe_serialize(obj) -> list | dict | str:
    """Serialise args/kwargs de facon sure pour MongoDB (pas de types non-serializables).

    Args:
        obj: Objet a serialiser (tuple, dict, ou autre).

    Returns:
        Representation JSON-safe.
    """
    try:
        if isinstance(obj, (tuple, list)):
            return [_safe_serialize(x) for x in obj]
        if isinstance(obj, dict):
            return {str(k): _safe_serialize(v) for k, v in obj.items()}
        # Test rapide de serializabilite.
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return str(obj)


def with_dead_letter(func):
    """Decorateur ARQ : ecrit en dead-letter MongoDB sur la derniere tentative d'echec.

    Usage :
        @with_dead_letter
        async def my_task(ctx, ...):
            ...

    Sur job_try < max_tries : l'exception est re-levee normalement (ARQ retry).
    Sur job_try >= max_tries : dead-letter MongoDB + re-leve (ARQ marque le job failed).

    [PROD] Le decorateur lit job_try depuis ctx (injecte par ARQ, 1-indexe).
    _MAX_TRIES doit correspondre a WorkerSettings.max_tries.

    Args:
        func: Fonction ARQ asynchrone a wrapper.

    Returns:
        Fonction wrappee avec gestion dead-letter.
    """
    @functools.wraps(func)
    async def wrapper(ctx, *args, **kwargs):
        try:
            return await func(ctx, *args, **kwargs)
        except Exception as exc:
            job_try = ctx.get("job_try", 1)
            if job_try >= _MAX_TRIES:
                await _write_dead_letter(ctx, func.__qualname__, args, kwargs, exc)
            raise  # Toujours re-lever pour que ARQ gere le statut du job

    return wrapper


async def dead_letter_handler(
    ctx: dict,
    job_id: str,
    function: str,
    args: list,
    kwargs: dict,
    error: str,
) -> None:
    """Task ARQ : enregistre manuellement un job en dead-letter MongoDB.

    Peut etre appelee explicitement depuis d'autres tasks ou mecanismes de monitoring.
    Pour l'usage automatique sur echec, preferer le decorateur with_dead_letter.

    Args:
        ctx: Contexte ARQ injecte automatiquement.
        job_id: Identifiant ARQ du job echoue.
        function: Nom qualifie de la fonction du job.
        args: Arguments positionnels du job.
        kwargs: Arguments nommes du job.
        error: Message d'erreur serialise.
    """
    tenant_slug = kwargs.get("tenant_slug", "unknown") if isinstance(kwargs, dict) else "unknown"
    document = {
        "job_id": str(job_id),
        "function": function,
        "args": _safe_serialize(args),
        "kwargs": _safe_serialize(kwargs),
        "error": str(error),
        "failed_at": datetime.now(timezone.utc).isoformat(),
        "tenant_slug": tenant_slug,
    }

    logger.error(
        "DEAD_LETTER (explicit) job_id=%s function=%s error=%s",
        job_id,
        function,
        error,
    )

    client: AsyncIOMotorClient | None = None
    try:
        client = AsyncIOMotorClient(settings.mongo_url)
        collection = client[settings.mongo_db][f"failed_jobs_{tenant_slug}"]
        await collection.insert_one(document)
    except Exception as mongo_exc:
        logger.error("DEAD_LETTER handler: echec MongoDB: %s", mongo_exc)
    finally:
        if client is not None:
            client.close()
