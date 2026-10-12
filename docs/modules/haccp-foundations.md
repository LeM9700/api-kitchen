# Fondations HACCP strictes — phase 1

Cette phase prépare les données et les contrats. Elle n'active aucun blocage de commande,
n'ajoute aucune route, ni écran, ni tâche de notification. `strict_enabled` reste faux.
Les services HACCP et stock existants continuent d'utiliser leurs tables historiques.

## Données et appartenance à un établissement

Les 17 tables nouvelles vivent dans chaque schéma `tenant_{slug}` :

| Ensemble | Tables / responsabilité |
| --- | --- |
| Configuration | `establishment_operations`, `establishment_business_hours`, `establishment_closures` : activation future, arrêt commercial, calendrier local. |
| Affectations | `haccp_equipment_scopes`, `haccp_cleaning_scopes`, `haccp_staff_scopes` : équipement/tâche/utilisateur rattaché au site. |
| Contrôle | `haccp_daily_sessions`, `haccp_requirements`, `haccp_requirement_batches`, `haccp_evidence` : session quotidienne, exigences versionnées, lots attendus et preuves. |
| Suivi | `haccp_regularizations`, `haccp_audit_events`, `haccp_reminder_deliveries` : régularisation présente, audit et livraison des rappels. |
| Stock | `establishment_stock`, `stock_batch_positions`, `stock_batch_allocations`, `establishment_stock_movements` : solde, reste de lot, consommation par commande et journal. |

Les clés étrangères composites empêchent de rattacher une preuve, un équipement, une tâche,
un lot ou une allocation à une session/commande d'un autre établissement. La migration ajoute
deux contraintes uniques sur `(orders.id, establishment_id)` et
`(ingredient_batches.id, ingredient_id)` pour permettre ces références.

Le référentiel ingrédients et les recettes restent communs au tenant. Un lot physique est
affecté à un seul établissement ; les transferts/splits entre sites ne font pas partie du sprint.
Un solde est unique par site/ingrédient. `remaining_quantity=NULL` signifie **inconnu**, jamais
disponible ni nul. Quantités négatives, allocations dupliquées et restitution supérieure à la
consommation sont refusées par PostgreSQL. La prévention de la surconsommation cumulée relève
des transactions/verrous de phase 3 : les seules contraintes ne suffisent pas.

## Contrats à respecter dans les phases suivantes

- Une session par site, date métier locale et type `opening`/`closing`. Horodatages UTC avec fuseau,
  fuseau du site figé sur la session. Aucun deuxième contrôle midi/soir.
- États : `in_progress` → `complete` seulement après conformité de toutes les exigences ;
  `in_progress` → `regularized` réservé à une fermeture oubliée, avec motif et actions présentes.
  `regularized` conserve la distinction avec un contrôle effectué à l'heure attendue.
- La fermeture commerciale reste indépendante. Une fermeture HACCP complète termine la journée.
  Une réouverture exceptionnelle le même jour conserve l'identité de la session d'ouverture,
  incrémente sa révision, la replace en `in_progress`, efface sa validation courante et crée
  de nouvelles exigences. Le contrôle complet est requis avant la reprise des commandes.
  La fermeture devra à son tour être revalidée sur une nouvelle révision. Les événements d'audit
  conservent les validations antérieures ; ces transitions seront transactionnelles en phase 2.
- Une exigence appartient à une révision précise et fige les seuils/configuration nécessaires.
  Ses lots ont une référence relationnelle, leur révision et leur DLC effective. Les preuves
  anciennes sont conservées, mais ne valident pas une nouvelle version de la checklist.
- Changement de lot, d'équipement, de tâche ou de configuration : relecture serveur,
  contrôle de `expected_revision`, nouvelle version des exigences et audit. Une suppression
  de configuration ne peut effacer silencieusement une non-conformité.
- Une confirmation accepte uniquement site, session, révision, exigences et clé d'idempotence.
  `force`, auteur, heure et conformité fournis par le client sont refusés. L'auteur vient
  de l'authentification ; l'heure et la conformité viennent du serveur.
- Pour une confirmation groupée, dériver une clé stable par exigence depuis la clé de requête
  (hash borné à 128 caractères), puis insérer toutes les preuves dans une seule transaction.
  Une clé rejouée avec un autre contenu devra être refusée, pas interprétée comme un nouvel accord.
- La régularisation désigne une fermeture, un motif et les actions réalisées maintenant.
  Pas de date passée envoyée par l'interface ; ne jamais fabriquer les mesures oubliées.
- L'affectation staff n'est pas une permission. La phase 2 combinera les permissions existantes,
  le tenant courant et le périmètre établissement. Aucune habilitation n'est attribuée par migration.
- Les anomalies strictes restent liées à l'exigence, sa preuve non conforme et l'audit du site.
  Les non-conformités historiques du module HACCP restent dans leur historique ; leur rattachement
  éventuel demandera une décision explicite, pas une duplication automatique.
- Le journal des rappels déduplique site/date/révision d'horaires/échéance/destinataire/canal.
  La phase 5 devra acquérir un bail, reprendre les erreurs et invalider les anciennes échéances.
  Une unicité en base ne garantit pas à elle seule l'absence de doubles push chez le fournisseur.

`foundation_contracts.py` définit les motifs stables `HACCP_*`. La phase 2 produira la décision
métier ; la phase 4 l'appliquera à toutes les nouvelles commandes. Ces contrats ne sont pas
encore une API publique. Les contraintes garantissent la structure, pas les permissions,
l'immutabilité de l'audit ni la conformité complète d'une session : ces garanties applicatives
seront implémentées et testées avant activation.

## Reprise sans inventer de preuves

La migration figée n'importe aucun modèle applicatif. Pour un tenant possédant exactement un
établissement, elle affecte les équipements/tâches/lots et copie un instantané du stock global,
des horaires non ambigus et des fermetures. Les soldes restent non réconciliés et les restes
de lots inconnus. Aucun contrôle quotidien strict, aucune allocation passée ni preuve n'est créé.

Pour zéro ou plusieurs établissements, seule la configuration inactive par site est créée.
La répartition physique sera explicite : aucun stock n'est copié dans chaque site.
Les créneaux dupliqués ne sont pas choisis arbitrairement. Les tâches hebdomadaires/mensuelles
exigent une date d'ancrage ; `per_service` demande une conversion explicite au modèle quotidien.
La phase 2 devra appliquer les règles d'échéance à partir de ces choix.

Le diagnostic en lecture seule énumère les affectations manquantes, restes inconnus, stock non
réconcilié, DLC absentes, équipements sans seuils et calendriers ambigus. Il indique toujours
`activation_available=false`. Il constitue un état de préparation global au tenant, pas un statut
d'ouverture par établissement ni une autorisation sanitaire.

Pendant la coexistence, `Ingredient.current_qty` reste utilisé par les anciens services.
Les instantanés de phase 1 deviennent donc périmés avec l'activité. Ils ne devront jamais être
utilisés pour autoriser une vente avant réconciliation au basculement et raccordement complet
des écritures stock. Même précaution pour les horaires, fermetures et affectations modifiés
après migration. Les nouveaux établissements créés après provisionnement seront configurés
par le workflow des phases suivantes ; l'absence de configuration devra alors bloquer l'activation.

Déploiement et retour applicatif : [procédure phase 1](../operations/haccp-phase1-production.md).
