# Auth — Améliorations complètes : Design Spec

**Date :** 2026-06-23  
**Basé sur :** `docs/modules/auth.md` — axes d'amélioration + ce qui manque pour les interfaces  
**Statut :** Approuvé

---

## Contexte

Le module auth couvre inscription, login, refresh, logout et vérification email. Cette spec couvre l'ensemble des axes d'amélioration identifiés dans la doc du module : sécurité transversale, nouveaux flows utilisateur, gestion des sessions, admin tenant, super-admin et audit des connexions.

Stack existante : FastAPI + SQLAlchemy async + PostgreSQL multi-tenant + ARQ/Redis + JWT (access 15 min / refresh rotatif) + MongoDB (stats admin).

---

## Section 1 — Sécurité transversale

### 1a. Validation slug DDL (double rempart)

**Problème :** `_TENANT_DDL_STATEMENTS` interpole le slug dans le nom du schema PostgreSQL. Un slug malveillant pourrait injecter du DDL si la validation Pydantic est contournée (script, job ARQ, appel direct).

**Solution :**
- Regex `^[a-z0-9][a-z0-9_-]{0,62}[a-z0-9]$` ajoutée dans `RegisterRequest` (Pydantic `field_validator`)
- `tenant_schema_name()` dans `app/core/tenant.py` valide également cette regex et lève `AppError("INVALID_SLUG", "Invalid tenant slug", 400)` si non conforme
- Double rempart : protège les appels hors HTTP (scripts d'admin, jobs ARQ, tests directs)

### 1b. JTI + deny-list Redis

**Problème :** Les access tokens (15 min) ne peuvent pas être révoqués après émission.

**Solution :**
- `create_access_token()` ajoute un claim `jti` (UUID4) dans chaque token
- `get_current_user()` vérifie `REDIS.EXISTS("jti:{jti}")` avant de valider — si présent → `401 UNAUTHORIZED`
- Révocation : `REDIS.SETEX("jti:{jti}", ttl_résiduel_secondes, "1")` déclenchée sur :
  - Logout (toutes sessions ou session ciblée)
  - Désactivation de compte
  - Reset password (forcé ou volontaire)
  - Fin de session d'impersonation
- Flag complémentaire `user_disabled:{user_id}` en Redis pour invalider en masse (désactivation compte, suspension tenant)
- Client Redis : réutilise `app.state.arq_pool` (connexion ARQ déjà disponible)

### 1c. `email_verified` dans `/me` + `Retry-After` sur 429

- `UserOut` schema : ajout du champ `email_verified: bool` (calculé : `email_verified_at is not None`)
- Handler `/me` : retourne ce champ depuis la BDD (nécessite une requête tenant pour lire `email_verified_at`)
- Custom exception handler `RateLimitExceeded` : ajoute le header `Retry-After: 60` dans la réponse 429

---

## Section 2 — Nouveaux flows d'authentification

### 2a. Mot de passe oublié

**Migration :** colonnes `password_reset_token VARCHAR(64)` et `password_reset_expires_at TIMESTAMPTZ` sur `users` (nullable)

**`POST /auth/forgot-password`** — public, rate-limit 3/min
- Body : `{ email: str, tenant_slug: str }`
- Génère un token alphanumérique 8 chars (`secrets.token_urlsafe(6)`)
- Stocké hashé (bcrypt) dans `password_reset_token`, TTL 30 min dans `password_reset_expires_at`
- Réponse toujours `202 Accepted` avec message générique (évite l'énumération d'emails — contexte différent de /register où la transparence B2B est acceptable)
- Enqueue job ARQ `send_password_reset_email(tenant_slug, user_id, token_en_clair)`

**`POST /auth/reset-password`** — public, rate-limit 5/min
- Body : `{ email: str, tenant_slug: str, token: str, new_password: str }`
- Vérifie le token par bcrypt + TTL ; erreur `400 INVALID_TOKEN` si invalide ou expiré
- Valide la politique mot de passe (même validator que RegisterRequest)
- Hashe le nouveau mot de passe, nullifie `password_reset_token` + `password_reset_expires_at` (usage unique)
- Révoque tous les refresh tokens de l'utilisateur
- Met à jour `must_change_password = False` si actif
- Réponse `200` avec message de confirmation (pas de tokens — l'utilisateur doit se reconnecter)

### 2b. Renvoi de vérification email

**`POST /auth/resend-verification`** — authentifié (Bearer JWT), rate-limit 3/min
- Vérifie que `email_verified_at is None` → sinon `400 ALREADY_VERIFIED`
- Génère un nouveau token UUID4, remet `email_verification_expires_at = now() + 24h`
- Enqueue job ARQ `send_verification_email`
- Réponse `202 Accepted`

### 2c. Changement de mot de passe

**`POST /auth/change-password`** — authentifié, rate-limit 5/min
- Body : `{ current_password: str | None, new_password: str }`
- Si `must_change_password = True` : `current_password` non requis
- Si `must_change_password = False` : vérifie `current_password` par bcrypt obligatoirement
- Valide la politique mot de passe
- Met `must_change_password = False`
- Révoque tous les refresh tokens sauf le courant (l'utilisateur reste connecté)
- Invalide le JTI de l'ancien access token en Redis

**Middleware `must_change_password` :**
- `must_change_password` ajouté dans le payload JWT à l'émission
- `get_current_user()` : si flag actif → lève `403 PASSWORD_CHANGE_REQUIRED` sur toutes les routes sauf `POST /auth/change-password` et `POST /auth/logout`

**Migration :** colonne `must_change_password BOOLEAN NOT NULL DEFAULT FALSE` sur `users`

---

## Section 3 — Gestion des sessions multi-appareils

**Migration :** colonnes `user_agent VARCHAR(512)` et `ip_address VARCHAR(45)` (nullable) sur `refresh_tokens`

`issue_tokens()` reçoit la `Request` FastAPI en paramètre optionnel et alimente ces colonnes à l'émission.

**`TokenResponse` mis à jour :**
Login, refresh et création de compte retournent désormais un champ `session_id` (l'`id` de la ligne `refresh_tokens`) — le client le stocke et le transmet en query param pour identifier sa session courante.

**`GET /auth/sessions?current_session_id={id}`** — authentifié
- Retourne les refresh tokens actifs (non révoqués, non expirés) de l'utilisateur courant
- Champs : `id`, `created_at`, `expires_at`, `user_agent`, `ip_address`, `is_current`
- `is_current` : `true` si `id == current_session_id` (query param optionnel)

**`DELETE /auth/sessions/{session_id}`** — authentifié
- Révoque un refresh token précis appartenant à l'utilisateur courant (vérifie `user_id`)
- Si session courante révoquée → ajoute le JTI en deny-list Redis

**`DELETE /auth/sessions`** — authentifié (logout global)
- Remplace le logout actuel
- Révoque tous les refresh tokens sauf le courant par défaut
- Query param `?revoke_current=true` pour tout révoquer (déconnexion totale)
- Ajoute les JTI des tokens révoqués en deny-list Redis

---

## Section 4 — Gestion des utilisateurs côté admin tenant

Tous les endpoints sous `/api/v1/admin/users`, protégés par `require_role("admin")`.

**`GET /admin/users`**
- Pagination : `page`, `page_size`
- Filtres : `role` (customer/staff/admin), `is_active` (bool), `email_verified` (bool)
- Retourne : `id`, `email`, `full_name`, `role`, `is_active`, `email_verified`, `created_at`, `must_change_password`

**`POST /admin/users`** — création avec mot de passe temporaire
- Body : `{ email: str, full_name: str | None, role: Literal["staff", "admin"] }`
- Génère mot de passe temporaire 16 chars (`secrets.token_urlsafe(12)`)
- Crée l'utilisateur avec `must_change_password = True`, `email_verified_at = now()` (pas de vérification email pour les comptes créés par admin)
- Retourne `temporary_password` une seule fois dans la réponse (`201 Created`)

**`PATCH /admin/users/{id}/deactivate`**
- Met `is_active = False`
- Révoque tous les refresh tokens de l'utilisateur
- Pose le flag `user_disabled:{user_id}` en Redis (TTL 24h, renouvelable)
- Réponse `200` avec confirmation

**`PATCH /admin/users/{id}/reactivate`**
- Met `is_active = True`
- Supprime le flag `user_disabled:{user_id}` en Redis

**`POST /admin/users/{id}/reset-password`** — reset forcé par admin
- Génère un nouveau mot de passe temporaire 16 chars
- Met `must_change_password = True`
- Révoque tous les refresh tokens de l'utilisateur
- Pose le flag `user_disabled:{user_id}` en Redis pour forcer re-login
- Retourne `temporary_password` une seule fois

---

## Section 5 — Super-admin

Tous les endpoints sous `/api/v1/admin`, protégés par `require_role("super-admin")`.

**`POST /admin/impersonate/{tenant_slug}`**
- Génère un access token spécial :
  - TTL 5 min (non renouvelable — pas de refresh token émis)
  - Claims supplémentaires : `impersonated_by: <super_admin_email>`, `impersonation: true`
- Logue dans `tenant_audit_log` (migration `0012`) : `actor`, `action = "impersonate"`, `tenant_slug`, `timestamp`, `super_admin_ip`
- Le claim `impersonated_by` est visible dans tous les logs applicatifs de la session
- À expiry ou logout → JTI ajouté en deny-list Redis

**`GET /admin/impersonation-log`**
- Retourne les entrées `tenant_audit_log` où `action = "impersonate"`, triées par date DESC
- Filtres : `tenant_slug`, `actor`, plage de dates, pagination

**`PATCH /admin/tenants/{id}/suspend`**
- Migration `0017` : colonne `suspended_at TIMESTAMPTZ` sur `public.tenants` (déjà en place)
- Met `suspended_at = now()` pour suspendre, `null` pour réactiver
- `TenantMiddleware` : vérifie `suspended_at` et retourne `403 TENANT_SUSPENDED` pour toutes les requêtes vers le tenant (sauf `/auth/login` pour que l'admin puisse constater la suspension)
- Révoque tous les refresh tokens de tous les utilisateurs du tenant (bulk UPDATE)

**`GET /admin/tenants/users`** — cross-tenant
- Liste les utilisateurs à travers tous les tenants
- Filtres : `tenant_slug`, `role`, `email_verified`, `is_active`, pagination
- Requêtes parallèles sur les schemas tenants via `asyncio.gather`

---

## Section 6 — Audit des connexions (MongoDB)

**Collection :** `login_events_{tenant_slug}` dans MongoDB

**Structure document :**
```json
{
  "tenant_slug": "pizza-roma",
  "user_id": 42,
  "email": "user@example.com",
  "ip_address": "1.2.3.4",
  "user_agent": "Mozilla/5.0...",
  "success": true,
  "failure_reason": null,
  "created_at": "2026-06-23T10:00:00Z"
}
```

**Intégration :**
- Loggué dans `service.authenticate()` après chaque tentative (succès ET échec)
- `user_id` nullable (null si email inconnu — l'email est quand même loggué pour détecter le credential stuffing)
- Index TTL MongoDB sur `created_at` : rétention 90 jours (configurable via settings)
- Logging non-bloquant : `asyncio.create_task()` pour ne pas impacter la latence du login

**`GET /admin/security/login-events`** — `require_role("admin")`, rate-limit 30/min
- Filtres : `success` (bool), `email`, `ip_address`, `date_from`, `date_to`, pagination
- Permet à l'admin tenant de détecter des patterns suspects

---

## Migrations requises

| # | Table | Changement |
|---|-------|------------|
| 0018 | `users` (tenant) | `password_reset_token`, `password_reset_expires_at`, `must_change_password` |
| 0019 | `refresh_tokens` (tenant) | `user_agent`, `ip_address` |
| 0020 | `users` (tenant) | Index sur `password_reset_token` |

*Note : migration `0017` (suspension tenant) déjà en place.*

---

## Nouveaux endpoints — récapitulatif

| Méthode | Path | Auth | Rôle |
|---------|------|------|------|
| POST | `/api/v1/auth/forgot-password` | Public | — rate-limit 3/min |
| POST | `/api/v1/auth/reset-password` | Public | — rate-limit 5/min |
| POST | `/api/v1/auth/resend-verification` | Bearer JWT | tous |
| POST | `/api/v1/auth/change-password` | Bearer JWT | tous |
| GET | `/api/v1/auth/sessions` | Bearer JWT | tous |
| DELETE | `/api/v1/auth/sessions/{id}` | Bearer JWT | tous |
| DELETE | `/api/v1/auth/sessions` | Bearer JWT | tous |
| GET | `/api/v1/admin/users` | Bearer JWT | admin |
| POST | `/api/v1/admin/users` | Bearer JWT | admin |
| PATCH | `/api/v1/admin/users/{id}/deactivate` | Bearer JWT | admin |
| PATCH | `/api/v1/admin/users/{id}/reactivate` | Bearer JWT | admin |
| POST | `/api/v1/admin/users/{id}/reset-password` | Bearer JWT | admin |
| POST | `/api/v1/admin/impersonate/{tenant_slug}` | Bearer JWT | super-admin |
| GET | `/api/v1/admin/impersonation-log` | Bearer JWT | super-admin |
| PATCH | `/api/v1/admin/tenants/{id}/suspend` | Bearer JWT | super-admin |
| GET | `/api/v1/admin/tenants/users` | Bearer JWT | super-admin |
| GET | `/api/v1/admin/security/login-events` | Bearer JWT | admin |

---

## Décisions clés

| Sujet | Décision | Raison |
|-------|----------|--------|
| Reset password token | Token court (8 chars), POST uniquement, jamais en URL | Sécurité : évite les proxy logs |
| Resend verification | Route authentifiée seulement | Évite inbox flooding par attaquant non-auth |
| Sessions enrichies | `user_agent` + `ip_address` sur refresh_tokens | UX : l'utilisateur reconnaît ses appareils |
| Invitation staff | Mot de passe temporaire affiché une fois + `must_change_password` | Simplicité opérationnelle |
| Impersonation | Token court 5 min + claim `impersonated_by` + audit log | Traçabilité complète sur toute la session |
| JWT révocation | Deny-list Redis JTI ciblée | Révocation immédiate sans overhead excessif |
| Login events | MongoDB `login_events_{slug}` | Volume logs, cohérence avec stats existantes |
| Énumération /register | 409 explicite conservé | B2B : transparence UX prime sur anti-enum |
| Slug DDL | Double rempart Pydantic + `tenant_schema_name()` | Protège les appels hors HTTP |
