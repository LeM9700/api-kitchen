# Delivery

Gestion des zones de livraison géographiques par tenant : CRUD admin, liste publique et vérification de couverture par ray-casting.

> **Plan 01 (fondations et durcissement)** — ce document reflète l'état du module après les 5 tâches du
> plan `delivery-network/plan-01-delivery-foundations.md`. Les zones et `POST /check` couvrent toujours le
> mode de livraison **historique/interne** du tenant. Le réseau de livreurs indépendants (checkout dédié,
> affectation, suivi, tarification kilométrique) est posé en fondations logicielles désactivées — voir
> [Réseau indépendant désactivé](#réseau-indépendant-désactivé) — mais aucune route ni logique métier de ce
> réseau n'existe encore.

## Endpoints

| Méthode | Path | Auth | Rôles |
|---------|------|------|-------|
| GET | `/api/v1/delivery/zones` | Public | header `X-Tenant-Slug` obligatoire (400 si absent, 404 si tenant inconnu) |
| POST | `/api/v1/delivery/zones` | Bearer JWT | admin |
| PUT | `/api/v1/delivery/zones/{zone_id}` | Bearer JWT | admin |
| DELETE | `/api/v1/delivery/zones/{zone_id}` | Bearer JWT | admin — soft-delete idempotent (`is_active=false`), 404 si l'id n'a jamais existé |
| POST | `/api/v1/delivery/check` | Bearer JWT | tous |

Toutes les routes sont rate-limitées (`60/minute` par IP, sauf `POST /check` à `30/minute` — voir
[Sécurité implémentée](#sécurité-implémentée)).

## Modèles de données

**`delivery_zones`** : `id`, `name`, `polygon` (JSON GeoJSON, sous-ensemble `Polygon` strict validé — voir
ci-dessous), `fee`, `min_order_amount`, `estimated_minutes`, `is_active`.

> `fee` est le tarif **fixe du mode de livraison historique/interne** (livreur du restaurant). Le réseau de
> livreurs indépendants (Plan 05, à venir) introduira son propre moteur de tarification kilométrique dans
> le contexte du checkout réseau — il ne réutilisera pas `delivery_zones.fee` tel quel.

## Comportements métier

**Liste des zones (`GET /zones`)** : retourne toutes les zones actives triées par nom, pour le tenant
résolu depuis `X-Tenant-Slug` (voir [Sécurité implémentée](#sécurité-implémentée) pour la résolution du
tenant).

**Vérification adresse (`POST /check`)** : accepte `lat`/`lng` bornés (`-90 ≤ lat ≤ 90`,
`-180 ≤ lng ≤ 180`, rejetés en 422 sinon), teste l'appartenance à chaque zone **active** par ray-casting
sur le polygone GeoJSON. Si plusieurs zones actives se chevauchent sur le point testé, la zone retenue est
**déterministe** : celle dont l'`id` est le plus faible parmi les zones actives correspondantes (jamais une
zone inactive, quel que soit son id) — la sélection ne dépend plus de l'ordre implicite renvoyé par
PostgreSQL. Retourne `{zone_id, name, fee, estimated_minutes}` (schéma `AddressCheckOut`, forme JSON
inchangée depuis avant le Plan 01) de la zone gagnante. Erreur `DELIVERY_ZONE_UNREACHABLE` 422 si aucune
zone ne couvre le point (pas de 404 — comportement inchangé, déjà consommé ainsi par `app-client`).

**CRUD zones** : création et mise à jour full-replace (`PUT`) via des fonctions de service dédiées
(`service.create_zone`/`update_zone`, testables sans HTTP). `PUT`/`DELETE` sur un `zone_id` absent
retournent `DELIVERY_ZONE_NOT_FOUND` 404 (jamais un 500). `DELETE` est un soft-delete
(`is_active = False`, jamais de suppression physique), idempotent sur une zone déjà inactive.

## Sécurité implémentée

- **Validation GeoJSON stricte** (`app/modules/delivery/common/geo.py`) : à la création/mise à jour d'une
  zone, le `polygon` doit être un `Polygon` GeoJSON à un seul anneau extérieur (pas de trou, pas de
  multipolygone), entre 4 et 501 positions, premier point identique au dernier, positions `[lng, lat]`
  finies et dans les bornes valides, au moins 3 sommets distincts, aire non nulle, sans auto-intersection.
  Toute violation lève `INVALID_DELIVERY_POLYGON` 422 — jamais une exception arithmétique ou un 500.
- **Tenant obligatoire, jamais de fallback silencieux** : `GET /delivery/zones` exige l'en-tête
  `X-Tenant-Slug` (`TENANT_REQUIRED` 400 si absent) et vérifie son existence en base publique
  (`TENANT_NOT_FOUND` 404 si inconnu) — l'ancien fallback implicite sur un tenant `"default"` inexistant a
  été supprimé.
- **Aucun 500 sur un identifiant absent** : `PUT`/`DELETE /delivery/zones/{id}` sur une zone inexistante
  retournent `DELIVERY_ZONE_NOT_FOUND` 404, jamais une `AttributeError` non gérée.
- **Sélection de zone déterministe** : voir [Comportements métier](#comportements-métier) — le
  chevauchement de zones actives ne peut plus produire un résultat qui varie selon l'ordre physique de la
  table.
- **Rate-limit** : `60/minute` par IP sur les routes zones (cohérent avec le reste de l'API, ex.
  `catalog`), `30/minute` sur `POST /check` — plus strict pour limiter le reverse-engineering des zones par
  appels répétés, tout en restant large pour un usage normal de checkout.
- **Bornes de coordonnées** : `AddressCheckRequest.lat`/`.lng` bornés Pydantic (`ge=-90/le=90`,
  `ge=-180/le=180`) ; `DeliveryZoneCreate.fee`/`.min_order_amount` non négatifs, `.estimated_minutes`
  strictement positif.
- Ray-casting ne gère toujours pas les trous (polygones avec exclusions) ni les multipolygones — c'est un
  choix délibéré du sous-ensemble v1 (rejetés explicitement en entrée par la validation ci-dessus, pas une
  lacune).

---

## Axes d'amélioration

Résolus par le Plan 01 (retirés de cette liste) : validation GeoJSON stricte, soft-delete de zone,
rate-limit sur `/check`, bornes de coordonnées, sélection déterministe en cas de zones imbriquées, tenant
obligatoire sans fallback, 404 au lieu de 500 sur id absent. Ce qui reste réellement hors scope :

### Logique métier
- **Calcul de frais dynamique** : les frais de livraison (`fee`) restent fixes par zone pour le mode
  historique/interne. Pas de logique de frais progressifs (ex. tarif au km depuis l'adresse du
  restaurant) — le moteur de tarification du réseau indépendant est explicitement reporté au Plan 05.
- **Choix de la zone la moins chère** : en cas de chevauchement, la zone retenue est déterministe (id le
  plus faible, voir ci-dessus) mais ce n'est pas nécessairement la moins chère ou la plus rapide — aucun
  besoin produit ne justifie encore une colonne `priority` ou un calcul de coût comparatif.
- **Géocodage** : `POST /check` exige `lat`/`lng` — le client doit géocoder l'adresse texte lui-même.
  Aucune intégration Google Maps / Nominatim côté API.
- **Délai estimé dynamique** : `estimated_minutes` est statique. Pas de lien avec `TenantConfig.prep_time`
  ni avec la charge en cours.

### Sécurité & contre-intrusion
- **DoS par polygone complexe** : la validation stricte plafonne désormais un polygone à 501 positions,
  ce qui borne le coût du ray-casting par zone. Reste hors scope : une limite explicite sur le **nombre de
  zones actives par tenant** (un tenant pourrait toujours créer un grand nombre de zones valides
  individuellement).
- **Cross-tenant zone check** : `POST /check` est authentifié mais le tenant est résolu depuis le JWT —
  vérifier que le middleware enforce bien le `search_path` tenant avant le ray-casting (non re-audité dans
  ce plan, hors de son périmètre déclaré).

### Accessibilité API
- Exposer les frais de livraison et le temps estimé dans `GET /delivery/zones` pour affichage côté client
  sans appel à `/check`.
- Calculer les frais de livraison côté serveur lors de la création de commande (`POST /orders`) et rejeter
  si `delivery_fee` client diverge.

---

## Réseau indépendant désactivé

Le Plan 01 pose les conventions internes partagées des Plans 02-05 (réseau de livreurs indépendants —
checkout dédié, diffusion des demandes de livraison, affectation, suivi), **sans monter aucune route ni
logique métier de ce réseau**. Concrètement, à ce stade :

- **Flag `delivery_network_enabled`** (`app/core/config/settings.py`, aussi dans `.env.example`) : `False`
  par défaut. Aucun code de ce plan ne le lit encore pour activer un comportement — c'est une réservation
  de nom pour les plans suivants.
- **`app/modules/delivery/common/`** — sous-packages posés en fondations, non consommés par le code
  existant (`models.py`/`service.py`/`router.py` du mode historique) :
  - `enums.py` : `DeliveryHandoffMode`, `DeliveryCheckoutStatus`, `DeliveryRequestStatus`,
    `DeliveryVehicleType`, `DeliveryAuditActor` — des `str, Enum` Python (délibérément **pas** un ENUM
    Postgres natif, pour éviter une migration `ALTER TYPE` à chaque nouvelle valeur pendant l'itération
    rapide attendue sur les plans 02-05).
  - `audit.py` : `record_delivery_audit_event(...)`, un helper qui n'accepte que des identifiants, une
    transition et des métadonnées explicitement nettoyées — il **refuse** toute clé ressemblant à une
    adresse, des coordonnées GPS, un token, du contenu de document d'identité ou un secret Stripe Connect
    (`ForbiddenAuditMetadataError`), en anticipation des données sensibles que manipulera le réseau
    (documents de livreurs, géolocalisation en temps réel, comptes Stripe Connect).
  - `errors.py` : erreurs internes du module (`AppError` sous-classées) — certaines déjà utilisées par le
    mode historique (`InvalidDeliveryPolygonError`, `TenantRequiredError`, `TenantNotFoundError`,
    `DeliveryZoneNotFoundError`), d'autres réservées au futur réseau (`ForbiddenAuditMetadataError`).
  - `geo.py` : validation GeoJSON stricte (voir [Sécurité implémentée](#sécurité-implémentée)) —
    partagée par construction, pas spécifique au réseau indépendant, mais posée dans ce plan.

Aucune table, aucune route, aucun flux de commande propre au réseau indépendant n'existe dans le code à ce
stade — uniquement ces conventions internes, prêtes à être consommées par les Plans 02-05.

---

## Ce qui manque pour les interfaces

### Interface client
- **Carte interactive** : afficher les zones de livraison sur une carte (Leaflet/Mapbox) depuis `GET /delivery/zones`.
- **Saisie d'adresse avec géocodage** : intégrer Google Maps Places / Nominatim pour convertir l'adresse texte en `lat`/`lng` avant d'appeler `POST /check`.
- **Frais de livraison en temps réel** : afficher les frais et le délai estimé dès que l'adresse est saisie (avant de valider la commande).
- **Zone non couverte** : message explicite si l'adresse est hors zone, avec suggestion de récupération en magasin (click & collect — non implémenté).

### Interface staff
- **Vue carte des livraisons actives** : afficher la position des commandes `out_for_delivery` sur la carte (nécessite lat/lng dans `orders`).

### Interface admin (tenant)
- **Éditeur de zones** : carte interactive (Leaflet) permettant de dessiner/modifier les polygones de livraison.
- **CRUD zones** : formulaire pour créer/modifier/désactiver les zones avec prévisualisation sur carte.
- **Test de couverture** : saisir une adresse et voir quelle zone la couvre (ou "hors zone").

### Super-admin
- Vue de couverture géographique cross-tenant (carte globale de tous les tenants actifs — usage analytique).
