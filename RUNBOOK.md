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
  `TEST_DATABASE_URL` (déjà une variable connue du projet, voir `.env.example`).

- [ ] **4. Restaurer le dump sur l'instance de test**
  ```bash
  pg_restore -d "$TEST_DATABASE_URL" --clean --if-exists backup_XXXXXXXX.dump
  ```
  Un code de sortie non nul ou des lignes `ERROR` dans la sortie signifient un problème à
  diagnostiquer avant de considérer le backup fiable (version Postgres incompatible, droits
  insuffisants, dump tronqué...).

- [ ] **5. Valider l'intégrité des données restaurées**
  Sur l'instance de test restaurée, exécuter au minimum :
  ```sql
  SELECT slug FROM public.tenants;                                    -- les tenants existent
  SELECT count(*) FROM tenant_<un_slug_reel>.users;                   -- des utilisateurs existent
  SELECT count(*) FROM tenant_<un_slug_reel>.orders;                  -- des commandes existent
  ```
  Comparer les comptages avec des valeurs connues côté production (approximatives suffisent) pour
  confirmer qu'il ne s'agit pas d'un dump vide ou partiel.

- [ ] **6. Démarrer l'application contre l'instance restaurée (optionnel mais recommandé)**
  Pointer temporairement `DATABASE_URL` vers l'instance de test restaurée en local, lancer
  `uv run uvicorn app.main:app`, et confirmer qu'un login existant fonctionne (`POST /auth/login`
  avec un compte connu du dump) — preuve que les données restaurées sont réellement exploitables par
  l'application, pas seulement présentes en base.

- [ ] **7. Documenter le résultat ci-dessous**
  Une fois les 6 étapes validées avec succès, remplacer la ligne suivante :

  > **Dernier test de restore réussi : jamais exécuté.**

  par : `Dernier test de restore réussi : <date> — dump de <taille>, restauré sur <instance de test>,
  validé par <nom>.` Si une étape échoue, documenter l'échec et le blocage ici plutôt que de laisser
  la ligne à « jamais exécuté » sans explication.

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
