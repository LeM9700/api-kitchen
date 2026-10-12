# Mise en production — HACCP phase 3

## Préparation

1. Vérifier que la révision `haccp_p3_20261004` est disponible dans l’artefact déployé.
2. Sauvegarder la base et vérifier qu’aucune migration n’est en cours.
3. Exécuter `alembic upgrade head`. La migration parcourt les schémas `tenant_*` et remplace la clé primaire de `stock_batch_positions` par `(batch_id, establishment_id, ingredient_id)`.
4. Vérifier sur un échantillon que chaque établissement dispose d’une ligne `establishment_stock` et que les reliquats inconnus restent `NULL`.

## Vérifications après déploiement

- Contrôler que le même lot peut exister dans deux établissements sans collision.
- Tester l’allocation d’une commande sur deux lots et vérifier les mouvements négatifs correspondants.
- Tester le rejeu avec la même clé d’idempotence et le rejet avec une clé différente.
- Tester le retour d’un lot expiré : le mouvement positif existe, mais la quantité ne redevient pas disponible.
- Surveiller les erreurs `SITE_STOCK_INSUFFICIENT`, `STOCK_ORDER_ALREADY_ALLOCATED` et les erreurs de contrainte de clé étrangère.

## Retour arrière

Ne pas exécuter le downgrade en production si deux établissements utilisent le même lot : l’ancien schéma ne peut pas représenter ces positions. Restaurer la sauvegarde ou corriger les données avant toute tentative de retour arrière.

La phase 3 ne modifie pas `Product.is_active` et ne bloque pas encore les commandes ; ces comportements seront activés par les phases suivantes.
