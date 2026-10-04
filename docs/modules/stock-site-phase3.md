# Stock local et lots — phase 3 HACCP

Cette phase fournit les décisions transactionnelles nécessaires au stock par établissement. Elle ne branche pas encore ces décisions sur la création ou le paiement des commandes ; ce raccord est réservé à la phase 4.

## Règles garanties

- Chaque position de lot est portée par `(lot, établissement, ingrédient)` ; un même lot peut donc être suivi séparément dans deux établissements.
- Un lot utilisable doit avoir un reliquat connu et positif, ne pas être écarté, être `sealed` ou `opened`, et avoir une DLC effective future.
- La DLC effective est la plus proche des DLC primaire, secondaire et tertiaire.
- L’allocation verrouille la commande et les positions, consomme les lots dans l’ordre FIFO de DLC effective et écrit une allocation ainsi qu’un mouvement d’audit par lot.
- Une allocation existante est rejetée, sauf rejeu avec la même clé d’idempotence.
- Un retour restaure uniquement la quantité non déjà retournée. Si le lot a expiré, le mouvement est conservé mais le reliquat reste indisponible.
- Un stock inconnu (`NULL`) est traité comme zéro disponible.

## Interface de service

- `site_product_availability(...)` retourne une disponibilité calculée localement et une cause explicite (`expired_or_insufficient_site_stock`, `ingredient_missing`).
- `allocate_order(...)` et `restore_order(...)` doivent être appelés dans la transaction du cas d’usage appelant.

La désactivation catalogue et le blocage de commande sont volontairement laissés à la phase 4, afin d’éviter un état global incohérent entre établissements.
