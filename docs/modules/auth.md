# Auth

Gestion complète du cycle de vie des tenants et des sessions utilisateurs : inscription, connexion timing-safe, vérification email, rotation de tokens, déconnexion, récupération de mot de passe, gestion multi-appareils, administration des utilisateurs, super-admin impersonation et audit des connexions.

## Endpoints

| Méthode | Path | Auth | Rôles |
|---------|------|------|-------|
| **Auth publique** |
| POST | `/api/v1/auth/register` | Public | — rate-limit 5/min |
| POST | `/api/v1/auth/login` | Public | — rate-limit 10/min |
| POST | `/api/v1/auth/forgot-password` | Public | — rate-limit 3/min |
| POST | `/api/v1/auth/reset-password` | Public | — rate-limit 5/min |
| GET | `/api/v1/auth/verify-email?token=&tenant_slug=` | Public | — |
| POST | `/api/v1/auth/refresh` | Public | — rate-limit 20/min |
| **Auth utilisateur** |
| GET | `/api/v1/auth/me` | Bearer JWT | tous |
| POST | `/api/v1/auth/resend-verification` | Bearer JWT | tous — rate-limit 3/min |
| POST | `/api/v1/auth/change-password` | Bearer JWT | tous — rate-limit 5/min |
| POST | `/api/v1/auth/logout` | Bearer JWT | tous |
| GET | `/api/v1/auth/sessions` | Bearer JWT | tous |
| DELETE | `/api/v1/auth/sessions/{id}` | Bearer JWT | tous |
| DELETE | `/api/v1/auth/sessions` | Bearer JWT | tous |
| **Admin tenant — gestion utilisateurs** |
| GET | `/api/v1/admin/users` | Bearer JWT | admin — filtres : role, is_active, email_verified |
| POST | `/api/v1/admin/users` | Bearer JWT | admin — création staff/admin avec mot de passe temporaire |
| PATCH | `/api/v1/admin/users/{id}/deactivate` | Bearer JWT | admin |
| PATCH | `/api/v1/admin/users/{id}/reactivate` | Bearer JWT | admin |
| POST | `/api/v1/admin/users/{id}/reset-password` | Bearer JWT | admin — réinitialisation forcée |
| GET | `/api/v1/admin/security/login-events` | Bearer JWT | admin — rate-limit 30/min |
| **Super-admin — cross-tenant** |
| POST | `/api/v1/admin/impersonate/{tenant_slug}` | Bearer JWT | super-admin — token 5 min |
| GET | `/api/v1/admin/impersonation-log` | Bearer JWT | super-admin |
| GET | `/api/v1/admin/tenants/users` | Bearer JWT | super-admin — cross-tenant listing |
| PATCH | `/api/v1/admin/tenants/{id}/suspend` | Bearer JWT | super-admin |
| PATCH | `/api/v1/admin/tenants/{id}/unsuspend` | Bearer JWT | super-admin |

## Modèles de données

**`users`** (schema tenant) : `id`, `email` (unique, indexé), `password_hash`, `full_name`, `role` (customer/staff/admin/super-admin), `is_active`, `created_at`, `email_verification_token` (UUID4, unique, nullable), `email_verification_expires_at`, `email_verified_at`, **`password_reset_token` (bcrypt hash, nullable, indexed)**, **`password_reset_expires_at`**, **`must_change_password` (default False)**, `mfa_secret`, `mfa_enabled`, `mfa_backup_codes` (hashes uniquement).

**`refresh_tokens`** (schema tenant) : `id`, `user_id`, `token_hash` (bcrypt), `token_lookup` (HMAC-SHA256, unique, indexé — migration `0003`), `expires_at`, `revoked_at`, `created_at`, **`user_agent` (nullable)**, **`ip_address` (nullable)**.

**`TokenResponse`** : `access_token`, `refresh_token`, `token_type`, **`session_id`** (ID de la ligne refresh_tokens — utilisé pour `/auth/sessions`).

**`UserOut` (GET /me)** : `id`, `email`, `full_name`, `role`, `is_active`, **`email_verified`** (booléen calculé : `email_verified_at is not None`), **`must_change_password`**.

**MongoDB — `login_events_{tenant_slug}`** : Document avec `tenant_slug`, `user_id` (nullable), `email`, `ip_address`, `user_agent`, `success`, `failure_reason`, `created_at`. Index TTL 90 jours.

## Comportements métier

### Inscription & Email

**Inscription (`POST /register`)** : crée le tenant en `public.tenants`, provisionne le schema PostgreSQL via DDL explicite (`_TENANT_DDL_STATEMENTS` — source de vérité pour tous les tenants), crée l'utilisateur avec role `admin`, génère un token de vérification email (UUID4, TTL 24h), enqueue `send_verification_email` via ARQ. Le champ `must_change_password` est False à l'inscription (le premier mot de passe est validé).

**Politique de mot de passe** : validée par `field_validator` Pydantic — minimum 8 caractères, 1 majuscule, 1 chiffre, 1 caractère spécial parmi `!@#$%^&*`. Erreur 422 avec liste des règles manquantes.

**Vérification email (`GET /verify-email`)** : marque `email_verified_at`, nullifie `email_verification_token` et `email_verification_expires_at`. Erreur 400 si token introuvable ou expiré (TTL 24h).

**Renvoi de vérification email (`POST /resend-verification`)** : authentifié uniquement (évite inbox flooding). Vérifie que `email_verified_at is None` → sinon `400 ALREADY_VERIFIED`. Génère un nouveau token UUID4, remet `email_verification_expires_at = now() + 24h`, enqueue `send_verification_email`. Réponse `202 Accepted`.

### Mot de passe oublié & réinitialisation

**Mot de passe oublié (`POST /auth/forgot-password`)** — public, rate-limit 3/min
- Body : `{ email: str, tenant_slug: str }`
- Génère un token alphanumérique 8 chars (`secrets.token_urlsafe(6)`), stocké hashé (bcrypt) dans `password_reset_token`, TTL 30 min dans `password_reset_expires_at`
- Réponse toujours `202 Accepted` avec message générique — évite l'énumération d'emails (contrairement à `/register` où la transparence B2B est acceptable)
- Enqueue job ARQ `send_password_reset_email(tenant_slug, user_id, token_en_clair)`

**Réinitialisation mot de passe (`POST /auth/reset-password`)** — public, rate-limit 5/min
- Body : `{ email: str, tenant_slug: str, token: str, new_password: str }`
- Vérifie le token par bcrypt + TTL ; erreur `400 INVALID_TOKEN` si invalide ou expiré
- Valide la politique mot de passe (même validator que RegisterRequest)
- Hashe le nouveau mot de passe, nullifie `password_reset_token` + `password_reset_expires_at` (usage unique)
- Révoque tous les refresh tokens de l'utilisateur
- Met à jour `must_change_password = False` si actif
- Réponse `200` avec message de confirmation (pas de tokens — l'utilisateur doit se reconnecter)

### Changement de mot de passe

**Changement de mot de passe (`POST /auth/change-password`)** — authentifié, rate-limit 5/min
- Body : `{ current_password: str | None, new_password: str }`
- Si `must_change_password = True` : `current_password` non requis — l'utilisateur doit changer son mot de passe avant d'accéder à d'autres routes
- Si `must_change_password = False` : vérifie `current_password` par bcrypt obligatoirement
- Valide la politique mot de passe
- Met `must_change_password = False`
- Révoque tous les refresh tokens sauf le courant (l'utilisateur reste connecté)
- Invalide le JTI de l'ancien access token en Redis (force expiration immédiate)

**Middleware `must_change_password`** :
- `must_change_password` ajouté dans le payload JWT à l'émission
- `get_current_user()` : si flag actif → lève `403 PASSWORD_CHANGE_REQUIRED` sur toutes les routes sauf `POST /auth/change-password` et `POST /auth/logout`

### Connexion & Audit

**Connexion timing-safe (`POST /login`)** : si l'email est introuvable, un `bcrypt.verify` dummy (`DUMMY_HASH`) est quand même exécuté pour uniformiser le temps de réponse et éviter l'énumération de comptes par timing oracle.

**MFA super-admin** : si `role=super-admin` et `mfa_enabled=true`, `POST /login` exige `mfa_code` avant d'émettre access/refresh tokens. Le code peut être un TOTP valide (`valid_window=1`) ou un code de secours hashé, consommé une seule fois. Les secrets et codes MFA ne sont jamais exposés dans `/me` ni les JWT.

**Audit des connexions (MongoDB)** — non-bloquant via `asyncio.create_task()` :
- Loggué à chaque tentative de login (succès ET échec)
- Document : `tenant_slug`, `user_id` (nullable), `email`, `ip_address`, `user_agent`, `success`, `failure_reason`, `created_at`
- `user_id` nullable : null si email inconnu — l'email est quand même loggué pour détecter le credential stuffing
- Index TTL MongoDB sur `created_at` : rétention 90 jours (configurable via settings)

**`GET /admin/security/login-events`** — admin tenant, rate-limit 30/min
- Filtres : `success` (bool), `email`, `ip_address`, limite 200 résultats max
- Permet à l'admin tenant de détecter des patterns suspects

### Refresh token & Sessions

**Refresh token** : lookup en deux phases — (1) `SELECT WHERE token_lookup = HMAC(token)` O(1) pour les tokens post-migration `0003` ; (2) fallback O(n·bcrypt) pour les anciens tokens. Rotation : `revoked_at` posé avant l'émission du nouveau token.

**Provisioning tenant** : DDL explicite via `_TENANT_DDL_STATEMENTS` — inclut toutes les tables applicatives + migrations `0003` (token_lookup) et `0004` (email_verification + promo_code). Tout changement de schéma doit être répercuté ici ET dans une migration Alembic.

**Gestion multi-appareils (`POST /auth/login`, `POST /auth/refresh`)** : les tokens incluent `user_agent` et `ip_address` pour identifier l'appareil. `TokenResponse` retourne un `session_id` (ID de la ligne refresh_tokens) que le client stocke et transmet en query param.

**`GET /auth/sessions`** — authentifié, liste des refresh tokens actifs
- Retourne les tokens non révoqués et non expirés de l'utilisateur
- Champs : `id`, `created_at`, `expires_at`, `user_agent`, `ip_address`, `is_current` (true si `id == current_session_id` query param)

**`DELETE /auth/sessions/{session_id}`** — authentifié
- Révoque un refresh token précis appartenant à l'utilisateur courant (vérifie `user_id`)
- Si session courante révoquée → ajoute le JTI en deny-list Redis
- Réponse `204`

**`DELETE /auth/sessions`** (logout global) — authentifié
- Query param `?revoke_current=true` pour tout révoquer (déconnexion totale)
- Sans ce param : révoque tous les refresh tokens sauf le courant (l'utilisateur reste connecté)
- Ajoute les JTI des tokens révoqués en deny-list Redis
- Réponse `204`

### Admin tenant — gestion utilisateurs

Tous les endpoints sous `/api/v1/admin/users`, protégés par `require_role("admin")`.

**`GET /admin/users`** — pagination + filtres
- Filtres : `role` (customer/staff/admin), `is_active` (bool), `email_verified` (bool)
- Retourne : `id`, `email`, `full_name`, `role`, `is_active`, `email_verified`, `created_at`, `must_change_password`

**`POST /admin/users`** — création avec mot de passe temporaire
- Body : `{ email: str, full_name: str | None, role: Literal["staff", "admin"] }`
- Génère mot de passe temporaire 16 chars (`secrets.token_urlsafe(12)`)
- Crée l'utilisateur avec `must_change_password = True`, `email_verified_at = now()` (pas de vérification email pour les comptes créés par admin)
- Retourne `temporary_password` une seule fois dans la réponse (`201 Created`)

**`PATCH /admin/users/{id}/deactivate`** — désactivation
- Met `is_active = False`
- Révoque tous les refresh tokens de l'utilisateur
- Pose le flag `user_disabled:{user_id}` en Redis (TTL 24h, renouvelable)
- Réponse `200` avec confirmation

**`PATCH /admin/users/{id}/reactivate`** — réactivation
- Met `is_active = True`
- Supprime le flag `user_disabled:{user_id}` en Redis

**`POST /admin/users/{id}/reset-password`** — reset forcé par admin
- Génère un nouveau mot de passe temporaire 16 chars
- Met `must_change_password = True`
- Révoque tous les refresh tokens de l'utilisateur
- Pose le flag `user_disabled:{user_id}` en Redis pour forcer re-login
- Retourne `temporary_password` une seule fois

### Super-admin — cross-tenant

**`POST /admin/impersonate/{tenant_slug}`** — impersonation sécurisée
- Génère un access token spécial : TTL 5 min (non renouvelable — pas de refresh token émis)
- Claims supplémentaires : `impersonated_by: <super_admin_email>`, `impersonation: true`, `jti: UUID4`
- Logue dans `tenant_config_audits` : `actor`, `action = "impersonate"`, `ip_address`, `user_agent`
- Le claim `impersonated_by` est visible dans tous les logs applicatifs de la session
- À expiry ou logout → JTI ajouté en deny-list Redis

**`GET /admin/impersonation-log`** — audit cross-tenant
- Retourne les entrées `tenant_config_audits` où `field_name = "impersonate"` dans tous les tenants, triées par date DESC
- Filtres : `tenant_slug` (optionnel), `actor`, pagination

**`GET /admin/tenants/users`** — cross-tenant user listing
- Liste les utilisateurs à travers tous les tenants
- Filtres : `tenant_slug`, `role`, `email_verified`, `is_active`, pagination
- Requêtes parallèles sur les schemas tenants via `asyncio.gather`

**`PATCH /admin/tenants/{id}/suspend`** — suspension de tenant
- Colonne `suspended_at TIMESTAMPTZ` sur `public.tenants` (déjà en place — migration `0017`)
- Met `suspended_at = now()` pour suspendre, `null` pour réactiver
- `TenantMiddleware` : vérifie `suspended_at` et retourne `403 TENANT_SUSPENDED` pour toutes les requêtes (sauf `/auth/login` pour que l'admin puisse constater la suspension)
- Révoque tous les refresh tokens de tous les utilisateurs du tenant (bulk UPDATE)

**`PATCH /admin/tenants/{id}/unsuspend`** — réactivation

## Sécurité implémentée

- [🔒] **Login timing-safe** : `DUMMY_HASH` bcrypt pour les emails inexistants.
- [🔒] **Rate limiting SlowAPI** sur toutes les routes sensibles (`/register` 5/min, `/login` 10/min, `/refresh` 20/min, `/forgot-password` 3/min, `/reset-password` 5/min, `/resend-verification` 3/min, `/change-password` 5/min, `/security/login-events` 30/min).
- [🔒] **Refresh tokens** : double stockage bcrypt (vérification) + HMAC-SHA256 `token_lookup` (index O(1)). Révocation à la rotation et au logout.
- [🔒] **JWT payload** mis en cache dans `request.state.jwt_payload` (décodé une seule fois par middleware).
- [🔒] **MFA TOTP** obligatoire pour les super-admins ayant activé `mfa_enabled`, avec QR code de setup et codes de secours hashés.
- [🔒] **JTI deny-list Redis** : révocation immédiate des access tokens (clé `jti:{jti}`, TTL résiduel). Déclenché sur : logout (toutes sessions ou session ciblée), désactivation de compte, reset password (forcé ou volontaire), fin de session d'impersonation.
- [🔒] **Flag `user_disabled:{user_id}` en Redis** : désactivation compte, suspension tenant — invalidation en masse sans requête DB.
- [🔒] **Slug DDL double rempart** : validation Pydantic + `tenant_schema_name()` dans `app/core/tenant.py` — protège les appels hors HTTP (scripts d'admin, jobs ARQ, appels directs).
- [🔒] **`email_verified` dans `/me`** : le frontend peut afficher une bannière de rappel.
- [🔒] **`Retry-After: 60` sur 429** : guide les clients throttlés.
- [🔒] **`must_change_password` middleware guard** : `403 PASSWORD_CHANGE_REQUIRED` si flag actif (sauf sur `/auth/change-password` et `/auth/logout`).
- [🔒] **Tokens d'impersonation** : TTL 5 min, claim `impersonated_by`, audit `tenant_config_audits`, JTI deny-list à expiry.
- [🔒] **Password reset token** : token court (8 chars), stocké hashé (bcrypt), TTL 30 min, jamais en URL (POST uniquement).
- [🔒] **Audit des connexions non-bloquant** : `asyncio.create_task()` ne bloque pas le login, logs dans MongoDB.
- [🔒] **Énumération d'emails** : `/register` révèle si email existe (409) — accepté pour B2B. `/forgot-password` répond toujours 202 — protégé.
- [⚠️ PROD] `_TENANT_DDL_STATEMENTS` doit rester synchronisé avec les migrations Alembic — tout écart crée une divergence entre tenants existants et nouveaux.
- [⚠️ PROD] Redis partagé pour JTI deny-list — s'assurer que toutes les instances Railway lisent/écrivent sur le même Redis (via `app.state.arq_pool`).
- [⚠️ PROD] MongoDB TTL sur login_events : configurable via settings, par défaut 90 jours.

## Migrations

| # | Table | Changement |
|---|-------|------------|
| 0018 | `users` (tenant) | `password_reset_token`, `password_reset_expires_at`, `must_change_password` |
| 0019 | `refresh_tokens` (tenant) | `user_agent`, `ip_address` |

*Note : migration `0017` (suspension tenant) et `0012` (tenant_config_audits) déjà en place.*

## Améliorations réalisées — Récapitulatif

✅ Récupération de mot de passe (forgot + reset password flow avec token 8 chars)  
✅ Renvoi de mail de vérification (POST /resend-verification authentifié)  
✅ Changement de mot de passe sécurisé (must_change_password flag + middleware)  
✅ Sessions multi-appareils (user_agent + ip_address + session_id)  
✅ Audit des connexions (MongoDB login_events_{slug})  
✅ Gestion admin des utilisateurs (list, create, deactivate, reactivate, reset-password)  
✅ Super-admin impersonation (token 5 min + audit log)  
✅ Suspension de tenant (bulk token revocation)  
✅ Cross-tenant user listing (asyncio.gather)  
✅ JTI deny-list Redis (révocation immédiate)  
✅ Slug DDL double rempart (Pydantic + tenant_schema_name())  
✅ email_verified dans /me + Retry-After 60  

---

## Architecture

La sécurité repose sur plusieurs niveaux :
1. **Rate limiting** : SlowAPI limite les tentatives par minute
2. **Timing-safe auth** : DUMMY_HASH uniformise les durées
3. **Token lifecycle** : JTI deny-list + flag `user_disabled` pour révocation immédiate
4. **Session isolation** : multi-tenant via schema PostgreSQL + tenant_slug dans JWT
5. **Audit trail** : MongoDB login_events + tenant_config_audits pour traçabilité
6. **Admin controls** : désactivation/réactivation/reset forcé avec TTL Redis
7. **Password policy** : Pydantic validators + complexity requirements
