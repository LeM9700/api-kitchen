# Paiements

Module de paiement Stripe pour :

- la création de PaymentIntent ;
- la finalisation canonique par webhook Stripe ;
- la confirmation client idempotente ;
- la confirmation automatique de commande via le service commandes ;
- le remboursement admin manuel ou automatique ;
- les paiements caisse simples (`cash`, `external_terminal`, `cash_register`) crees par `/orders/manual` ;
- les remboursements partiels cumulés ;
- la lecture client, staff et admin des paiements ;
- les agrégats financiers staff/admin ;
- la préparation Stripe Connect Express par tenant.

Le code applicatif principal se trouve dans `app/modules/payments`.

## Endpoints

| Méthode | Chemin | Authentification | Rôles | Usage |
|---------|--------|------------------|-------|-------|
| POST | `/api/v1/payments/intent` | Bearer JWT | tous | Crée un PaymentIntent Stripe pour une commande |
| POST | `/api/v1/payments/confirm` | Bearer JWT | tous | Synchronise le client avec la finalisation idempotente |
| POST | `/api/v1/payments/webhook` | Public Stripe | Stripe uniquement | Reçoit les événements Stripe signés |
| POST | `/api/v1/payments/{order_id}/refund` | Bearer JWT | admin | Cree un remboursement total ou partiel |
| GET | `/api/v1/payments/{order_id}` | Bearer JWT | client propriétaire, staff, admin | Détail paiement, remboursements et reçu |
| GET | `/api/v1/payments/{order_id}/refunds` | Bearer JWT | client propriétaire, staff, admin | Historique des remboursements |
| GET | `/api/v1/payments` | Bearer JWT | staff, admin | Liste paginée des paiements du tenant |
| GET | `/api/v1/payments/summary` | Bearer JWT | staff, admin | Totaux encaissés, remboursés et nets |
| GET | `/api/v1/payments/export/csv` | Bearer JWT | admin | Export CSV des paiements |
| POST | `/api/v1/payments/terminal/connection-token` | Bearer JWT | staff, admin | Token Stripe Terminal |
| POST | `/api/v1/payments/terminal/intent` | Bearer JWT | staff, admin | PaymentIntent `card_present` |
| GET | `/api/v1/payments/terminal/readers` | Bearer JWT | staff, admin | Liste lecteurs Terminal |
| POST | `/api/v1/payments/terminal/readers/{reader_id}/process` | Bearer JWT | staff, admin | Lance l'action lecteur |
| POST | `/api/v1/payments/terminal/readers/{reader_id}/cancel` | Bearer JWT | staff, admin | Annule l'action lecteur |

Limites de débit :

- `/webhook` : 60/minute.
- `/{order_id}/refund` : 5/minute.

## Corps de requête et réponses

### `POST /payments/intent`

Requête :

```json
{
  "order_id": 123
}
```

Réponse :

```json
{
  "client_secret": "pi_..._secret_...",
  "payment": {
    "id": 1,
    "order_id": 123,
    "amount": 42.5,
    "currency": "EUR",
    "status": "pending",
    "provider_payment_id": "pi_..."
  }
}
```

### `POST /payments/confirm`

Requête :

```json
{
  "provider_payment_id": "pi_..."
}
```

Réponse : `PaymentOut`.

Cet endpoint n’est pas la source de vérité. Il utilise le même chemin de finalisation idempotent que le webhook et sert surtout à synchroniser le client après Stripe Elements ou Payment Sheet.

### `POST /payments/{order_id}/refund`

Requête :

```json
{
  "amount": 1500,
  "reason": "customer_request"
}
```

`amount` est exprimé en centimes. Si `amount` est omis ou vaut `null`, l’API rembourse le montant encore remboursable.
`reason` est obligatoire.

Réponse : `RefundOut`.

### `GET /payments`

Liste staff/admin. Paramètres de requête :

- `page`, `page_size` ;
- `status` : statuts séparés par des virgules ;
- `date_from`, `date_to` ;
- `order_id` ;
- `provider` ;
- `min_amount`, `max_amount`.

Réponse : `PaginatedResponse[PaymentListItemOut]`.

### `GET /payments/summary`

Endpoint d’agrégats staff/admin. Paramètres de requête :

- `date_from` ;
- `date_to`.

Réponse :

```json
{
  "collected_amount_cents": 10000,
  "refunded_amount_cents": 2500,
  "net_amount_cents": 7500,
  "payment_count": 4,
  "refund_count": 1,
  "counts_by_status": {
    "paid": 3,
    "refunded": 1
  }
}
```

## Modèles de données

### `payments`

Champs :

- `id` ;
- `order_id` ;
- `provider` ;
- `provider_payment_id` ;
- `provider_account_id` ;
- `external_reference` ;
- `amount` en euros ;
- `amount_received` en euros, utile pour l'espece ;
- `currency` ;
- `status` ;
- `expires_at` ;
- `created_by_user_id` ;
- `created_at`.

Providers supportes :

- `stripe`
- `stripe_terminal`
- `cash`
- `external_terminal`
- `cash_register`

Statuts :

- `pending` : PaymentIntent créé, paiement non finalisé.
- `paid` : paiement Stripe reussi ou paiement caisse valide.
- `partially_refunded` : au moins un remboursement a réussi, mais il reste un solde remboursable.
- `refunded` : montant payé intégralement remboursé.
- `failed` : paiement échoué.
- `expired` : paiement pending expiré par le cleanup.
- `refund_failed` : paiement réussi, mais remboursement automatique de compensation échoué.

### `refunds`

Champs :

- `id` ;
- `order_id` ;
- `payment_id` ;
- `stripe_refund_id` ;
- `amount` en centimes ;
- `reason` ;
- `status` ;
- `failure_reason` ;
- `created_by_user_id` ;
- `created_at`.

Statuts de remboursement :

- `pending` ;
- `succeeded` ;
- `failed`.

## Flux métier

### Création du PaymentIntent

`POST /intent` :

1. Charge la commande dans le schéma du tenant courant.
2. Crée un `Payment(status="pending")` local.
3. Résout le contexte Stripe Connect depuis `public.tenant_configs.stripe_account_id`.
4. Appelle `stripe.PaymentIntent.create`.
5. Envoie les métadonnées Stripe :
   - `tenant_slug` ;
   - `order_id` ;
   - `payment_id`.
6. Stocke `provider_payment_id`, `provider_account_id` si présent, et `expires_at`.

Fallback local :

- le fallback `local_{payment_id}` est autorisé uniquement en local/dev/test ;
- en production, un échec Stripe lève `STRIPE_PAYMENT_FAILED`.

### Finalisation

Le webhook `payment_intent.succeeded` est canonique.

Le webhook et `/confirm` appellent le même chemin de finalisation :

1. Chercher le paiement par `provider_payment_id`.
2. Si le paiement est déjà finalisé, retourner une réponse idempotente.
3. Passer `payment.status = "paid"`.
4. Passer `order.payment_status = "paid"`.
5. Si la commande est encore `pending`, appeler `orders.service.update_status(..., "confirmed")`.

L’appel à `orders.service.update_status` est essentiel : il déclenche la déduction du stock via le workflow de commande existant.

### Paiements caisse

Les paiements `cash`, `external_terminal` et `cash_register` sont crees depuis `POST /api/v1/orders/manual`.

Regles :

- aucun PaymentIntent Stripe n'est cree ;
- le paiement est insere avec `status = paid` ;
- `created_by_user_id` audite le staff/admin qui encaisse ;
- `external_reference` est obligatoire pour `external_terminal` et `cash_register` ;
- la commande est marquee `payment_status = paid`, puis confirmee via `orders.service.update_status`.

### Compensation en cas de stock insuffisant

Si la confirmation de commande échoue après paiement réussi :

1. Le paiement reste marqué comme payé.
2. Le service tente un remboursement automatique.
3. Un payload d’alerte staff/admin est produit avec :
   - `tenant_slug` ;
   - `order_id` ;
   - `payment_id` ;
   - la raison de l’échec ;
   - un message exploitable côté utilisateur.
4. Si le remboursement réussit, le paiement passe à `refunded`.
5. Si le remboursement échoue, le paiement passe à `refund_failed`.

L’interface frontend/admin n’est pas implémentée ici. Le service payments expose les informations nécessaires pour afficher un message client et alerter le staff/admin.

## Sécurité webhook

Le webhook :

- lit le corps brut avant tout parsing JSON ;
- exige le header `stripe-signature` ;
- vérifie la signature avec une tolérance explicite ;
- rejette les signatures invalides avec HTTP 400 ;
- exige `metadata.tenant_slug` sur l’objet Stripe ;
- ne revient jamais vers le tenant `default` ;
- ouvre la session DB tenant seulement après validation des métadonnées.

Métadonnées Stripe attendues :

```json
{
  "tenant_slug": "tenant-a",
  "order_id": "123",
  "payment_id": "456"
}
```

## Règles de remboursement

Remboursements manuels :

- reserves au role admin ;
- `reason` obligatoire ;
- la commande doit être `cancelled` ou `delivered` ;
- le montant est exprimé en centimes ;
- `amount = null` signifie : rembourser le solde restant.
- pour `cash`, `external_terminal` et `cash_register`, aucun appel Stripe n'est fait ; une entree `refunds` locale est creee et le paiement passe `refunded` ou `partially_refunded`.

Remboursements automatiques :

- chemin interne au service ;
- utilisés après un paiement réussi mais une commande non confirmable ;
- peuvent contourner la règle de statut de commande du remboursement manuel.

Validation :

- le montant doit être positif ;
- le cumul des remboursements `pending + succeeded` ne peut pas dépasser le montant payé ;
- un remboursement supérieur au solde restant est rejeté avant tout appel Stripe ;
- les erreurs Stripe sont nettoyées avant exposition au client.

## Stripe Connect

Payments utilise la référence de configuration publique existante :

- table : `public.tenant_configs` ;
- champ : `stripe_account_id`.

Lorsqu’elle est configurée, cette référence est utilisée comme contexte de compte Stripe tenant pour :

- la création de PaymentIntent ;
- la création de remboursements ;
- la récupération de reçu ;
- l’annulation de PaymentIntent pendant le cleanup.

Les secrets Stripe bruts ne sont pas exposés dans les schémas payments ni dans les erreurs client.

En production, un tenant sans configuration Stripe lève :

- code : `PAYMENT_PROVIDER_NOT_CONFIGURED` ;
- statut : HTTP 409.

## Interfaces client et métier

Interface client :

- `POST /payments/intent` pour obtenir `client_secret` ;
- `POST /payments/confirm` pour synchroniser le client ;
- `GET /payments/{order_id}` pour afficher statut, reçu et remboursements.

Interfaces staff/admin :

- `GET /payments` pour la réconciliation ;
- `GET /payments/{order_id}` ;
- `GET /payments/{order_id}/refunds` ;
- `GET /payments/summary` pour les totaux encaissés, remboursés et nets.

Interface admin :

- `POST /payments/{order_id}/refund`.

## Cleanup opérationnel

`cleanup_expired_pending_payments(session, tenant_slug, older_than_hours=24)` :

- cherche les paiements `pending` trop anciens ;
- tente l’annulation Stripe pour les vrais identifiants Stripe ;
- marque les paiements comme `expired` ;
- retourne les compteurs, les IDs concernés et les échecs d’annulation.

La fonction est prête pour une intégration future worker/cron. Le scheduling n’est pas implémenté dans cette phase du module.

## Migrations et provisioning

Migration :

- `alembic/versions/0027_payments_improvements.py`
- `alembic/versions/0035_admin_staff_api_contracts.py`

Elle ajoute :

- `payments.provider_account_id` ;
- `payments.expires_at` ;
- `refunds.failure_reason` ;
- `refunds.created_by_user_id`.

La migration `0035` ajoute :

- `payments.external_reference`
- `payments.amount_received`
- `payments.created_by_user_id`

Elle crée aussi `refunds` si la table manque pour des tenants provisionnés avant l’ajout de cette table au bootstrap manuel.

Le bootstrap des nouveaux tenants dans `app/modules/auth/service.py` crée aussi les structures `payments` et `refunds` mises à jour.

## Vérification

Commandes ciblées :

```powershell
$env:PYTHONPATH='C:\Users\melbo\workspace\pizza\api-pizza'
uv run pytest tests/test_payments_interfaces.py -q
uv run python -m compileall app/modules/payments alembic/versions/0035_admin_staff_api_contracts.py
```

Dernier résultat ciblé :

- `tests/test_payments_interfaces.py` passe dans le paquet P0 cible.
- Les modules payments compilent.

Note sur la suite complète :

- `pytest tests -q` necessite une base PostgreSQL locale migree. Les modeles ORM P0 attendent la migration `0035`.

## Limites connues

- Le cleanup n’est pas encore branché à ARQ/cron.
- L’onboarding Stripe Connect UI/API est hors scope de ce module ; payments consomme seulement `stripe_account_id`.
- Le lien de reçu dépend de la présence de `latest_charge.receipt_url` côté Stripe.
- Les vues financières cross-tenant super-admin ne sont pas implémentées ici.
