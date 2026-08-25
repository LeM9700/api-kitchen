"""Tests de normalisation du schema DATABASE_URL.

[PROD] Railway (et d'autres PaaS) exposent la variable Postgres avec le schema
sync ``postgresql://`` (voire ``postgres://``), jamais ``postgresql+asyncpg://``.
Si cette URL est transmise telle quelle a ``create_async_engine``, SQLAlchemy
resout le driver sync par defaut (psycopg2, absent des dependances) et le
service crash au demarrage avec ``ModuleNotFoundError: No module named
'psycopg2'``. Le validator doit forcer le driver asyncpg quel que soit le
schema fourni par l'infra.
"""

from app.core.config.settings import Settings

_REQUIRED_KWARGS = {
    "mongo_url": "mongodb://localhost:27017",
    "arq_redis_url": "redis://localhost:6379",
    "redis_url": "redis://localhost:6379",
    "cloudinary_cloud_name": "test",
    "cloudinary_api_key": "test",
    "cloudinary_api_secret": "test",
    "stripe_secret_key": "sk_test",
    "stripe_webhook_secret": "whsec_test",
    "jwt_secret": "x" * 32,
}


def _settings(**overrides) -> Settings:
    values = {**_REQUIRED_KWARGS, "test_database_url": "", **overrides}
    return Settings(_env_file=None, **values)


def test_plain_postgresql_scheme_gets_asyncpg_driver():
    s = _settings(database_url="postgresql://user:pass@host:5432/db")
    assert s.database_url == "postgresql+asyncpg://user:pass@host:5432/db"


def test_legacy_postgres_scheme_gets_asyncpg_driver():
    s = _settings(database_url="postgres://user:pass@host:5432/db")
    assert s.database_url == "postgresql+asyncpg://user:pass@host:5432/db"


def test_explicit_asyncpg_scheme_is_left_untouched():
    s = _settings(database_url="postgresql+asyncpg://user:pass@host:5432/db")
    assert s.database_url == "postgresql+asyncpg://user:pass@host:5432/db"


def test_test_database_url_is_also_normalized():
    s = _settings(
        database_url="postgresql+asyncpg://user:pass@host:5432/db",
        test_database_url="postgres://user:pass@host:5432/db_test",
    )
    assert s.test_database_url == "postgresql+asyncpg://user:pass@host:5432/db_test"


def test_empty_test_database_url_is_left_untouched():
    s = _settings(database_url="postgresql+asyncpg://user:pass@host:5432/db")
    assert s.test_database_url == ""


def test_pos_oauth_settings_default_to_hubrise():
    s = _settings(database_url="postgresql+asyncpg://user:pass@host:5432/db")
    assert s.pos_hub_provider_name == "hubrise"
    assert s.pos_hub_client_id == ""
    assert s.pos_hub_client_secret == ""
    assert s.pos_hub_authorize_url == "https://manager.hubrise.com/oauth2/v1/authorize"
    assert s.pos_hub_token_url == "https://manager.hubrise.com/oauth2/v1/token"
    assert s.pos_hub_revoke_url == ""
    assert s.pos_hub_redirect_uri == ""
    assert s.pos_hub_default_scopes == ""
    assert s.pos_hub_establishment_id_field == "location_id"
    assert s.pos_token_encryption_key == ""
    assert s.pos_oauth_frontend_return_url == ""


def test_pos_oauth_settings_can_be_overridden():
    s = _settings(database_url="postgresql+asyncpg://user:pass@host:5432/db", pos_hub_client_id="abc", pos_hub_authorize_url="https://hub.example/authorize")
    assert s.pos_hub_client_id == "abc"
    assert s.pos_hub_authorize_url == "https://hub.example/authorize"


def test_hub_catalog_sync_settings_have_safe_defaults():
    from app.core.config import settings

    assert settings.pos_hub_catalog_url == ""
    assert settings.pos_hub_catalog_rate_limit_per_minute == 10
    assert settings.pos_hub_snapshot_staleness_minutes == 60


def test_hub_catalog_hard_expiry_setting_defaults_to_24_hours():
    from app.core.config import settings

    assert settings.pos_hub_snapshot_hard_expiry_hours == 24
