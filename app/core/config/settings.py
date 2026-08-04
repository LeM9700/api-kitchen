import pathlib

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve .env relative to this file so the path works regardless of cwd.
_ENV_FILE = pathlib.Path(__file__).parent.parent.parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), extra="ignore")

    database_url: str
    test_database_url: str = ""

    @field_validator("database_url", "test_database_url")
    @classmethod
    def _force_asyncpg_driver(cls, v: str) -> str:
        # Railway/Heroku-style Postgres plugins expose DATABASE_URL with the
        # sync scheme ("postgres://" or "postgresql://"). Passed as-is to
        # create_async_engine, SQLAlchemy silently resolves the default sync
        # driver (psycopg2, not a project dependency) and crashes at startup
        # with ModuleNotFoundError instead of a clear config error.
        if v.startswith("postgres://"):
            v = "postgresql://" + v[len("postgres://") :]
        if v.startswith("postgresql://"):
            v = "postgresql+asyncpg://" + v[len("postgresql://") :]
        return v
    mongo_url: str
    mongo_db: str = "pizzeria_stats"
    arq_redis_url: str
    stripe_secret_key: str
    stripe_webhook_secret: str
    # Secret de signature dedie aux webhooks Stripe Connect (comptes connectes, direct charges).
    # Vide = Connect non configure — seul stripe_webhook_secret (plateforme) est tente.
    stripe_webhook_connect_secret: str = ""
    jwt_secret: str
    # Cle dediee pour le HMAC de lookup des refresh tokens (compute_token_lookup).
    # Vide = fallback sur jwt_secret (comportement historique) — définir une valeur
    # dédiée sépare la responsabilité de signature JWT de celle du lookup en DB.
    jwt_hmac_secret: str = ""
    environment: str = "local"

    # Sentry — APM + tracking d'erreurs. Vide = désactivé (ex: en local/CI).
    sentry_dsn: str = ""

    jwt_access_expire_minutes: int = 15
    jwt_refresh_expire_days: int = 30

    # SMTP — laisser smtp_host vide pour désactiver l'envoi (graceful degradation).
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "noreply@example.com"

    # URL publique de l'application (utilisée dans les liens d'email).
    app_base_url: str = "http://localhost:8000"

    # CORS — origines autorisées (JSON array dans le .env).
    # Ex : CORS_ORIGINS=["https://app.monsite.com","https://admin.monsite.com"]
    cors_origins: list[str] = ["http://localhost:3000"]

    # Cloudinary — stockage médias multi-tenant.
    cloudinary_cloud_name: str
    cloudinary_api_key: str
    cloudinary_api_secret: str

    # Redis URL pour le pub/sub inter-instances (connexion dédiée, distincte du pool arq).
    # Railway expose automatiquement REDIS_URL lorsqu'un service Redis est attaché.
    redis_url: str

    # APNs (iOS push notifications).
    # Option A (prod Railway) : chemin vers le Secret File monté (APNS_PRIVATE_KEY_PATH).
    # Option B (dev local)    : contenu PEM complet en env var (APNS_PRIVATE_KEY).
    # Voir .env.example pour les deux variantes.
    apns_key_id: str | None = None
    apns_team_id: str | None = None
    apns_bundle_id: str | None = None
    apns_private_key: str | None = None
    apns_private_key_path: str | None = None

    # FCM v1 (Android push notifications).
    # fcm_service_account_json : JSON string complet du service account Google
    # téléchargé depuis Firebase Console → Project Settings → Service Accounts.
    fcm_project_id: str | None = None
    fcm_service_account_json: str | None = None


settings = Settings()
