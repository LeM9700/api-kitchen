# Loyalty

Programme de fidelite tenant-scoped : credit automatique a la livraison, historique, catalogue de recompenses, remise checkout par reservation de points, configuration admin, stats, expiration et notifications preventives.

## Endpoints client

| Methode | Path | Auth | Roles |
|---------|------|------|-------|
| GET | `/api/v1/loyalty/me` | Bearer JWT | tous |
| GET | `/api/v1/loyalty/transactions` | Bearer JWT | tous |
| GET | `/api/v1/loyalty/expiring` | Bearer JWT | tous |
| POST | `/api/v1/loyalty/redeem` | Bearer JWT | tous |
| GET | `/api/v1/loyalty/rewards` | Bearer JWT | tous |
| POST | `/api/v1/loyalty/rewards/{reward_id}/redeem` | Bearer JWT | tous |
| GET | `/api/v1/loyalty/preview` | Bearer JWT | tous |
| POST | `/api/v1/loyalty/checkout/reservations` | Bearer JWT | tous |
| POST | `/api/v1/loyalty/checkout/reservations/{reservation_id}/confirm` | Bearer JWT | tous |
| POST | `/api/v1/loyalty/checkout/reservations/{reservation_id}/cancel` | Bearer JWT | tous |

## Endpoints staff read-only

| Methode | Path | Auth | Roles |
|---------|------|------|-------|
| GET | `/api/v1/loyalty/users/{user_id}` | Bearer JWT | staff, admin |
| GET | `/api/v1/loyalty/users/{user_id}/transactions` | Bearer JWT | staff, admin |

Le staff peut consulter les soldes et historiques, mais ne peut pas modifier les points.

## Endpoints admin

| Methode | Path | Auth | Roles |
|---------|------|------|-------|
| POST | `/api/v1/loyalty/points` | Bearer JWT | admin |
| GET | `/api/v1/loyalty/config` | Bearer JWT | admin |
| PATCH | `/api/v1/loyalty/config` | Bearer JWT | admin |
| GET | `/api/v1/loyalty/rules` | Bearer JWT | admin |
| POST | `/api/v1/loyalty/rules` | Bearer JWT | admin |
| PATCH | `/api/v1/loyalty/rules/{rule_id}` | Bearer JWT | admin |
| DELETE | `/api/v1/loyalty/rules/{rule_id}` | Bearer JWT | admin |
| POST | `/api/v1/loyalty/rewards` | Bearer JWT | admin |
| PATCH | `/api/v1/loyalty/rewards/{reward_id}` | Bearer JWT | admin |
| DELETE | `/api/v1/loyalty/rewards/{reward_id}` | Bearer JWT | admin |
| GET | `/api/v1/loyalty/stats` | Bearer JWT | admin |

## Modeles de donnees

**`loyalty_accounts`** : `id`, `user_id` unique, `points`, `created_at`.

**`loyalty_transactions`** : ledger des mouvements avec `points_delta`, `reason`, `transaction_type`, `source`, `changed_by_user_id`, `order_id`, `reward_id`, `reservation_id`, `metadata`, `created_at`.

Types principaux : `earn`, `redeem`, `manual`, `expire`, `reservation`, `adjustment`.

Sources principales : `order`, `reward`, `checkout`, `staff`, `admin`, `system`.

**`loyalty_config`** : `base_ratio`, `points_expiry_days`, `points_to_euro_rate`, `max_cumulative_multiplier`, `is_active`, `updated_at`.

**`loyalty_rules`** : regles de bonus `category_multiplier`, `period_multiplier`, `day_multiplier`, `first_order`.

**`loyalty_rewards`** : recompenses `discount_euros` ou `free_product`.

**`loyalty_point_reservations`** : reservations checkout avec `reserved`, `confirmed`, `cancelled`, `expired`.

## Comportements metier

### Credit automatique

`credit_points_for_order()` est appele depuis le module orders lors du passage a `delivered`.

Calcul :
1. `base_points = floor(order_total * base_ratio)`.
2. Les regles actives applicables ajoutent leur bonus.
3. Le multiplicateur cumule est plafonne par `loyalty_config.max_cumulative_multiplier`.
4. La transaction est tracee avec `transaction_type=earn`, `source=order`, `order_id`.

Le credit est idempotent par `order_delivered_{order_id}`.

### Rachat libre

`POST /loyalty/redeem` ne prend plus de `user_id` dans le body. Le debit utilise toujours `current_user["id"]`, ce qui ferme l'IDOR historique.

Le debit est atomique : il ne passe que si `points >= points_a_debiter`.

### Catalogue de recompenses

`GET /loyalty/rewards` retourne toutes les recompenses actives avec :
- `can_redeem`
- `missing_points`
- details de la recompense

`POST /loyalty/rewards/{reward_id}/redeem` utilise le meme chemin de debit securise que le rachat libre.

### Remise checkout par reservation

Le client reserve d'abord des points pour une commande :
1. `POST /checkout/reservations`
2. confirmation avec `/confirm` si la commande aboutit
3. annulation avec `/cancel` si le checkout est abandonne

Les points reserves ne sont pas reutilisables par une autre reservation active.

### Expiration et notifications

`GET /loyalty/expiring` expose les points qui expireront bientot.

Le worker `expire_loyalty_points` :
- envoie les notifications preventives d'expiration ;
- deduit ensuite les points expires ;
- trace les expirations avec `transaction_type=expire`, `source=system`.

### Stats admin

`GET /loyalty/stats` retourne :
- membres
- membres actifs sur periode
- points distribues
- points rachetes
- points expires
- solde total en circulation
- taux de redemption
- compteurs par type de transaction
- top rewards

## Securite

- IDOR ferme sur le rachat libre : le `user_id` client est ignore car absent du payload.
- Les debits utilisent une requete atomique `points >= requested`.
- Le staff est read-only pour les soldes et historiques.
- Les modifications manuelles sont admin-only et auditees avec `changed_by_user_id`.
- Les regles et recompenses sont validees selon leur type avant ecriture.
- Le multiplicateur cumule est plafonne par configuration tenant.
