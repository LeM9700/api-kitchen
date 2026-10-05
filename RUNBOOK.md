# Runbook — api-pizza

Procédures opérationnelles pour les incidents courants. Cible : déploiement Railway
(`railpack.json`), deux services (web API + worker ARQ) partageant Postgres/MongoDB/Redis.

---

## 1. Rollback d'un déploiement

**Symptôme** : erreurs 500 en masse après un déploiement, régression fonctionnelle détectée.

1. Dans le dashboard Railway du service concerné (web ou worker) : **Deployments** → sélectionner
   le déploiement précédent stable → **Redeploy**. Railway garde l'historique des builds ; c'est la
   voie la plus rapide (pas besoin de revert Git).
2. Si le rollback doit repartir d'un commit précis : `git revert <commit>` (jamais `git reset --hard`
   sur une branche partagée) puis laisser le déploiement automatique (ou manuel) reprendre.
3. Vérifier `/health` puis `/health/ready` sur la nouvelle instance avant de considérer l'incident clos.

**Cas particulier — rollback de migration Alembic** : si le déploiement problématique a appliqué une
migration incompatible avec le code précédent :

```bash
uv run alembic downgrade -1
```

⚠️ Toutes les migrations de ce projet itèrent sur `public.tenants` et appliquent le DDL par
schéma tenant (voir `alembic/versions/00XX_*.py` — pattern `_get_tenant_slugs` + boucle). Un
`downgrade` doit être testé en staging avant d'être exécuté en production : certaines migrations
suppriment des colonnes/tables (perte de données si la migration avait déjà des données réelles).

---

## 2. Rejouer un webhook Stripe

**Symptôme** : un paiement Stripe a réussi côté Stripe mais la commande n'a pas été confirmée côté
API (webhook manqué — Stripe down, erreur 500 transitoire, etc.).

1. Dashboard Stripe → **Developers → Webhooks** → sélectionner l'endpoint → onglet **Events** →
   retrouver l'event concerné (filtrer par `payment_intent.succeeded` / ID de PaymentIntent) →
   **Resend**.
2. En local/dev, avec la Stripe CLI :
   ```bash
   stripe events resend evt_xxx --webhook-endpoint we_xxx
   ```
3. **Idempotency** : le webhook est protégé par la table `processed_webhook_events`
   (contrainte UNIQUE sur `stripe_event_id`, voir `app/modules/payments/service.py::handle_webhook`).
   Un rejeu du **même** event est donc un no-op silencieux si déjà traité — sûr à rejouer sans
   double-traitement. Si l'event n'a jamais été traité (échec avant l'insertion de la marque), le
   rejeu déclenche le traitement normal.
4. Si le rejeu ne suffit pas (ex. commande déjà annulée entre-temps) : vérifier manuellement l'état
   dans `payments`/`orders` et corriger via l'admin (remboursement, statut) plutôt que de forcer un
   rejeu qui ne changera rien à un état déjà divergent.
5. **Deux origines, deux secrets** : l'endpoint `/api/v1/payments/webhook` accepte à la fois les
   events du compte plateforme (`STRIPE_WEBHOOK_SECRET`) et les events des comptes connectés Stripe
   Connect / direct charges (`STRIPE_WEBHOOK_CONNECT_SECRET`) — voir
   `service.verify_stripe_webhook_event`. Si un 400 `Invalid Stripe signature` apparaît uniquement
   pour les events Connect, vérifier que `STRIPE_WEBHOOK_CONNECT_SECRET` est bien défini sur Railway
   et correspond au signing secret de l'endpoint webhook Connect du Dashboard Stripe (distinct de
   celui de l'endpoint plateforme, même si les deux endpoints pointent vers la même URL).
6. **Empreinte bancaire (livraison payée à la remise)** : l'endpoint doit aussi recevoir
   `payment_intent.amount_capturable_updated`, `checkout.session.completed` et
   `checkout.session.expired` (sur l'endpoint plateforme **et** Connect). Sans le premier, une empreinte
   posée par lien de paiement n'est finalisée que lorsque l'app appelle `POST /payments/confirm`.
   Détail et cycle de vie : `docs/modules/payments.md`, section « Garantie de paiement ».
   - **Empreinte restée `authorized` alors que la commande est annulée** (l'annulation Stripe a échoué à ce
     moment-là) : `POST /api/v1/payments/{order_id}/guarantee/release`, ou annuler le PaymentIntent dans le
     Dashboard Stripe (le webhook `payment_intent.canceled` la marque alors `released`). Sans action, la banque
     la libère d'elle-même au bout d'environ 7 jours.
   - **Débiter un client** (échec de livraison de son fait) : administrateur uniquement,
     `POST /api/v1/payments/{order_id}/guarantee/capture` avec un motif ; jamais depuis le Dashboard Stripe si
     possible (l'API trace le motif, l'auteur et le montant).

---

## 2bis. Livraison : livreur ou livraison bloques

- **Un livreur ne peut pas recevoir de commande** : il doit avoir **pointe** (`DRIVER_NOT_CLOCKED_IN`). Corriger le pointage
  dans l'app (RH, correction admin) puis reattribuer.
- **Une commande ne part pas** (409 `DRIVER_REQUIRED`) : le dispatch est actif, assigner un livreur depuis l'ecran Dispatch.
  Pour rouvrir le depart libre en urgence : couper `driver_dispatch_enabled` (page Dispatch, administrateur, ou
  `PUT /api/v1/delivery/settings`). Aucune donnee n'est perdue.
- **Livreur parti avec une commande puis injoignable** : le comptoir ne peut plus reattribuer (`DELIVERY_ALREADY_STARTED`).
  Declarer l'echec de livraison (motif obligatoire) ou annuler la commande : la livraison se cloture toute seule.
- **Livraison restee `out_for_delivery` apres une panne** : l'etat de la commande fait foi ; le statut de la livraison suit
  `orders.update_status`. Ne jamais modifier `deliveries` a la main sans modifier la commande.
- **Desactiver un livreur** : refuse tant qu'il a des livraisons vivantes (retirer ou reattribuer d'abord). La desactivation
  du profil ne ferme pas son compte ; pour couper aussi la connexion, desactiver l'utilisateur depuis l'administration.
- **Livreur bloque : « trop de codes incorrects »** (423 `DELIVERY_CODE_LOCKED`) : la livraison est verrouillee apres 5 essais.
  Un administrateur la conclut sans code (page Dispatch, « Livrer sans code », motif obligatoire) ou la traite comme un echec.
- **Le client n'a pas son code** : app client non a jour, ou client sans compte et sans telephone. Verifier que
  `delivery_proof_required` n'est actif que si l'app client affichant le code est deployee ; sinon le couper
  (`PUT /api/v1/delivery/settings`, administrateur) ou livrer sans code avec un motif.
- **Echecs « a traiter » qui s'accumulent** : la page Dispatch les liste en tete ; seul un administrateur les traite
  (rembourser, retenir des frais si la faute est au client, relivrer). Une commande en echec non traitee garde son
  paiement : a surveiller pour les empreintes (elles expirent seules apres ~7 jours).
- **Rotation de `JWT_SECRET`** : change tous les codes de remise en cours (ils sont recalcules, jamais stockes). A faire hors
  service, ou prevoir des livraisons sans code pour les commandes en route.
- **Le client ne voit pas son livreur / la carte est vide** : vérifier dans l'ordre (1) la commande est `out_for_delivery` ; (2) le livreur a **accepté**
  le partage (page Dispatch : « n'a pas accepté le partage de position ») ; (3) l'application du livreur n'a **pas été fermée** (balayée des
  applications récentes) et la localisation reste autorisée ; sur Android, la **notification « Livraison en cours »** doit être visible et
  l'application exclue de l'optimisation de batterie (Réglages > Applications > Batterie > Sans restriction, surtout Xiaomi, Huawei,
  Samsung, Oppo) ; sur iPhone, l'indicateur de localisation doit être visible ; (4) `GET /delivery/live` montre `signal_lost`.
- **Livreur « signal perdu »** : appeler le livreur. Si l'application a été fermée ou tuée par le système, la position se met à jour dès qu'il la rouvre (les points accumulés
  hors réseau sont renvoyés, au plus 30).
- **Purge GPS** : tâche ARQ `purge_driver_locations` (04 h 15 UTC). Vérifier dans les logs du worker `gps purge done`. Si elle ne tourne pas, les
  positions s'accumulent au-delà de 96 h (voir `PRIVACY.md`) : redémarrer le worker (§3). Ne **jamais** réduire `GPS_RETENTION_HOURS` sous 96
  (ignoré par le code) ; l'augmenter exige de mettre `PRIVACY.md` à jour.
- **Litige sur une course** : les positions échantillonnées d'un livreur (`driver_location_points`, avec `run_id`) sont conservées 96 h minimum après
  la mesure ; les extraire **avant** la purge si nécessaire.
- **Un livreur ne voit aucune commande « Disponibles »** : vérifier dans l'ordre (1) `driver_dispatch_enabled` actif ; (2) l'établissement est en mode
  `self_assign` (page Dispatch, carte de l'établissement) ; (3) la commande est une livraison confirmée (statut `confirmed`, `queued`, `preparing` ou `ready`) de **son** établissement ;
  (4) elle n'est pas déjà attribuée (`ORDER_ALREADY_TAKEN`, voir « En cours » au comptoir).
- **« Plafond atteint » (409 `DRIVER_CAPACITY_REACHED`)** : le livreur a déjà le maximum de livraisons `assigned`/`out_for_delivery`/`arrived`. Il livre ou rend
  une commande ; le comptoir peut toujours attribuer au-delà. Le plafond se règle par établissement (1 à 10).
- **Une commande est restée coincée chez un livreur absent** : le comptoir la retire (bouton « Retirer » de la page Dispatch) ; seule une commande **non partie** peut être
  rendue/retirée. Après départ : échec de livraison ou annulation.
- **Repasser un établissement en mode comptoir en urgence** : page Dispatch, carte de l'établissement (administrateur), ou `PUT
  /delivery/establishments/{id}/dispatch-settings` avec `{"expected_version": N, "dispatch_mode": "counter"}`. Les livraisons déjà prises restent attribuées.
  Pour couper toute prise libre partout : `driver_dispatch_enabled` à faux.
- **Règles d'échec différentes selon l'établissement** : `GET .../dispatch-settings` montre les valeurs effectives et `failure_rules_overridden`. Remettre
  `null` sur une règle pour qu'elle suive de nouveau le réglage général.
- **Retard affiché à tort** : l'estimation est recalculée au départ ; sans coordonnées de l'établissement ni zone, c'est celle de la commande qui s'applique.
  Renseigner la position de l'établissement.
- **Rollback de la migration `0079`** : `alembic downgrade 0078` supprime les réglages par établissement (retour : mode comptoir, plafond 3, règles
  générales). Les livraisons prises en libre-service ne sont pas touchées. Même réserve que `0076` sur `out of shared memory` localement.
- **Rollback de la migration `0078`** : `alembic downgrade 0077` supprime l'historique GPS, les dernières positions et les consentements ; mêmes
  précautions que `0076`/`0077`.
- **Rollback de la migration `0077`** : `alembic downgrade 0076` supprime les echecs et le journal des essais de code (donnees
  perdues) et la graine des codes ; memes precautions que pour `0076`.
- **Rollback de la migration `0076`** : `alembic downgrade 0075` supprime les tables livreurs/livraisons (donnees perdues).
  Sur une base avec beaucoup de schemas tenant, une seule transaction peut echouer (`out of shared memory`, voir
  `max_locks_per_transaction`) : dans ce cas supprimer schema par schema puis `alembic stamp 0075`.

---

## 3. Redémarrer le worker ARQ

**Symptôme** : jobs qui ne se traitent plus (emails non envoyés, alertes stock absentes, cron
`expire_loyalty_points`/`aggregate_live_stats` qui ne tournent plus).

1. Railway dashboard → service **worker** → **Restart**. Le worker est stateless (pool Redis arq) —
   un restart ne perd pas les jobs déjà enqueued (ils restent dans la queue Redis).
2. Vérifier les logs du service worker juste après restart : il doit logger la reprise des cron jobs
   (`aggregate_live_stats` toutes les 5 min, `expire_loyalty_points` à 03:00 UTC).
3. Si le restart ne résout rien, vérifier la connectivité Redis du worker (`ARQ_REDIS_URL`) —
   c'est une variable **distincte** de `REDIS_URL` (utilisée pour le pub/sub WebSocket), les deux
   doivent pointer vers la même instance Redis en général mais sont configurées séparément.
4. Jobs en échec définitif (après `max_tries=3`) : consulter la collection MongoDB
   `failed_jobs_{tenant_slug}` (voir `docs/modules/worker.md`, dead-letter handling) pour
   diagnostiquer et rejouer manuellement si nécessaire.

---

## 4. Backup et restore PostgreSQL

⚠️ **Checklist à exécuter manuellement par l'équipe, avec un accès réel à Railway** — aucun outil de
ce dépôt ne peut l'exécuter à votre place (pas d'accès aux identifiants Railway/production depuis un
environnement d'agent). **Ne pas accepter de données clients réelles tant que cette checklist n'a pas
été cochée en entier au moins une fois.**

`tools/verify_backup_restore.py` automatise les étapes 2, 4 et 5 (dump PostgreSQL, restore sur une
cible isolée, validation des comptages) ainsi qu'un dump/restore MongoDB équivalent (optionnel) et
un contrôle d'intégrité Cloudinary en lecture seule (voir son docstring pour le détail des
garde-fous). **Il ne couvre PAS l'étape 1** (vérification du plan Railway — nécessite le dashboard) ni
l'étape 8 (planification récurrente). Dry-run par défaut ; `--execute` exige `RESTORE_TEST_DATABASE_URL`
(instance isolée, distincte de `DATABASE_URL` **et** de `TEST_DATABASE_URL`) et `--confirm-target`.
Le rapport produit (JSON ou texte, horodaté, source/cible anonymisées) s'écrit hors dépôt par défaut
(`~/.api-kitchen-backup-reports/`) pour pouvoir être archivé sans jamais transiter par git :

```bash
uv run python tools/verify_backup_restore.py                                    # dry-run
RESTORE_TEST_DATABASE_URL="postgresql+asyncpg://...instance-isolee.../pizza_restore_test" \
  uv run python tools/verify_backup_restore.py --execute --confirm-target <hote-cible>
```

**Exécuter ce script ne remplace pas l'étape 1** (vérifier que les backups automatiques du
fournisseur sont réellement actifs) : une exécution réussie prouve qu'un dump pris à cet instant se
restaure et se valide correctement sur la cible fournie, pas que les backups automatiques Railway
fonctionnent ou qu'une restauration future réussira de la même manière — le rapport produit le
rappelle explicitement.

### Fréquence de sauvegarde et rétention

**Backups automatiques (fournisseur)** — Railway propose des backups automatiques sur les plans
PostgreSQL managés ; fréquence et rétention dépendent du plan souscrit et ne sont **pas vérifiables
depuis ce dépôt ni depuis un environnement d'agent** (nécessite le dashboard Railway, étape 1
ci-dessous). Tant que l'étape 1 n'a pas été cochée avec la fréquence/rétention réelles documentées
ici, considérer qu'**aucun backup automatique n'est garanti**.

**Backup manuel explicite (recommandé en complément, pas encore automatisé au 2026-09-08)** :
- PostgreSQL : cible quotidienne via `pg_dump` (étape 2), déclenché par un cron externe (GitHub
  Actions planifiée ou équivalent — étape 8, non encore mise en place). Rétention cible : 7 derniers
  dumps quotidiens + 4 hebdomadaires (rotation simple), stockés hors de la machine qui les produit
  (objet storage externe) et **jamais dans ce dépôt git**.
- MongoDB (`login_events_{tenant_slug}`, snapshots catalogue) : même fréquence recommandée via
  `mongodump` ; rétention alignée sur les 90 jours de rétention applicative déjà en place pour
  `login_events_*` (section 6).
- Médias Cloudinary : gérés par Cloudinary lui-même, pas de dump applicatif côté ce dépôt —
  `tools/verify_backup_restore.py` vérifie seulement que les `cloudinary_public_id` référencés en
  base résolvent toujours côté Cloudinary après une restauration PostgreSQL, pas une sauvegarde des
  fichiers eux-mêmes.
- Configuration non-secrète : versionnée nativement via `.env.example` dans ce dépôt, pas de
  sauvegarde séparée nécessaire. Les secrets réels (`JWT_SECRET`, `STRIPE_SECRET_KEY`, identifiants
  DB/Mongo/Cloudinary...) restent exclusivement dans les variables d'environnement Railway — jamais
  dans ce dépôt ni dans un dump : un `pg_dump`/`mongodump` ne contient que des données applicatives.

Ces chiffres sont des **cibles proposées, pas un engagement actif** — à valider/ajuster avec le
plan Railway réel (étape 1) et à formaliser dans un cron (étape 8) avant d'être considérées
opérationnelles.

### Objectifs RPO / RTO (réalistes, à recalibrer sur des données de production)

Mesurés localement le 2026-09-08 avec `tools/verify_backup_restore.py`, sur un jeu de test de 35
schémas tenant (~9,7 Mo de dump) :

| Étape | Durée mesurée (local, 35 tenants / 9,7 Mo) |
|---|---|
| `pg_dump` | ~7 s |
| `pg_restore` | ~21 s |
| Validation (connectivité + cohérence multi-tenant + données métier) | quelques secondes |

**RPO** (perte de données maximale tolérée) — borné par la fréquence de backup *réelle*, pas par la
vitesse du dump. Avec un backup manuel quotidien (cible ci-dessus) et sans confirmation des backups
automatiques Railway (étape 1 non cochée), le RPO **actuel est de 24 h dans le meilleur cas,
indéterminé tant que l'étape 1 n'est pas validée**.

**RTO** (temps de restauration) — le total dump+restore+validation mesuré ci-dessus (~30 s) est une
**borne basse non représentative** : petite base de test locale, sans latence réseau vers Railway,
sans provisionnement d'une instance de test (étape 3), sans démarrage applicatif (étape 6), sans
coordination humaine (étapes 1, 3, 7 sont manuelles). **Ne pas extrapoler ce chiffre local tel
quel** à un engagement client — un RTO réaliste doit être remesuré en conditions réelles (volume de
données de production, réseau Railway, provisionnement d'instance) avant d'être communiqué. Jusqu'à
ce recalibrage, retenir un ordre de grandeur prudent de **quelques heures**, le temps humain de
coordination et de provisionnement dominant très largement le temps machine.

### Responsabilités

- **Vérification des backups automatiques (étape 1)** : équipe ops/infra, accès dashboard Railway
  requis — non délégable à un agent ou un script de ce dépôt.
- **Exécution du test de restauration (checklist ci-dessous)** : à effectuer avant tout onboarding
  d'un client avec des données réelles, puis périodiquement une fois l'étape 8 automatisée. Un
  développeur backend disposant de `RESTORE_TEST_DATABASE_URL` peut l'exécuter seul via
  `tools/verify_backup_restore.py`.
- **Documentation du résultat (étape 7)** : la personne ayant exécuté le test, directement dans ce
  fichier.
- **Rotation des secrets en cas d'incident** : voir section 6.

### Checklist

- [ ] **1. Vérifier les backups automatiques du plan Railway**
  Dashboard Railway → service PostgreSQL → onglet **Backups**. Noter : sont-ils activés par défaut ?
  Fréquence ? Rétention (nombre de jours/snapshots) ? Si le plan souscrit ne les active pas
  nativement, passer directement à l'étape 2 pour un backup explicite.

- [ ] **2. Prendre un dump manuel de référence**
  Depuis une machine ayant `pg_dump` installé (même version majeure que le Postgres Railway — voir
  `postgres --version` dans les logs du service Railway) et la variable `DATABASE_URL` de production
  exportée dans l'environnement :
  ```bash
  pg_dump "$DATABASE_URL" -F c -f backup_$(date +%Y%m%d_%H%M%S).dump
  ```
  Vérifier que le fichier produit a une taille non nulle (`ls -lh backup_*.dump`) — un dump à 0 octet
  signifie un problème de connexion/permission silencieux à corriger avant de continuer.

- [ ] **3. Provisionner une instance Postgres de test isolée**
  Ne jamais restaurer directement sur l'instance de production. Soit un second service Postgres
  Railway dédié aux tests, soit une instance locale/Docker temporaire. Exporter son URL dans
  `RESTORE_TEST_DATABASE_URL` — **jamais** `TEST_DATABASE_URL` : cette dernière est la base que la
  suite pytest recrée/detruit à chaque run (`DROP SCHEMA ... CASCADE` par tenant, voir
  `tests/conftest.py::bootstrap_default_tenant`) ; la partager avec la vérification de restore
  ferait courir aux deux le risque de s'écraser mutuellement.

- [ ] **4. Restaurer le dump sur l'instance de test**
  ```bash
  pg_restore -d "$RESTORE_TEST_DATABASE_URL" --clean --if-exists backup_XXXXXXXX.dump
  ```
  Un code de sortie non nul ou des lignes `ERROR` dans la sortie signifient un problème à
  diagnostiquer avant de considérer le backup fiable (version Postgres incompatible, droits
  insuffisants, dump tronqué...).

- [ ] **5. Valider l'intégrité des données restaurées**
  `tools/verify_backup_restore.py --execute` exécute automatiquement, dans l'ordre, après un restore
  réussi :
  - `postgres_connectivity_check` — la cible restaurée accepte bien des connexions applicatives.
  - `postgres_tenant_coherence_check` — réutilise `tools/audit_tenant_schemas.py` (section 8) pour
    confirmer que chaque tenant de `public.tenants` a bien son schéma `tenant_{slug}` restauré, sans
    schéma orphelin ni table manquante par rapport à `Base.metadata`.
  - `postgres_business_data_check` — échantillonne jusqu'à `--validate-tenant-limit` tenants
    (défaut 25) et compte, via les modèles ORM réels (`User`, `Order`, `Product`), qu'il ne s'agit
    pas d'un dump vide ou partiel.
  - `cloudinary_media_cross_check` — pour les tenants échantillonnés, prend quelques
    `cloudinary_public_id` réellement stockés en base et confirme côté API Cloudinary (lecture seule)
    qu'ils résolvent toujours ; `skip` (pas un échec) si aucune image n'est présente dans
    l'échantillon.
  - `mongo_validate` — si `RESTORE_TEST_MONGO_URL` est fournie et qu'un restore Mongo a eu lieu :
    liste les collections et leurs comptages sur la cible restaurée.

  Si l'un de ces contrôles échoue, `overall_status` du rapport passe à `failure` (code de sortie non
  nul) — ne pas considérer le backup fiable tant que ce n'est pas corrigé. Pour une vérification
  manuelle complémentaire, les mêmes requêtes SQL directes restent valables :
  ```sql
  SELECT slug FROM public.tenants;                                    -- les tenants existent
  SELECT count(*) FROM tenant_<un_slug_reel>.users;                   -- des utilisateurs existent
  SELECT count(*) FROM tenant_<un_slug_reel>.orders;                  -- des commandes existent
  ```

- [ ] **6. Démarrer l'application contre l'instance restaurée (optionnel mais recommandé)**
  Pointer temporairement `DATABASE_URL` vers l'instance de test restaurée en local, lancer
  `uv run uvicorn app.main:app`, et confirmer qu'un login existant fonctionne (`POST /auth/login`
  avec un compte connu du dump) — preuve que les données restaurées sont réellement exploitables par
  l'application, pas seulement présentes en base.

- [ ] **7. Documenter le résultat ci-dessous**
  `tools/verify_backup_restore.py` écrit déjà, à chaque exécution, un rapport horodaté hors dépôt
  (`~/.api-kitchen-backup-reports/` par défaut, ou `--report-path`) — c'est la trace datée du
  résultat, à archiver (ticket interne, stockage objet...), jamais dans ce dépôt git. Une fois les
  6 étapes validées avec succès, remplacer la ligne suivante :

  > **Dernier test de restore réussi : jamais exécuté.**

  par : `Dernier test de restore réussi : <date> — dump de <taille>, restauré sur <instance de test>,
  validé par <nom>, rapport : <chemin ou référence d'archive>.` Si une étape échoue, documenter
  l'échec et le blocage ici plutôt que de laisser la ligne à « jamais exécuté » sans explication.

- [ ] **8. Automatiser la récurrence**
  Une fois la procédure validée manuellement, planifier son exécution périodique (cron externe,
  GitHub Actions planifiée, ou fonctionnalité native Railway si le plan le permet) plutôt que de
  dépendre d'une exécution manuelle ponctuelle.

**Dernier test de restore réussi : jamais exécuté.**

---

## 4bis. Stratégie de scaling (workers / pool DB / réplicas)

**État actuel :** `railpack.json` démarre `uvicorn` **sans flag `--workers`** — un seul process par
instance/réplica Railway. Le pool SQLAlchemy (`app/core/database/session.py`) est dimensionné à
`pool_size=10, max_overflow=20`, soit **30 connexions DB max par instance**.

**Règle d'alignement à respecter avant d'augmenter le nombre de réplicas ou de workers :**

```
connexions_DB_max_utilisées = pool_size_total × nombre_de_workers_par_instance × nombre_de_réplicas
```

Avec la configuration actuelle (1 worker/instance, `pool_size + max_overflow = 30`), chaque réplica
Railway peut ouvrir jusqu'à 30 connexions Postgres simultanées. **Avant d'augmenter le nombre de
réplicas**, vérifier la limite `max_connections` du plan PostgreSQL managé Railway souscrit
(dashboard Railway → service PostgreSQL → onglet Settings/Metrics) et s'assurer que
`30 × nombre_de_réplicas` reste confortablement en dessous de cette limite (garder une marge pour
les connexions d'outils externes : migrations manuelles, clients SQL, etc.).

**Deux leviers de scaling disponibles, à ne pas combiner sans recalculer la formule ci-dessus :**

1. **Scaling horizontal (recommandé en premier)** : augmenter le nombre de réplicas Railway du
   service `web`. Chaque réplica reste à 1 worker Uvicorn / `pool_size=10`. C'est le levier le plus
   simple à ajuster dynamiquement (dashboard Railway, pas de redéploiement de code).
2. **Scaling vertical (`--workers`)** : ajouter `--workers N` à la commande `uvicorn` dans
   `railpack.json` augmente le nombre de process par instance, donc multiplie la consommation du
   pool DB par `N` **sur la même instance**. À ne faire qu'après avoir confirmé la marge de
   connexions disponible, et en réduisant `pool_size` en conséquence si nécessaire (le total par
   instance doit rester sous contrôle).

**Recommandation :** privilégier le scaling horizontal (réplicas) tant que le plan Postgres le
permet — plus simple à raisonner avec la formule ci-dessus, et plus résilient (une instance qui
plante n'affecte qu'une fraction du trafic).

---

## 5. Smoke test post-déploiement

Après tout déploiement en production :

```bash
curl -f https://<domaine>/health
curl -f https://<domaine>/health/ready
```

Puis un parcours applicatif minimal : login staff/admin existant → `GET /api/v1/catalog/products`
→ `POST /api/v1/orders` (avec un compte de test) → vérifier réception d'une notification WebSocket
si un client est connecté. Si Sentry est configuré (`SENTRY_DSN`), forcer une erreur contrôlée
(endpoint de test ou exception volontaire) et vérifier sa réception dans Sentry avant de considérer
l'observabilité opérationnelle.

---

## 6. Incident sécurité (fuite suspectée, compte compromis)

1. Révoquer les sessions concernées : `POST /api/v1/admin/users/{id}/sessions/revoke-all` (ou via
   le endpoint sessions de l'utilisateur lui-même si c'est son propre compte).
2. Bannir l'IP si attaque active : `POST /api/v1/admin/ban-ip` (super-admin).
3. Consulter le SIEM WebSocket (`GET /api/v1/admin/ws-alerts`) et les login events MongoDB
   (`login_events_{tenant_slug}`, rétention 90 jours) pour reconstituer la chronologie.
4. Si une clé secrète a fuité (`JWT_SECRET`, `STRIPE_SECRET_KEY`, etc.) : la faire tourner
   immédiatement dans les variables d'environnement Railway et redéployer — toutes les sessions
   actives seront invalidées (JWT signés avec l'ancien secret deviennent invalides).

## 7. Connexion POS (hub externe) — limitations connues

Avant d'activer la fonctionnalité en configurant les variables `POS_HUB_*` en production, prendre connaissance de ces limitations identifiées lors de la revue finale (2026-08-10), acceptées comme dette technique à traiter dans un lot futur :

1. **Le catalogue passe en lecture seule dès la connexion POS active.** `save_connection` (`app/modules/pos/service.py`) passe `public.tenants.integration_mode` à `'connected'`, et `app/modules/catalog/deps.py` (`require_catalog_writable`) bloque alors toutes les écritures catalogue (produits, allergènes, images) pour ce tenant. Ce comportement n'est aujourd'hui affiché nulle part côté admin — un restaurant qui active la connexion POS perd silencieusement la possibilité d'éditer son catalogue depuis l'interface interne.
2. **Une connexion POS peut être transférée d'un tenant à un autre.** L'upsert dans `save_connection` cible `(provider, external_establishment_id)` (contrainte `uq_pos_connections_provider_establishment`, migration 0044) sans vérifier le tenant. Si deux tenants complètent un flux OAuth pour le même `external_establishment_id` côté hub, le second « vole » la ligne : le premier tenant reste avec `integration_mode='connected'` mais sans ligne `pos_connections` active — `POST /pos/connect/disconnect` renvoie alors `404 POS_NOT_CONNECTED` et ce tenant ne peut plus revenir en mode standalone via l'API. Récupération actuelle : correction manuelle en base (`UPDATE public.tenants SET integration_mode = 'standalone' WHERE slug = '...'`).
3. **Pas de contrainte DB empêchant plusieurs connexions actives pour un même tenant.** La règle « une seule connexion active par tenant » n'est appliquée que par une vérification applicative dans `POST /pos/connect/start` (lire-puis-décider), pas par une contrainte SQL. Deux appels concurrents à `/start` pourraient théoriquement produire deux connexions actives pour le même tenant.

Avant d'activer la fonctionnalité pour un client réel, traiter au minimum le point 1 (affichage cohérent côté admin du mode « connecté » et de ses conséquences) et envisager un index unique partiel pour le point 3 (`CREATE UNIQUE INDEX ... ON public.pos_connections (tenant_id) WHERE status = 'active'`).

---

## 8. Audit des tenants PostgreSQL (pré-déploiement)

**Contexte** : l'isolation multi-tenant repose entièrement sur le nom du schéma PostgreSQL
(`tenant_{slug}`) — voir la section « Hidden constraints » de `CLAUDE.md`. PostgreSQL tronque
silencieusement tout identifiant de plus de 63 octets (`NAMEDATALEN=64`) au lieu de rejeter la
requête : un slug trop long, ou deux slugs partageant leurs 56 premiers caractères, peuvent produire
le même nom de schéma physique. `app/modules/auth/schemas.py` et
`app/modules/admin/tenants/lifecycle_router.py` empêchent désormais qu'un **nouveau** tenant soit créé
dans cet état, mais ne peuvent rien garantir sur des lignes déjà présentes en base (import de données,
intervention manuelle, restauration d'un backup pré-correctif...).

Depuis le correctif de provisioning unifié (`app/core/tenancy/provisioning.py::provision_tenant`,
utilisé par `POST /auth/register` ET `POST /admin/tenants`), un tenant créé par n'importe quel
parcours reçoit exactement la même structure, en une seule transaction PostgreSQL — un échec à
n'importe quelle étape annule tout (aucune ligne `public.tenants` orpheline, aucun schéma vide ou
partiel possible). Ce risque disparaît donc pour tout NOUVEAU tenant ; l'audit reste nécessaire pour
les tenants déjà en base avant ce correctif, et pour toute anomalie d'origine opérationnelle (import,
intervention manuelle, restauration).

`tools/audit_tenant_schemas.py` audite `public.tenants` face aux schémas `tenant_*` réellement
présents dans `pg_namespace`, **en lecture seule** (la connexion est ouverte avec
`SET TRANSACTION READ ONLY` — PostgreSQL refuse lui-même toute écriture, ce n'est pas qu'une
convention de code).

### Quand l'exécuter

- **Avant tout déploiement** touchant à l'auth, au provisioning de tenant, ou après une restauration
  de backup (voir section 4) — sur l'instance restaurée, avant de la promouvoir.
- En cas de doute sur l'intégrité multi-tenant (alerte, comportement suspect signalé par un tenant).
- Périodiquement en production (à automatiser en cron externe / GitHub Actions planifiée, même
  logique que la recommandation de la section 4).

### Exécution

```bash
# Contre la base configurée dans .env / DATABASE_URL
uv run python tools/audit_tenant_schemas.py

# Contre une base précise (ex. instance de staging ou backup restauré avant promotion)
uv run python tools/audit_tenant_schemas.py --database-url "postgresql+asyncpg://user:pass@host/db"

# Sortie JSON (intégration CI/monitoring)
uv run python tools/audit_tenant_schemas.py --format json
```

Code de sortie : `0` si aucune anomalie, `1` si au moins une anomalie détectée (à exploiter dans un
pipeline : `uv run python tools/audit_tenant_schemas.py || <bloquer le déploiement>`), `2` en cas
d'erreur de connexion/exécution (à distinguer d'un vrai « OK »).

### Ce que l'audit vérifie

1. Tenants dont le slug dépasse 56 caractères (la limite de création, voir
   `TENANT_SLUG_MAX_LENGTH_FOR_CREATION`).
2. Couples de tenants dont `tenant_{slug}` partage le même préfixe de 63 octets — le nom physique que
   PostgreSQL retiendrait réellement pour chacun, donc une collision certaine s'ils sont (ou
   deviennent) tous les deux provisionnés.
3. Cohérence `public.tenants` ↔ `pg_namespace`, dans les deux sens :
   - tenants enregistrés sans schéma physique correspondant ;
   - schémas `tenant_*` sans ligne `public.tenants` correspondante (orphelins).
4. Schémas `tenant_*` existants mais **incomplets** — tables attendues (d'après `Base.metadata`, la
   même source de vérité que `provision_tenant()` et les migrations Alembic) absentes du schéma réel.
   Signale un provisioning interrompu ou une migration tenant jamais appliquée à ce schéma précis.

### En cas d'anomalie détectée

Ne **jamais** corriger silencieusement en supprimant des données sans comprendre la cause :

- **Slug trop long / couple en collision** : vérifier en premier si les deux tenants partagent
  réellement le même schéma physique (`\dn tenant_*` en `psql`, ou `pg_namespace` directement) — si
  oui, c'est un incident d'isolation actif (voir section 6) et non une simple anomalie de données.
- **Tenant sans schéma** : le tenant est inaccessible (login impossible). Vérifier les logs applicatifs
  autour de sa date de création (`created_at`) pour un `TENANT_SCHEMA_COLLISION` ou une erreur de
  provisioning déjà survenue, avant de reprovisionner manuellement.
- **Schéma orphelin** : confirmer qu'aucun tenant actif n'en dépend avant tout `DROP SCHEMA` — un
  schéma orphelin peut aussi être un résidu d'un tenant offboardé délibérément (ligne `public.tenants`
  supprimée sans nettoyer le schéma), à ne pas confondre avec une collision.
- **Schéma incomplet** : avec le provisioning unifié et atomique, un schéma incomplet ne peut plus
  provenir d'un `POST /auth/register` ou `POST /admin/tenants` normal — c'est le signal d'une migration
  tenant qui n'a pas bouclé sur ce schéma (voir `alembic/versions/00XX_*.py`, pattern `_get_tenant_slugs`)
  ou d'une intervention manuelle (`DROP TABLE` accidentel). Rejouer la migration concernée sur ce schéma
  précis plutôt que de recréer le tenant.
