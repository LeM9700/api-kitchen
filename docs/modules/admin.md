# Admin

Configuration tenant (horaires, fermetures, temps de préparation, audit), tableaux de bord analytiques (MongoDB) et gestion super-admin des tenants.

## Endpoints

### Configuration tenant
| Méthode | Path | Auth | Rôles |
|---------|------|------|-------|
| GET | `/api/v1/admin/tenant/status` | Public | — statut ouvert/fermé + temps de préparation |
| GET | `/api/v1/admin/tenant/hours` | Public | — horaires de la semaine |
| GET | `/api/v1/admin/tenant/next-opening` | Public | — prochain horaire d'ouverture |
| GET | `/api/v1/admin/tenant/config` | Bearer JWT | admin |
| PATCH | `/api/v1/admin/tenant/config` | Bearer JWT | admin — rate-limit 30/min |
| PATCH | `/api/v1/admin/tenant/toggle-closure` | Bearer JWT | admin — rate-limit 5/min |
| PUT | `/api/v1/admin/tenant/scheduled-closure` | Bearer JWT | admin — planifier/annuler une fermeture |
| GET | `/api/v1/admin/tenant/audit` | Bearer JWT | admin — `PaginatedResponse` |
| PUT | `/api/v1/admin/tenant/hours/{day}` | Bearer JWT | admin |
| DELETE | `/api/v1/admin/tenant/hours/{day}` | Bearer JWT | admin |
| GET | `/api/v1/admin/tenant/closures` | Bearer JWT | admin |
| POST | `/api/v1/admin/tenant/closures` | Bearer JWT | admin |
| DELETE | `/api/v1/admin/tenant/closures/{id}` | Bearer JWT | admin |

### Statistiques
| Méthode | Path | Auth | Rôles |
|---------|------|------|-------|
| GET | `/api/v1/admin/stats/daily` | Bearer JWT | admin — 30 derniers jours |
| GET | `/api/v1/admin/stats/monthly` | Bearer JWT | admin — 12 derniers mois |
| GET | `/api/v1/admin/stats/live` | Bearer JWT | staff, admin — temps réel |
| GET | `/api/v1/admin/stats/summary` | Bearer JWT | admin — daily + live en une requête |
| GET | `/api/v1/admin/stats/stock` | Bearer JWT | admin — snapshot stock |

### Super-admin
| Méthode | Path | Auth | Rôles |
|---------|------|------|-------|
| GET | `/api/v1/admin/tenants` | Bearer JWT | super-admin |
| POST | `/api/v1/admin/tenants` | Bearer JWT | super-admin |
| PATCH | `/api/v1/admin/tenants/{id}/suspend` | Bearer JWT | super-admin |
| PATCH | `/api/v1/admin/tenants/{id}/unsuspend` | Bearer JWT | super-admin |

## Modèles de données

**`tenant_config`** (schema tenant) : `id`, `is_temporarily_closed`, `temporary_closure_message`, `default_closure_message`, `prep_time_normal_minutes`, `prep_time_peak_minutes`, `peak_orders_threshold`, `auto_calc_prep_time`, `overhead_per_order_minutes`, `timezone` (IANA, défaut `'Europe/Paris'`), `updated_at`, `scheduled_close_at`.

**`business_hours`** : `id`, `day_of_week` (0=Lundi, 6=Dimanche), `slot_index` (plusieurs créneaux par jour), `opens_at`, `closes_at`, `is_active`. Contrainte : `closes_at > opens_at`.

**`exceptional_closures`** : `id`, `closure_date` (unique), `custom_message`, `use_default_message`, `created_at`.

**`tenant_config_audit`** (migration `0012`) : `id`, `changed_by_user_id`, `changed_at`, `field_name`, `old_value`, `new_value`, `ip_address`, `user_agent`, `user_email`. Table immuable.

**`public.tenants`** : `id`, `tenant_name`, `slug`, `schema_name`, `created_at`, `is_suspended`, `suspended_at`, `suspension_message`. Le tenant suspendu bloque tous les logins (hors super-admin) avec 403.

## Comportements métier

**Statut tenant (`GET /tenant/status`)** :
1. Si `is_temporarily_closed` → fermé avec message.
2. Si la date du jour est dans `exceptional_closures` → fermé avec message.
3. Sinon, évalue `business_hours` pour le jour courant (timezone du tenant) → ouvert/fermé + prochain horaire d'ouverture.
4. `prep_time` : si `auto_calc_prep_time`, calculé dynamiquement depuis le nombre de commandes actives vs `peak_orders_threshold`.

**Horaires** : plusieurs `slot_index` par jour pour les services midi/soir (ex. 11h-14h + 18h-22h).

**Timezone par tenant** : `timezone` dans `tenant_config` (défaut `Europe/Paris`). Toutes les évaluations horaires utilisent cette valeur.

**Audit config** : chaque `PATCH /tenant/config` crée une entrée dans `tenant_config_audit` avec l'ancienne et la nouvelle valeur, l'IP, le `User-Agent` et l'email du requêteur.

**File d'attente commandes** : à la confirmation d'une commande, si `active_orders >= peak_orders_threshold`, le statut passe à `queued` au lieu de `confirmed`. Transitions valides depuis `queued` : `confirmed`, `cancelled`.

**Cooldown fermeture** : `PATCH /tenant/toggle-closure` vérifie que le dernier changement de `is_temporarily_closed` date de plus de 2 minutes (429 sinon). Rate-limit supplémentaire : 5/min.

**Fermeture programmée** : `PUT /tenant/scheduled-closure` pose ou annule `scheduled_close_at`. Le cron ARQ `process_scheduled_closures` vérifie les tenants actifs toutes les 5 minutes, ferme automatiquement si l'heure est atteinte, vide `scheduled_close_at`, écrit l'audit système et enqueue `notify_config_change`.

**Notifications config** : tout changement de `is_temporarily_closed` enqueue `notify_config_change` — email aux admins + push aux tokens staff/admin actifs.

**Suspension tenant** : `PATCH /tenants/{id}/suspend` met `is_suspended=true` + force `is_temporarily_closed=true` + bloque tous les logins (hors super-admin) avec 403.

**Stats daily** : lit `daily_stats_{tenant_slug}` MongoDB, 30 derniers documents triés par `date` DESC. Produit par `aggregate_daily_stats`.

**Stats monthly** : lit `monthly_stats_{tenant_slug}`, 12 derniers documents triés par `month` DESC. Produit par le cron `aggregate_monthly_stats` (00:00 UTC).

**Stats live** : lit `live_dashboard_{tenant_slug}`. Produit par le cron `aggregate_live_stats` (toutes les 5 minutes). Accessible staff + admin.

**Stats stock** : lit `stock_snapshots_{tenant_slug}`. Alimentée par cron `aggregate_stock_snapshot` (toutes les heures), qui écrit les ingrédients sous seuil depuis PostgreSQL.

**Gestion tenants (super-admin)** : `GET /tenants` liste tous les tenants depuis `public.tenants`. `POST /tenants` insère un tenant et crée le schema — ne provisionne pas les tables applicatives (flow recommandé : `POST /auth/register`).

## Sécurité implémentée

- [🔒] Routes config en `admin` only — staff ne peut pas modifier les horaires.
- [🔒] Audit trail immuable avec IP + User-Agent + email sur chaque modification de config.
- [🔒] `GET /tenant/status` et `GET /tenant/hours` publics — pas de données sensibles exposées.
- [🔒] `PATCH /tenant/toggle-closure` : rate-limit 5/min + cooldown 2 minutes — évite les attaques DoS par alternance fermeture/ouverture.
- [🔒] Suspension tenant : check dans `get_current_user` — tenants suspendus bloqués à la couche auth, pas au niveau des routes.
- [⚠️ PROD] `POST /admin/tenants` crée le schema PostgreSQL mais pas les tables — les tenants créés via cet endpoint ne sont pas opérationnels sans appel complémentaire à `_create_tenant_tables`.
- [⚠️ PROD] Schémas Pydantic stricts ajoutés pour daily/monthly/live/stock — risque de leak réduit mais vérifier la structure MongoDB côté worker.

---

## Axes d'amélioration

### Logique métier
- [✅ IMPLÉMENTÉ] **Stock snapshots** : cron `aggregate_stock_snapshot` (toutes les heures) — voir worker.
- [✅ IMPLÉMENTÉ] **Timezone configurable** : champ `timezone` dans `tenant_config` (défaut `Europe/Paris`).
- [✅ IMPLÉMENTÉ] **Fermeture programmée** : champ `scheduled_close_at`, endpoint de planification et cron ARQ.
- [✅ IMPLÉMENTÉ] **File d'attente commandes** : statut `queued` quand `active_orders >= peak_orders_threshold`.
- [✅ IMPLÉMENTÉ] **Notifications de configuration** : email + push sur changement de `is_temporarily_closed`.
- [✅ IMPLÉMENTÉ] **Gestion multi-admins** : `user_email` ajouté dans l'audit.

### Sécurité & contre-intrusion
- **Audit log tampering** : la table `tenant_config_audit` est "immuable par convention" mais un admin PostgreSQL peut modifier les enregistrements. Envisager un hash chaîné ou un export externe.
- [✅ IMPLÉMENTÉ] **MFA super-admin** : TOTP + QR code + codes de secours pour `role=super-admin`.
- [✅ IMPLÉMENTÉ] **DoS par fermeture/réouverture** : cooldown 2 min + rate-limit 5/min sur toggle-closure.
- **Injection dans le message de fermeture** : `temporary_closure_message` affiché côté client — s'assurer que le frontend l'encode correctement (XSS potentiel si rendu en HTML non sanitisé).
- [✅ IMPLÉMENTÉ] **Stats MongoDB leak** : schémas Pydantic stricts ajoutés sur daily/monthly/live/stock.

### Implémentation
- [✅ IMPLÉMENTÉ] **Suspension de tenant** : `PATCH /tenants/{id}/suspend` + blocage login à la couche auth.

---

## Ce qui manque pour les interfaces

> **Endpoints disponibles** pour intégration immédiate :
> - Widget horaires + statut : `GET /tenant/hours`, `GET /tenant/status`, `GET /tenant/next-opening`
> - Dashboard live initial : `GET /stats/summary` (live + dernier jour en une requête)
> - Fermeture urgente : `PATCH /tenant/toggle-closure`
> - Suspension tenant : `PATCH /tenants/{id}/suspend` / `PATCH /tenants/{id}/unsuspend`
> - Stock en alerte : `GET /stats/stock` (alimenté par cron horaire)

### Interface client
- **Widget horaires** : `GET /tenant/hours` et `GET /tenant/status` sont publics — afficher dans le header/footer : "Ouvert jusqu'à 22h" ou "Fermé — rouvre lundi à 11h".
- **Bannière de fermeture** : afficher `temporary_closure_message` si `is_temporarily_closed`.

### Interface staff
- **Dashboard live** : `GET /stats/summary` + `GET /stats/live` → nombre de commandes en cours, revenue du jour, commandes en attente. Rafraîchissement automatique (polling toutes les 5min ou WebSocket).
- **Indicateur statut tenant** : visible en permanence — "Le restaurant est OUVERT / FERMÉ".

### Interface admin (tenant)
- **Gestion des horaires** : interface de planning hebdomadaire (chaque jour avec ses créneaux), drag pour modifier.
- **Fermeture exceptionnelle** : calendrier pour ajouter des jours fériés / fermetures ponctuelles.
- **Fermeture urgente** : bouton "Fermer maintenant" avec message personnalisé (`PATCH /tenant/toggle-closure`).
- **Config prep time** : slider entre manuel et automatique, réglage des seuils peak/normal.
- **Dashboard analytique** : graphes daily/monthly (chiffre d'affaires, nombre de commandes, panier moyen), lien avec `GET /stats/daily` + `GET /stats/monthly` ou `GET /stats/summary`.
- **Audit trail** : tableau des modifications de config avec filtres par date et par champ.
- **Stock snapshot** : état des ingrédients en alerte (`GET /stats/stock`, alimenté par cron horaire).

### Super-admin
- **Liste des tenants** : table avec statut (actif/suspendu), date de création, nombre d'utilisateurs, dernière activité.
- [✅ IMPLÉMENTÉ] **Suspension de tenant** : `PATCH /admin/tenants/{id}/suspend` + `PATCH /admin/tenants/{id}/unsuspend` — bloquer les logins.
- **Métriques agrégées cross-tenant** : revenue total, nombre de commandes, tenants actifs sur la plateforme.
- **Console de diagnostic** : accès en lecture aux logs d'un tenant (failed_jobs MongoDB).
