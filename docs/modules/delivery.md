# Delivery

Livraison par les livreurs du restaurant : zones par établissement (rayon, temps de trajet ou dessin), règles de tarification, frais calculés côté serveur, géocodage d'adresses via Mapbox, interrupteur général.

## Endpoints

| Méthode | Path | Auth | Rôles |
|---------|------|------|-------|
| GET | `/api/v1/delivery/zones` | Public | header `X-Tenant-Slug` ; zones actives **sans contour** |
| GET | `/api/v1/delivery/availability` | Public | header `X-Tenant-Slug` |
| GET | `/api/v1/delivery/zones/manage` | Bearer JWT | staff, admin (toutes les zones, avec contour et règles) |
| POST | `/api/v1/delivery/zones/preview` | Bearer JWT | admin (limité à 20/min) |
| POST | `/api/v1/delivery/zones` | Bearer JWT | admin |
| PUT | `/api/v1/delivery/zones/{id}` | Bearer JWT | admin |
| PATCH | `/api/v1/delivery/zones/{id}` | Bearer JWT | admin (`{is_active}`) |
| DELETE | `/api/v1/delivery/zones/{id}` | Bearer JWT | admin (désactive, ne supprime jamais) |
| PUT | `/api/v1/delivery/zones/{id}/rules` | Bearer JWT | admin (remplace toutes les règles) |
| PUT | `/api/v1/delivery/establishments/{id}/location` | Bearer JWT | admin |
| GET / PUT | `/api/v1/delivery/settings` | Bearer JWT | staff, admin / admin |
| POST | `/api/v1/delivery/check` | Bearer JWT | tous (60/min par utilisateur) |
| GET | `/api/v1/delivery/geocode` | Bearer JWT | tous (60/min par utilisateur) |
| GET | `/api/v1/delivery/reverse-geocode` | Bearer JWT | tous (60/min par utilisateur) |

## Modèles de données

- **`delivery_zones`** : `id`, `name`, `establishment_id` (FK ; `NULL` = zone historique valable pour tous les établissements), `polygon` (toujours un `Polygon` GeoJSON valide), `shape_kind` (`polygon` | `circle` | `isochrone`), `shape_params` (centre, rayon, minutes : pour rouvrir la zone telle qu'elle a été créée), `fee`, `min_order_amount`, `estimated_minutes`, `is_active`.
- **`delivery_zone_rules`** : `zone_id`, `label`, `kind` (`fee` | `free`), `fee`, `min_subtotal`, `days_of_week` (0 = lundi), `start_time`/`end_time` (heure locale de l'établissement, fenêtre qui passe minuit acceptée), `starts_on`/`ends_on`, `priority`, `is_active`.
- **`establishments.latitude` / `longitude`** : position du restaurant (centre des cartes, point de départ des zones).
- **`promotions.free_delivery`** : le code offre aussi les frais de livraison (`discount_value` peut valoir 0).
- **`restaurant_delivery_settings`** (une ligne par tenant, créée au premier accès) : `internal_enabled` est l'interrupteur général ; `version` sert à la concurrence optimiste ; chaque changement est tracé dans `restaurant_delivery_settings_audits`.

## Saisie d'une zone

L'administrateur ne saisit jamais de coordonnées. `shape` accepte :

- `{"kind": "circle", "center_lat", "center_lng", "radius_m"}` : rayon de 100 m à 50 km ;
- `{"kind": "isochrone", "center_lat", "center_lng", "minutes"}` : zone atteignable en voiture en 1 à 60 minutes (Mapbox), simplifiée à 200 sommets au plus ;
- `{"kind": "polygon", "polygon": <GeoJSON>}` : contour dessiné (Polygon, Feature ou FeatureCollection d'un seul polygone).

Le champ historique `polygon` seul reste accepté. `POST /zones/preview` renvoie le contour calculé sans rien enregistrer.

Validation serveur du contour (codes d'erreur 422 stables) : `POLYGON_TOO_FEW_POINTS`, `POLYGON_TOO_MANY_POINTS` (500 max), `POLYGON_INVALID_COORDINATE` (bornes, valeurs finies), `POLYGON_SELF_INTERSECTING`, `POLYGON_TOO_SMALL`, `POLYGON_TOO_LARGE`, `GEOJSON_UNSUPPORTED` (multipolygone, trous), `CIRCLE_RADIUS_OUT_OF_RANGE`, `ISOCHRONE_MINUTES_OUT_OF_RANGE`. Le contour est fermé et orienté automatiquement. Maximum 50 zones actives par établissement (`ZONE_LIMIT_REACHED`, 409) et 20 règles par zone.

## Quelle zone s'applique

La zone active qui contient le point, parmi celles de l'établissement demandé (et les zones historiques sans établissement). Si plusieurs zones se chevauchent (anneaux « 0-3 km » et « 0-6 km » par exemple), **la plus petite gagne** (la plus spécifique), à surface égale la plus petite `id`. `/delivery/check` et la création de commande utilisent la même règle, donc les mêmes frais. Sans `establishment_id`, toutes les zones sont examinées et la réponse indique l'établissement qui livre.

## Calcul des frais (côté serveur uniquement)

Ordre d'application, du plus fort au plus faible :

1. code promo (`free_delivery`) ou récompense fidélité « livraison offerte » : frais à 0 ;
2. règle de zone `free` applicable ;
3. règle de zone `fee` applicable (priorité la plus haute, puis tarif le plus bas) ;
4. tarif de base de la zone.

Une règle est applicable quand l'heure locale de l'établissement tombe dans ses jours, horaires et dates, et que le sous-total atteint son seuil. Le seuil est évalué sur le **sous-total avant remises**, comme dans `/delivery/check`, pour que le prix annoncé soit le prix facturé. `remaining_for_free` indique ce qu'il manque au panier pour débloquer une livraison offerte.

`POST /delivery/check` renvoie `zone_id`, `name`, `establishment_id`, `fee` (frais appliqués), `base_fee`, `free_delivery`, `applied` (`rule`), `applied_label`, `remaining_for_free`, `estimated_minutes`, `min_order_amount`, `min_order_met` (`null` sans `subtotal`). Hors zone : 422 `DELIVERY_ZONE_UNREACHABLE`. Livraison coupée : 409 `DELIVERY_DISABLED`.

## Création de commande

`POST /orders` en livraison exige un point GPS, un téléphone et que la livraison soit activée (voir `docs/modules/orders.md`). Le serveur retrouve la zone et l'établissement, applique le minimum de commande de la zone (`DELIVERY_MIN_ORDER_NOT_MET`) et facture les frais calculés ci-dessus.

## Livraison offerte : promotions et fidélité

- **Promotion** : `free_delivery = true`. Avec `discount_value = 0` le code n'offre que la livraison ; avec une valeur, il cumule remise et livraison offerte. Un code « livraison offerte » seul est refusé hors livraison (`PROMO_DELIVERY_ONLY`) sans consommer d'utilisation.
- **Récompense fidélité** `reward_type = free_delivery` : échangée par le client, elle génère un code à usage unique (non public, lié à l'utilisateur) à saisir dans `promo_code` ; appliquée au comptoir (`loyalty_reward_id`), elle débite les points et offre les frais. Refusée hors livraison (`REWARD_DELIVERY_REQUIRED`).

## Géocodage (Mapbox)

Variables d'environnement :

| Variable | Rôle |
|---|---|
| `MAPBOX_ACCESS_TOKEN` | Jeton **secret** (`sk.*`), serveur uniquement. Vide : `/geocode`, `/reverse-geocode` et les zones par temps de trajet répondent 503 `GEOCODING_NOT_CONFIGURED` ; le reste fonctionne avec des coordonnées déjà connues. |
| `MAPBOX_GEOCODING_PERMANENT` | `true` si les coordonnées sont conservées (elles le sont sur la commande) **et** que le mode « permanent » est activé sur le compte Mapbox. À vérifier avec les conditions Mapbox avant la production. |

Pays autorisés : France (`fr`) et Serbie (`rs`). Les réponses identiques sont mises en cache 10 minutes en mémoire du processus. Les erreurs du fournisseur deviennent 503 `GEOCODING_UNAVAILABLE`. Le niveau de log `httpx` est remonté à WARNING pour que le jeton (présent dans l'URL des requêtes) n'apparaisse jamais dans les journaux. Les apps n'embarquent qu'un jeton **public** (`pk.*`) pour afficher les tuiles de la carte.

## Sécurité

- Le contour des zones n'est lisible que du personnel (`/zones/manage`) : la liste publique ne l'expose pas.
- Écriture réservée aux administrateurs ; une zone n'est jamais supprimée (des commandes y font référence).
- Les frais, la zone et le minimum sont toujours recalculés côté serveur : rien de ce qu'envoie le client n'est cru.
- `/check`, `/geocode`, `/reverse-geocode` et l'aperçu des zones sont limités en fréquence par utilisateur (coût Mapbox, cartographie de la couverture).
- Une zone historique illisible est ignorée sans empêcher la vérification des autres.

## Limites connues

- Pas de trous ni de multipolygones dans une zone.
- Le cache de géocodage est local à chaque instance de l'API.
- Pas de frais au kilomètre ni de créneaux de livraison programmée.
- Le suivi du livreur, l'attribution et la preuve de remise sont prévus aux phases suivantes du plan (`docs/superpowers/specs/2026-10-01-livraison-interne-plan-sprint.md` à la racine du dépôt).
