"""Locale de requete (FR/EN) propagee via ContextVar — meme pattern que
request_id (voir app/core/http/logging_config.py). Necessaire car la
resolution de locale se fait dans un middleware mais est consommee par des
chaines d'appel profondes (payments/service.py, orders/service.py,
core/email/resend_service.py) qui ne recoivent jamais l'objet Request.

Resolution "legere" : seul le header Accept-Language est lu ici (voir
app.main._resolve_locale), sans acces DB. TenantConfig.default_language
n'est consulte qu'aux points d'usage qui en ont reellement besoin (ex: les
emails transactionnels, qui doivent parler la langue du tenant destinataire
et non celle de l'appelant) — voir translate.t(..., locale=...).
"""
from contextvars import ContextVar

SUPPORTED_LOCALES: frozenset[str] = frozenset({"fr", "en"})
DEFAULT_LOCALE = "fr"

_locale_ctx: ContextVar[str] = ContextVar("locale", default=DEFAULT_LOCALE)


def set_locale(locale: str) -> None:
    """Associe ``locale`` au contexte async courant si elle est supportee,
    sinon retombe sur DEFAULT_LOCALE."""
    _locale_ctx.set(locale if locale in SUPPORTED_LOCALES else DEFAULT_LOCALE)


def get_locale() -> str:
    """Retourne la locale courante (ou DEFAULT_LOCALE hors contexte de requete)."""
    return _locale_ctx.get()


def resolve_locale_from_accept_language(accept_language: str) -> str:
    """Extrait la premiere langue supportee d'un header Accept-Language brut.

    Ex: "en-US,en;q=0.9,fr;q=0.8" -> "en". Retombe sur DEFAULT_LOCALE si
    aucune langue supportee n'est trouvee.
    """
    for part in accept_language.split(","):
        lang = part.split(";")[0].strip().split("-")[0].lower()
        if lang in SUPPORTED_LOCALES:
            return lang
    return DEFAULT_LOCALE
