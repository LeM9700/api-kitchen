# Politique de confidentialité — api-pizza

> ⚠️ **Ce document est une base technique, pas un document juridique validé.**
> Il décrit ce que le code fait réellement (données collectées, rétention technique,
> endpoints d'exercice des droits) sur la base d'une lecture du code au 2026-07-18.
> **Requiert une validation par un juriste/DPO avant toute publication officielle**
> ou mise en production traitant des données de clients réels — notamment pour les
> bases légales de traitement, les durées de conservation légales exactes, et les
> mentions obligatoires (responsable de traitement, contact DPO, autorité de contrôle).

## 1. Données collectées

| Catégorie | Champs | Module | Base légale probable |
|---|---|---|---|
| Identité | email, nom complet, téléphone (optionnel) | `customer`, `auth` | Exécution du contrat |
| Authentification | hash de mot de passe (bcrypt), secret MFA TOTP (si activé) | `auth` | Exécution du contrat |
| Commandes | historique, adresses de livraison, montants | `orders` | Exécution du contrat |
| Paiement | *aucune donnée de carte stockée* — délégué à Stripe (PCI-DSS) | `payments` | Exécution du contrat |
| Préférences alimentaires | allergènes/régimes consultés (pas stockés par utilisateur, catalogue uniquement) | `catalog` | — (donnée produit, pas personnelle) |
| Connexion | adresse IP, user-agent, horodatage de connexion | MongoDB (`login_events_*`), rétention 90 jours | Intérêt légitime (sécurité) |
| Fidélité | solde de points, historique de transactions points | `loyalty` | Exécution du contrat |
| Notifications | device token push (APNs/FCM) | `notifications` | Consentement (opt-in device) |

## 2. Sous-traitants / tiers

- **Stripe** (paiements, Stripe Connect) — données de paiement, jamais transmises en clair à l'API.
- **Cloudinary** (hébergement images produit) — pas de données personnelles client.
- **Apple (APNs) / Google (FCM)** — device tokens pour les notifications push.
- **Sentry** (monitoring d'erreurs, si `SENTRY_DSN` configuré) — peut capturer des fragments de
  requêtes en cas d'erreur (headers, paramètres) ; **à vérifier que les payloads sensibles
  (mots de passe, tokens) sont scrubés par la configuration Sentry par défaut avant tout envoi
  de données de production.**
- **MongoDB** (managé, hors du périmètre de ce dépôt) — événements de connexion, statistiques agrégées.

## 3. Droits des utilisateurs — endpoints d'exercice

| Droit | Endpoint | Notes |
|---|---|---|
| Accès | `GET /api/v1/customer/me` | Profil courant |
| Rectification | `PATCH /api/v1/customer/me` | Nom, téléphone |
| **Portabilité / export** | `GET /api/v1/customer/me/export` | Profil + jusqu'à 100 commandes récentes (JSON). `orders_truncated=true` si l'historique dépasse ce seuil — voir limite connue ci-dessous. |
| Effacement | `DELETE /api/v1/customer/me` | Soft delete (`is_active=False`) + révocation des sessions — voir limite connue ci-dessous |

## 4. Limites connues (à traiter avant une validation juridique complète)

- **Suppression = soft delete, pas d'effacement physique.** `DELETE /customer/me` désactive le
  compte (`is_active=False`) et révoque les sessions, mais **les lignes ne sont pas supprimées de
  la base**. Pour un droit à l'effacement complet (Art. 17 RGPD), une purge physique différée
  (ex: job planifié après un délai de rétention légal) reste à spécifier et implémenter.
- **Export limité à 100 commandes les plus récentes** (`_EXPORT_MAX_ORDERS` dans
  `app/modules/customer/service.py`). Un client avec un historique plus long reçoit
  `orders_truncated=true` mais pas l'intégralité — suffisant pour un usage courant, à étendre
  (pagination complète ou export asynchrone) si le volume de commandes par client le justifie.
- **L'export ne couvre pas** : solde/historique de points fidélité, device tokens de notification,
  événements de connexion (MongoDB). Périmètre volontairement restreint au profil + commandes pour
  cette première version — à étendre si un usage réel le demande.
- **Durées de rétention non formalisées** au-delà des 90 jours des `login_events_*` (codé en dur).
  Aucune politique de purge automatique n'existe pour les comptes désactivés ou les commandes anciennes.

## 5. Sécurité des données (résumé technique)

Voir `audit/audit-security-report.md` pour le détail complet. Points clés : isolation multi-tenant
par schéma PostgreSQL dédié, mots de passe hashés (bcrypt), JWT avec révocation, chiffrement en
transit (TLS géré par Railway), pas de secret loggué (`_safe_stripe_message()` redacte les clés Stripe).
