"""Logging structure (JSON) + correlation ID de requete.

[PROD] Sans formatter dedie, les champs passes via ``logger.info(..., extra={...})``
sont attaches au ``LogRecord`` mais jamais rendus par le formatter texte par
defaut de Python -- ils restaient donc invisibles dans les logs bruts malgre
le middleware de mesure de duree deja en place. Ce module corrige ca : chaque
ligne de log est emise en JSON avec les champs structures + un ``request_id``
de correlation propage via ``ContextVar``, sans avoir a modifier chaque appel
``logger.*`` existant dans le codebase.
"""
import json
import logging
from contextvars import ContextVar

_request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")

# Attributs standards d'un LogRecord -- tout le reste passe par **extra** et
# doit etre inclus dans la sortie JSON.
_STANDARD_RECORD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys())


def set_request_id(request_id: str) -> None:
    """Associe ``request_id`` a toutes les lignes de log emises dans ce contexte async."""
    _request_id_ctx.set(request_id)


def get_request_id() -> str:
    """Retourne le ``request_id`` courant (ou ``"-"`` hors contexte de requete)."""
    return _request_id_ctx.get()


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id()
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_ATTRS and key != "request_id":
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Configure le root logger en JSON structure avec request_id de correlation.

    Idempotent -- reinitialise les handlers existants pour eviter les logs
    dupliques si appele plusieurs fois (tests, reload uvicorn).
    """
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter())
    handler.addFilter(_RequestIdFilter())
    root.addHandler(handler)
