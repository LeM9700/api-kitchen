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

    # Resend — provider email transactionnel.
    # Vide = envoi désactivé (graceful degradation).
    resend_api_key: str = ""
    resend_from_email: str = "Super Admin <noreply@pizza-platform.com>"
    # URL publique du super-admin (utilisée dans les liens d'email).
    super_admin_base_url: str = "http://localhost:3001"

    # Hub POS OAuth 2.0 — HubRise (https://www.hubrise.com/developers). Chaine
    # vide sur client_id = feature desactivee (comme smtp_host,
    # stripe_webhook_connect_secret) ; URLs/champs ci-dessous ont leurs
    # valeurs HubRise par defaut mais restent overridables (multi-fournisseur).
    pos_hub_provider_name: str = "hubrise"
    pos_hub_client_id: str = ""
    pos_hub_client_secret: str = ""
    pos_hub_authorize_url: str = "https://manager.hubrise.com/oauth2/v1/authorize"
    pos_hub_token_url: str = "https://manager.hubrise.com/oauth2/v1/token"
    pos_hub_revoke_url: str = ""
    pos_hub_api_base_url: str = "https://api.hubrise.com/v1"
    pos_hub_redirect_uri: str = ""
    pos_hub_default_scopes: str = ""
    # Nom de la cle dans la reponse JSON du token endpoint qui porte
    # l'identifiant d'etablissement cote POS (HubRise : "location_id").
    pos_hub_establishment_id_field: str = "location_id"
    # Cle Fernet pour chiffrer les tokens OAuth au repos (app.core.services.crypto).
    pos_token_encryption_key: str = ""
    # Page frontend vers laquelle /pos/connect/callback redirige toujours
    # (jamais une URL fournie par le client -- evite l'open-redirect).
    pos_oauth_frontend_return_url: str = ""

    # Hub POS — recuperation catalogue (synchro en cache local). Chaine vide =
    # feature desactivee, meme pattern que pos_hub_client_id ci-dessus.
    # HubRise : GET https://api.hubrise.com/v1/catalogs/:id -- voir
    # app/modules/catalog/hub_client.py.
    pos_hub_catalog_url: str = ""
    pos_hub_catalog_rate_limit_per_minute: int = 10
    pos_hub_snapshot_staleness_minutes: int = 60
    # Au-dela de ce seuil (mesure depuis catalog_snapshots.synced_at), un
    # snapshot n'est plus servi du tout (meme comportement 409 qu'un snapshot
    # absent) plutot que d'etre servi indefiniment perime. Distinct de
    # pos_hub_snapshot_staleness_minutes (qui sert quand meme + resynchronise).
    pos_hub_snapshot_hard_expiry_hours: int = 24


settings = Settings()
