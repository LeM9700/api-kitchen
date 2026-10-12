# Mise en production — HACCP phase 4

## Périmètre

La création d'une commande vérifie maintenant, pour l'établissement résolu, le contrôle HACCP
quotidien et la disponibilité locale des produits. Le garde reste inactif tant que
`establishment_operations.strict_enabled` est faux. Cette phase ne modifie pas globalement
`Product.is_active` et ne lance pas encore l'allocation physique finale du stock.

## Déploiement

1. Déployer l'API et vérifier que les migrations des phases 1 et 3 sont déjà appliquées.
2. Maintenir `strict_enabled=false` pour les établissements non préparés.
3. Pour un établissement activé, réaliser un contrôle d'ouverture complet, réconcilier le stock
   local et tester une commande refusée puis une commande acceptée.
4. Vérifier que le refus expose `HACCP_ORDERING_BLOCKED` ou `HACCP_STOCK_UNAVAILABLE` sans révéler
   les preuves internes au client.
5. Vérifier les commandes invitées, manuelles, pickup, livraison et les rejeux d'idempotence.

## Surveillance et retour arrière

Surveiller les taux de refus HACCP, les erreurs de résolution d'établissement et la latence de
lecture des lots. En cas de problème, désactiver `strict_enabled` pour l'établissement concerné,
sans supprimer les preuves ni modifier les stocks. Le retour applicatif conserve les tables et les
migrations : les données HACCP restent auditables.
