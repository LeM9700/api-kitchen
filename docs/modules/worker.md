# Worker

Worker ARQ gérant les tâches asynchrones hors du chemin HTTP : emails, alertes stock, agrégations analytiques, expiration des points de fidélité, dead-letter handling.

## Configuration (`WorkerSettings`)

- `max_tries = 3`, `job_timeout = 120s` — retry automatique ARQ sur échec.
- Pool ARQ initialisé comme singleton dans le lifespan FastAPI (`app.state.arq_pool`) — partagé entre toutes les routes via `Depends(get_arq_pool)`.
- Connexions MongoDB et PostgreSQL créées à chaque exécution de task (fermées dans `finally`) — pas de pool partagé dans le worker (contexte process séparé de FastAPI).

## Tasks enregistrées

| Task | Déclencheur | Description |
|------|-------------|-------------|
| `send_stock_alert` | Enqueue post-commit `update_status` | Cooldown 4h via `last_alert_sent_at` — enqueue `send_stock_alert_email` si éligible |
| `send_stock_alert_email` | Enqueue par `send_stock_alert` | Email SMTP à l'admin du tenant, fallback log si SMTP non configuré |
| `send_email` | Enqueue post-commit `update_status` (annulation) | Email générique SMTP avec fallback log |
| `send_verification_email` | Enqueue à l'inscription | Récupère l'email depuis la DB tenant, envoie le lien de vérification UUID4 |
| `aggregate_daily_stats` | Enqueue manuel (par date) | Agrège commandes `delivered` J-1 de PostgreSQL vers `daily_stats_{slug}` MongoDB |
| `dead_letter_handler` | ARQ `on_job_startup` / monitoring | Enregistre explicitement un job en dead-letter MongoDB |

## Cron jobs

| Cron | Planification | Description |
|------|---------------|-------------|
| `aggregate_monthly_stats` | 00:00 UTC quotidien | Agrège `daily_stats_{slug}` par année+mois (pipeline MongoDB) vers `monthly_stats_{slug}`. Moyenne pondérée `avg_order_value`. Tous les tenants. |
| `aggregate_live_stats` | Toutes les 5 minutes | Commandes/revenue des 24h + commandes `pending` depuis PostgreSQL, écrit dans `live_dashboard_{slug}`. Tous les tenants. |
| `expire_loyalty_points` | 03:00 UTC quotidien | Expire les points de fidélité obsolètes par tenant. Rate-limité, timeout 600s. |

## Dead-letter handling

Décorateur `@with_dead_letter` wrappant toutes les tasks email :

- Sur `job_try < max_tries` : exception re-levée normalement (ARQ retry).
- Sur `job_try >= max_tries` (dernière tentative) : insertion dans `failed_jobs_{tenant_slug}` MongoDB avec `job_id`, `function`, `args`, `kwargs`, `error`, `error_type`, `failed_at`. Exception re-levée pour qu'ARQ marque le job `failed`.
- Le handler ne lève jamais sur échec MongoDB (dégradation gracieuse avec log).

## Collections MongoDB

| Collection | Écrite par | Lue par |
|------------|-----------|---------|
| `daily_stats_{slug}` | `aggregate_daily_stats` | `aggregate_monthly_stats`, `GET /admin/stats/daily` |
| `monthly_stats_{slug}` | `aggregate_monthly_stats` | `GET /admin/stats/monthly` |
| `live_dashboard_{slug}` | `aggregate_live_stats` | `GET /admin/stats/live` |
| `stock_snapshots_{slug}` | (non implémenté) | `GET /admin/stats/stock` |
| `failed_jobs_{slug}` | `@with_dead_letter` / `dead_letter_handler` | Monitoring externe |

## Sécurité implémentée

- [🔒] `@with_dead_letter` : échecs persistés avant re-raise — pas de perte silencieuse de job.
- [⚠️ PROD] `aggregate_daily_stats` et `aggregate_live_stats` créent des connexions à chaque exécution — fermées dans `finally` mais risque de leak si exception avant `finally`.
- [⚠️ PROD] `_MAX_TRIES = 3` dans `worker_utils.py` doit rester synchronisé avec `WorkerSettings.max_tries` — pas de référence directe.
- [⚠️ PROD] `stock_snapshots_{slug}` non alimenté — `GET /admin/stats/stock` retourne toujours vide.

---

## Axes d'amélioration

### Logique métier
- **stock_snapshots** : cron manquant pour alimenter `stock_snapshots_{slug}` — à ajouter (ex. toutes les heures : snapshot des ingrédients sous seuil).
- **Notifications email order** : `send_email` est enqueue sur annulation mais pas sur confirmation, préparation ou livraison — le client ne reçoit aucun email de suivi.
- **Email de confirmation de commande** : non implémenté — le client ne sait pas que sa commande est acceptée par email.
- **Nettoyage des PaymentIntents expirés** : pas de cron pour annuler les commandes `pending` dont le `PaymentIntent` Stripe a expiré (24h).
- **Retry intelligent** : le backoff ARQ est linéaire — envisager un backoff exponentiel pour les emails (évite le flood SMTP en cas de panne).
- **Monitoring des cron** : pas de mécanisme pour détecter qu'un cron ne s'exécute pas (dead cron). Intégrer un heartbeat (ex. Healthchecks.io).

### Sécurité & contre-intrusion
- **Job poisoning** : si un attaquant peut injecter des `kwargs` dans une task ARQ (via une faille applicative), les tasks email peuvent envoyer des emails arbitraires. Valider les paramètres de chaque task (recipient, contenu) contre une whitelist.
- **Redis compromise** : ARQ utilise Redis comme queue — si Redis est compromis, un attaquant peut injecter des jobs arbitraires. Sécuriser Redis avec authentification (`requirepass`) et TLS.
- **Leak de données dans dead-letter** : `failed_jobs_{slug}` stocke `args` et `kwargs` — si une task email contient un mot de passe ou un token dans ses arguments, il sera persisté en clair dans MongoDB. S'assurer que les tasks ne passent jamais de secrets en argument (utiliser des IDs, pas des valeurs).
- **DoS par saturation de queue** : un attaquant qui crée massivement des comptes peut saturer la queue ARQ avec des `send_verification_email`. Rate-limiter `/register` (déjà 5/min) + ajouter un circuit breaker sur l'enqueue.
- **Cross-tenant task** : les tasks reçoivent `tenant_slug` en argument — vérifier en début de chaque task que le slug est valide (existe dans `public.tenants`) pour éviter de travailler sur un schema inexistant.

### Accessibilité / Observabilité
- Endpoint `GET /admin/worker/failed-jobs` pour exposer `failed_jobs_{slug}` à l'admin via l'API (plutôt que MongoDB direct).
- Endpoint `GET /admin/worker/queue-depth` pour monitorer la profondeur de la queue ARQ (nombre de jobs en attente).
- Ajouter des métriques Prometheus sur les tasks (durée, succès, échecs, retries).

---

## Ce qui manque pour les interfaces

### Interface admin (tenant)
- **Console dead-letter** : tableau des jobs échoués (`failed_jobs_{slug}`) avec la raison d'échec, la date, et un bouton "Rejouer".
- **Statut worker** : indicateur "Worker ARQ actif / inactif" basé sur un heartbeat Redis.
- **Alertes stock reçues** : historique des emails d'alerte stock envoyés (date, ingrédient, destinataire).

### Super-admin
- **Vue globale des jobs** : nombre de jobs en queue, taux d'échec cross-tenant, alertes si la queue dépasse un seuil.
- **Santé des crons** : tableau de bord montrant la dernière exécution réussie de chaque cron par tenant.
