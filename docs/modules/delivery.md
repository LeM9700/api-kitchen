# Delivery

Gestion des zones de livraison géographiques par tenant : CRUD admin, liste publique et vérification de couverture par ray-casting.

## Endpoints

| Méthode | Path | Auth | Rôles |
|---------|------|------|-------|
| GET | `/api/v1/delivery/zones` | Public | header `X-Tenant-Slug` |
| POST | `/api/v1/delivery/zones` | Bearer JWT | admin |
| PUT | `/api/v1/delivery/zones/{zone_id}` | Bearer JWT | admin |
| POST | `/api/v1/delivery/check` | Bearer JWT | tous |

## Modèles de données

**`delivery_zones`** : `id`, `name`, `polygon` (JSON GeoJSON FeatureCollection), `fee`, `min_order_amount`, `estimated_minutes`, `is_active`.

## Comportements métier

**Liste des zones (`GET /zones`)** : retourne toutes les zones actives triées par nom.

**Vérification adresse (`POST /check`)** : accepte `lat`/`lng`, teste l'appartenance à chaque zone active par algorithme ray-casting sur le polygone GeoJSON. Retourne `{zone_id, name, fee, estimated_minutes}` de la première zone correspondante. Erreur 404 si aucune zone ne couvre le point.

**CRUD zones** : création et mise à jour full-replace (PUT). Pas de soft-delete exposé.

## Sécurité implémentée

- [⚠️ PROD] Pas de validation stricte du GeoJSON en entrée — un polygone malformé sera persisté sans erreur.
- [⚠️ PROD] Ray-casting ne gère pas les trous (polygones avec exclusions) ni les multipolygones.

---

## Axes d'amélioration

### Logique métier
- **Validation GeoJSON** : à la création/mise à jour d'une zone, valider que le GeoJSON est un polygone fermé (premier et dernier point identiques), qu'il a au moins 3 points et qu'il n'est pas self-intersecting.
- **Calcul de frais dynamique** : les frais de livraison (`fee`) sont fixes par zone. Pas de logique de frais progressifs (ex. tarif au km depuis l'adresse du restaurant).
- **Zones imbriquées** : si une adresse est dans deux zones, seule la première zone active est retournée. Pas de logique de priorité ou de sélection de la zone la moins chère.
- **Géocodage** : `POST /check` exige `lat`/`lng` — le client doit géocoder l'adresse texte lui-même. Aucune intégration Google Maps / Nominatim côté API.
- **Délai estimé dynamique** : `estimated_minutes` est statique. Pas de lien avec `TenantConfig.prep_time` ni avec la charge en cours.
- **Suppression de zone** : pas de soft-delete exposé — une zone supprimée (DELETE non implémenté) est purement destructive.

### Sécurité & contre-intrusion
- **DoS par polygone complexe** : un admin peut créer un polygone avec des milliers de points — le ray-casting s'exécute pour chaque zone sur chaque `POST /check`. Limiter le nombre de points par polygone (ex. 500 max) et le nombre de zones actives par tenant.
- **SSRF / GeoJSON injection** : si le GeoJSON est un jour passé à une bibliothèque externe (tuiles, geocodage), des valeurs inattendues (`Infinity`, `-Infinity`, `NaN`) pourraient causer des erreurs. Valider les coordonnées (`-90 ≤ lat ≤ 90`, `-180 ≤ lng ≤ 180`).
- **Cross-tenant zone check** : `POST /check` est authentifié mais le tenant est résolu depuis le JWT — vérifier que le middleware enforce bien le `search_path` tenant avant le ray-casting.
- **Rate-limit absent** : `POST /check` n'a pas de rate-limit — un client peut appeler l'endpoint en boucle pour cartographier les zones de livraison du tenant (reverse-engineering business).

### Accessibilité API
- Exposer les frais de livraison et le temps estimé dans `GET /delivery/zones` pour affichage côté client sans appel à `/check`.
- Ajouter `DELETE /delivery/zones/{zone_id}` (soft-delete `is_active=False`) pour cohérence avec les autres modules.
- Calculer les frais de livraison côté serveur lors de la création de commande (`POST /orders`) et rejeter si `delivery_fee` client diverge.

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
