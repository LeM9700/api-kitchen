# HACCP strict — phase 2

La phase 2 ajoute un workflow serveur quotidien par établissement. Elle ne branche pas encore
ce statut sur l'acceptation des commandes et n'envoie pas les rappels H−60/H−45/H−30/H−15.
Ces raccordements restent dans les phases 4 et 5. `strict_enabled` reste un paramètre de
configuration réservé à une activation ultérieure ; les routes de phase 2 retournent
`ordering_enforced=false`.

## Routes

Sous `/api/v1/haccp/strict/establishments/{establishment_id}` :

- `GET /status` : décision et tous les motifs bloquants sans créer de session.
- `POST /sessions` avec `opening` ou `closing` : crée une session quotidienne et fige la
  checklist. Une relance rafraîchit la révision si la configuration ou un lot a changé.
- `GET /sessions/{id}` : lecture d'une session et de ses preuves.
- `POST /sessions/{id}/evidence` : saisie groupée des températures, confirmations d'hygiène et
  confirmations DLC, avec révision attendue et idempotence.
- `POST /sessions/{id}/complete` : valide uniquement si chaque exigence est conforme.
- `POST /regularizations/prepare`, puis `POST /sessions/{id}/regularize` : expose une fermeture
  oubliée et la régularise avec motif, actions, auteur et heure réels.
- `POST /sessions/{id}/reopen` : réouvre une journée clôturée exceptionnelle ; ouverture et
  fermeture repassent en `in_progress`, leurs révisions sont incrémentées et les preuves
  précédentes restent dans l'audit.
- `PUT`/`DELETE /staff/{user_id}` : l'admin gère l'affectation HACCP d'un staff actif.

La permission `haccp:read` lit, `haccp:write` saisit, `haccp:validate` finalise/régularise et
`haccp:manage` affecte le personnel. Le rôle admin conserve ses droits ; le staff doit posséder
la permission explicite et une ligne `haccp_staff_scopes` pour le site. Le client, le staff non
habilité et les tokens d'impersonation sont refusés. Chaque requête vérifie l'établissement dans
les clés composites : une session du site A ne peut être lue ni écrite depuis le site B.

## Construction de la checklist

La checklist est préremplie depuis les équipements actifs affectés au site, les tâches d'hygiène
du type de session et tous les ingrédients présents dans `establishment_stock`. Elle conserve
un snapshot des seuils de température, de la tâche, de la fréquence, du rôle requis et des lots.
Les DLC primaire, secondaire et tertiaire sont évaluées par la date effective la plus proche.
Une expiration à l'instant du contrôle bloque ; l'heure exacte et le fuseau du site sont utilisés.

Les situations suivantes apparaissent simultanément dans le diagnostic : équipement/tâche non
affecté, température sans limites, fréquence `per_service` incompatible avec le modèle quotidien,
fréquence hebdomadaire/mensuelle sans ancrage, solde non réconcilié, reste de lot inconnu, DLC
absente, lot expiré non écarté et preuve manquante/non conforme. Une case humaine ne peut pas
transformer un lot expiré, un DLC manquant ou une quantité inconnue en stock utilisable. Un lot
expiré écarté avec preuve et un lot de remplacement valable, traçable et suffisant permettent
la validation DLC de l'ingrédient.

Un stock physique nul reste visible dans la checklist et peut être confirmé. Il ne fabrique pas
de lot, ne déplace pas le stock global et ne modifie aucun produit : la désactivation locale des
produits liés arrive en phase 3.

## Temps, fermeture et régularisation

La date métier est calculée dans le fuseau de l'établissement. L'ouverture est contrôlable dans
l'heure avant le premier créneau et jusqu'au service ; une fermeture peut terminer le dernier
service ou, juste après minuit, la journée précédente. La régularisation est une action effectuée
maintenant : aucune date d'auteur, mesure ou conformité n'est acceptée depuis le client.

Une journée sans créneau actif ou fermée exceptionnellement n'impose pas un contrôle artificiel.
Les sessions antérieures à `activated_at` ne créent pas de dette de fermeture. Après activation,
une ouverture complète sans fermeture complète ou régularisée bloque la prochaine ouverture et
est listée comme régularisation disponible. La régularisation ne remplit pas de mesures oubliées
et laisse l'oubli dans l'audit ; elle ne suffit pas à ouvrir la journée courante.

Une modification d'un lot, d'une quantité, d'un seuil, d'une tâche ou du fuseau rend le snapshot
obsolète. Le serveur incrémente la révision, garde les anciennes preuves comme historique et
demande une nouvelle confirmation. Une non-conformité saisie puis corrigée exige une action
corrective ; retirer un équipement après un échec conserve une anomalie non résolue dans le statut.

## Idempotence et concurrence

Les écritures verrouillent d'abord l'établissement puis la session. Chaque lot de preuves est
lié à une clé de requête et chaque preuve à une clé dérivée de l'exigence. Une répétition avec le
même contenu renvoie le résultat conservé ; une réutilisation avec un autre contenu est un conflit.
Les contraintes d'unicité, les clés composites et la traduction des conflits PostgreSQL en
`HACCP_RETRY_REQUIRED` protègent les doubles clics et synchronisations concurrentes. Une erreur
de notification ou de réseau ne peut donc pas fabriquer une validation locale.

## Limites et phase suivante

Les tables historiques HACCP restent disponibles pour compatibilité, mais leur statut
`incomplete_validated` ne satisfait plus le gate historique et `force=true` est refusé. Les
produits, commandes, paiements et routes manuelles ne consultent pas encore la décision stricte.
Il faut déployer la phase 3 pour raccorder le stock local et les produits, puis la phase 4 pour
refuser toute nouvelle commande et afficher le message client avant paiement. Jusqu'à cette
intégration, le contrôle strict est consultable et enregistrable mais ne doit pas être présenté
comme une protection de commande active.
