"""Wrapper gettext au-dessus de Babel, lisant la locale courante depuis le
ContextVar (app.core.i18n.locale) plutot que de la recevoir en parametre —
permet d'appeler t() depuis n'importe quelle profondeur d'appel
(notifications, emails) sans threader Request/locale partout.

Msgid en anglais (convention gettext standard) — le francais est traduit au
meme titre que l'anglais, via app/i18n/locales/fr/LC_MESSAGES/messages.po.
"""
from pathlib import Path

from babel.support import Translations

from app.core.i18n.locale import SUPPORTED_LOCALES, get_locale

_LOCALES_DIR = Path(__file__).resolve().parent.parent.parent / "i18n" / "locales"
_translations_cache: dict[str, Translations] = {}


def _load(locale: str) -> Translations:
    if locale not in _translations_cache:
        _translations_cache[locale] = Translations.load(str(_LOCALES_DIR), [locale])
    return _translations_cache[locale]


def t(message: str, *, locale: str | None = None, **kwargs) -> str:
    """Traduit ``message`` (msgid anglais) vers la locale courante, ou vers
    ``locale`` si fourni explicitement (ex: emails transactionnels qui
    doivent utiliser TenantConfig.default_language du destinataire plutot
    que la locale ContextVar de l'appelant).

    ``kwargs`` sont appliques via ``str.format()`` apres traduction, pour les
    interpolations (ex: ``t("Order #{order_id} ready", order_id=42)``).
    """
    active_locale = locale if locale in SUPPORTED_LOCALES else get_locale()
    translated = _load(active_locale).gettext(message)
    return translated.format(**kwargs) if kwargs else translated
