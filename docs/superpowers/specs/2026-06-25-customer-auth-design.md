# Customer Auth & Profile — Design Spec

**Date:** 2026-06-25  
**Statut:** Approuvé

---

## Contexte

L'API pizza est une plateforme SaaS multi-tenant vendue à des restaurants. Chaque restaurant dispose de sa propre app mobile (iOS/Android) avec le `tenant_slug` configuré dans le header `X-Tenant-Slug`.

Actuellement, `POST /auth/register` crée un *nouveau tenant* (onboarding restaurant), pas un compte client. Il n'existe aucun parcours d'auto-inscription pour les clients finaux. Or, des fonctionnalités comme la loyalty requièrent un utilisateur authentifié.

**Décision :** Tout client doit avoir un compte pour commander (pas de mode guest). L'inscription est rapide (email + mot de passe + nom + téléphone) et la vérification email est non bloquante.

---

## Objectif

Ajouter un module `app/modules/customer/` autonome qui gère :
- L'inscription client sur un tenant existant
- La lecture et la mise à jour du profil client
- La suppression de compte (RGPD)

Le login réutilise `POST /api/v1/auth/login` sans modification.

---

## Architecture

### Nouveau module

```
app/modules/customer/
    __init__.py
    router.py     # endpoints HTTP
    schemas.py    # Pydantic in/out
    service.py    # logique métier
```

Monté sur le préfixe `/api/v1/customer` dans `main.py`.

### Fichiers modifiés en dehors du module

| Fichier | Modification |
|---|---|
| `app/modules/auth/models.py` | Ajout colonne `phone: str \| None` sur `User` |
| `app/modules/auth/schemas.py` | Ajout `phone` dans `UserOut` |
| `app/modules/auth/service.py` | Ajout `phone VARCHAR(20)` dans `_TENANT_DDL_STATEMENTS` |
| `app/core/tenancy/tenant.py` | Ajout `/api/v1/customer/register` dans `BYPASS_PATHS` |
| `app/main.py` | Enregistrement du router customer |
| `alembic/versions/` | Nouvelle migration `ADD COLUMN phone` |

---

## Endpoints

### `POST /api/v1/customer/register`

- **Auth :** aucune (dans `BYPASS_PATHS`)
- **Tenant :** lu depuis header `X-Tenant-Slug` via dépendance FastAPI `Header(alias="x-tenant-slug")`
- **Rate limit :** 5/minute par IP
- **Body :** `CustomerRegisterRequest`
- **Réponse 201 :** `TokenResponse` (access_token, refresh_token, session_id)
- **Comportement :**
  1. Vérifie que le tenant existe via `public.tenants`
  2. Vérifie que l'email n'est pas déjà pris dans le schema tenant
  3. Crée `User` avec `role="customer"`
  4. Émet les tokens JWT (via `issue_tokens` existant)
  5. Enqueue `send_verification_email` en async (non bloquant)
- **Erreurs :** `TENANT_NOT_FOUND` 404, `EMAIL_ALREADY_EXISTS` 409

### `GET /api/v1/customer/me`

- **Auth :** JWT requis, `role="customer"` uniquement
- **Réponse 200 :** `CustomerOut`

### `PATCH /api/v1/customer/me`

- **Auth :** JWT requis, `role="customer"` uniquement
- **Body :** `CustomerUpdateRequest` (champs optionnels : `full_name`, `phone`)
- **Réponse 200 :** `CustomerOut`
- **Note :** email et mot de passe non modifiables ici — passent par les endpoints auth existants (`/auth/change-password`, futur `/auth/change-email`)

### `DELETE /api/v1/customer/me`

- **Auth :** JWT requis, `role="customer"` uniquement
- **Body :** `{ "password": str }` — confirmation par mot de passe avant suppression
- **Réponse 204**
- **Comportement :**
  1. Vérifie le mot de passe actuel
  2. Révoque tous les refresh tokens actifs
  3. Marque `is_active=False` sur l'utilisateur (soft delete — préserve l'intégrité des commandes et transactions loyalty)

---

## Schémas Pydantic

```python
# customer/schemas.py

class CustomerRegisterRequest(BaseModel):
    email: EmailStr
    password: str          # politique: 8+ car, 1 maj, 1 chiffre, 1 spécial (!@#$%^&*)
    full_name: str = Field(min_length=1, max_length=255)
    phone: str             # regex: ^\+?[0-9\s\-]{7,20}$

class CustomerOut(BaseModel):
    id: int
    email: EmailStr
    full_name: str | None
    phone: str | None
    role: str
    email_verified: bool
    created_at: datetime

class CustomerUpdateRequest(BaseModel):
    full_name: str | None = None   # min_length=1 si fourni
    phone: str | None = None       # même regex que register si fourni

class CustomerDeleteRequest(BaseModel):
    password: str
```

---

## Modèle de données

### Colonne `phone` sur `users`

- Type : `VARCHAR(20)`, nullable
- Pas d'index, pas d'unicité (un numéro peut exister dans plusieurs tenants)
- Les utilisateurs admin/staff existants ont `phone=NULL`

### Migration Alembic

```python
# alembic/versions/0030_add_phone_to_users.py
def _get_tenant_slugs(bind) -> list[str]:
    result = bind.execute(sa.text("SELECT slug FROM public.tenants"))
    return [row[0] for row in result]

def upgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.add_column("users", sa.Column("phone", sa.String(20), nullable=True), schema=schema)

def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.drop_column("users", "phone", schema=schema)
```

La table `users` est per-tenant (schema `tenant_{slug}`). La migration itère tous les tenants existants via `public.tenants`, identique au pattern de `0018_auth_user_extensions.py`. La colonne est aussi ajoutée dans `_TENANT_DDL_STATEMENTS` de `auth/service.py` pour les nouveaux tenants provisionnés après la migration.

---

## Sécurité

- **Timing-safe :** la vérification d'existence d'email doit utiliser le même pattern que `authenticate()` pour ne pas révéler via timing si un email existe
- **Rate limiting :** 5/minute sur `/customer/register` (identique à `/auth/register`)
- **Soft delete :** `is_active=False` plutôt que suppression physique — les foreign keys vers `orders`, `loyalty_accounts`, etc. restent intègres
- **Confirmation mot de passe sur DELETE :** évite la suppression accidentelle ou via CSRF
- **Révocation tokens sur DELETE :** tous les refresh tokens sont révoqués avant le soft delete

---

## Gestion d'erreurs

| Code | HTTP | Déclencheur |
|---|---|---|
| `TENANT_NOT_FOUND` | 404 | `X-Tenant-Slug` inconnu |
| `MISSING_TENANT_SLUG` | 400 | Header `X-Tenant-Slug` absent |
| `EMAIL_ALREADY_EXISTS` | 409 | Email déjà pris dans le tenant |
| `INVALID_CREDENTIALS` | 401 | Mauvais mot de passe sur DELETE |
| `FORBIDDEN` | 403 | Rôle != customer sur les endpoints /customer/* |

---

## Tests

- `POST /customer/register` : succès, tenant inconnu, email déjà pris, password policy, header manquant
- `GET /customer/me` : succès, token invalide, role admin rejeté
- `PATCH /customer/me` : mise à jour partielle (full_name seul, phone seul, les deux), validation phone
- `DELETE /customer/me` : succès (soft delete + révocation tokens), mauvais mot de passe
- Vérifier que le login via `POST /auth/login` fonctionne après inscription customer
- Vérifier que loyalty (`GET /loyalty/me`) fonctionne pour un customer inscrit
