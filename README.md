# api-pizza

API backend FastAPI (Python 3.12+) pour une plateforme SaaS multi-tenant de gestion de
pizzerias/restaurants : catalogue produits, commandes, paiements Stripe Connect, stock
d'ingrédients, livraison, fidélité, promotions et notifications push/temps réel.

Isolation multi-tenant par **schéma PostgreSQL dédié par tenant** (`tenant_{slug}`).

## Stack

- **API** : FastAPI, SQLAlchemy 2.0 (async) + asyncpg, `slowapi` (rate limiting)
- **Bases de données** : PostgreSQL (métier, par schéma tenant), MongoDB (stats précalculées,
  événements de connexion), Redis (cache/rate limit/révocation token/pub-sub WebSocket)
- **Jobs asynchrones** : `arq` (worker séparé — emails, alertes stock, agrégations, cron)
- **Auth** : JWT (PyJWT, HS256), refresh tokens en DB, MFA TOTP (admin/super-admin)
- **Paiements** : Stripe + Stripe Connect (reversements multi-tenant)
- **Médias** : Cloudinary
- **Observabilité** : Sentry (optionnel, activé via `SENTRY_DSN`)
- **Déploiement** : Railway (`railpack.json`)

## Prérequis

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) pour la gestion des dépendances
- PostgreSQL 16, MongoDB 7, Redis 7 (localement, via Docker ou installation native)

## Installation

```bash
uv sync --frozen --extra dev
cp .env.example .env
# éditer .env avec des identifiants réels (voir docs/local-testing.md pour Postgres via Docker)
uv run alembic upgrade head
```

## Lancer l'API en local

```bash
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Swagger UI : http://127.0.0.1:8000/docs (désactivé si `ENVIRONMENT=production`).

## Lancer le worker (jobs asynchrones)

```bash
uv run python -m arq worker.main.WorkerSettings
```

Voir [docs/modules/worker.md](docs/modules/worker.md) pour le détail des tasks et cron jobs.

## Tests

```bash
uv run pytest -v --tb=short
```

Voir [docs/local-testing.md](docs/local-testing.md) pour la configuration complète (Postgres
de test, variables d'environnement). Certains tests d'intégration se `skip` automatiquement
si la base de test n'est pas accessible.

## Test de charge

Un script [k6](https://k6.io/) basique couvre les 3 endpoints les plus critiques (catalogue,
création commande, listing commandes) : [scripts/load-test.js](scripts/load-test.js).

```bash
k6 run scripts/load-test.js
# ou contre un environnement de staging :
k6 run -e BASE_URL=https://staging.example.com scripts/load-test.js
```

Le script crée son propre tenant/produit de test dans son étape `setup()` — exécutable sans jeu de
données préexistant. Objectif : établir une baseline de latence (p95 par endpoint), pas un test de
rupture — ajuster `vus`/`duration` dans `options.scenarios` pour aller plus loin.

## Migrations

```bash
uv run alembic upgrade head          # appliquer
uv run alembic downgrade -1          # revenir en arrière d'une révision
uv run alembic check                 # vérifier que les modèles ORM correspondent aux migrations
```

Les migrations appliquent le DDL à **tous les tenants existants** (boucle sur `public.tenants`).
Un nouveau tenant est provisionné directement via `_TENANT_DDL_STATEMENTS`
(`app/modules/auth/service.py`) — toute modification de schéma doit être répercutée aux deux
endroits (migration Alembic + cette liste), voir le commentaire en tête de fichier.

## Endpoints d'exploitation

- `GET /health` — liveness probe (process vivant, pas de dépendance DB)
- `GET /health/ready` — readiness probe (vérifie Postgres `SELECT 1` + Redis `PING`, 503 si l'un échoue)

## Déploiement (Railway)

Le déploiement passe par `railpack.json` : la `startCommand` exécute les migrations
(`alembic upgrade head`) puis démarre `uvicorn`. Le **worker ARQ est un second process** et doit
être déclaré comme un service Railway distinct dans le même projet, avec sa propre start command :

```
python -m arq worker.main.WorkerSettings
```

Variables d'environnement requises : voir `.env.example`. À définir sur Railway (jamais commitées) :
`DATABASE_URL`, `MONGO_URL`, `ARQ_REDIS_URL`, `REDIS_URL`, `JWT_SECRET`, `STRIPE_SECRET_KEY`,
`STRIPE_WEBHOOK_SECRET`, `CLOUDINARY_*`, `CORS_ORIGINS`, `APP_BASE_URL`, `ENVIRONMENT=production`,
et optionnellement `SENTRY_DSN`, `JWT_HMAC_SECRET`, `STRIPE_WEBHOOK_CONNECT_SECRET`, `SMTP_*`,
`APNS_*`, `FCM_*`.

Procédures de rollback, replay webhook Stripe et restart worker : voir [RUNBOOK.md](RUNBOOK.md).
Politique de confidentialité (base technique, à valider juridiquement) : voir [PRIVACY.md](PRIVACY.md).

## Documentation par module

Voir [docs/modules/](docs/modules/) — un fichier par module métier (auth, catalog, orders,
payments, stock, loyalty, promotions, delivery, admin, worker).

## Structure

```
app/
  core/           # config, DB, auth, tenancy, HTTP (deps/erreurs/middlewares)
  modules/        # un dossier par domaine métier (auth, catalog, orders, payments, ...)
alembic/versions/ # migrations, une par changement de schéma
worker/           # définitions et tasks du worker arq
tests/            # tests pytest (asyncio_mode=auto)
docs/modules/      # documentation détaillée par module
```
