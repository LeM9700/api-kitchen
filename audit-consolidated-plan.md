# Plan Consolidé — Tous les Points P0/P1/P2/P3 des Audits api-pizza

> Source : `audit/audit-discovery-report.md`, `audit/audit-security-report.md`,
> `audit/audit-features-report.md`, `audit/audit-performance-report.md`, `audit/audit-prod-report.md`
> — tous mis à jour le 2026-07-18 après une passe complète de fermeture des P1 et P2.
>
> Ce fichier consolide et déduplique tous les points **encore ouverts** (P0→P3). Les points marqués
> ✅ CORRIGÉ dans les audits sources ne sont PAS repris ici (voir l'historique dans chaque rapport
> pour le détail de ce qui a été fait).
>
> **Bilan de cette passe (2026-07-18)** : les 2 P1 restants au 2026-07-17 (régression MFA admin,
> backup non testé) et l'intégralité des 12 points P2 identifiés sont traités. **11 des 12 P2 sont
> corrigés en code/doc** ; 1 (test de restore backup réel) reste, par nature, une action manuelle sur
> l'infrastructure Railway hors du périmètre d'un agent de code.

---

## P0 — Must Fix Before Production

**Aucun.**

---

## P1 — High Priority Before Launch

### P1-01 — MFA admin : routeur permissif, service bloquant — ✅ CORRIGÉ (2026-07-18)

Code corrigé (`app/modules/auth/service.py`), test de régression ajouté (`tests/test_auth_mfa.py`).
Réserve : non exécuté de bout en bout localement (pas de DB) — collecte pytest OK, à confirmer en CI.

### P1-02 — Backup PostgreSQL : checklist livrée, exécution réelle toujours à faire

**Seul point encore réellement ouvert de tout ce plan.** `RUNBOOK.md` §4 contient une checklist
exécutable en 8 étapes. **Action restante (non exécutable par un agent de code — nécessite des
identifiants Railway réels) :** suivre la checklist, documenter la date du test réussi.
**Effort :** 3-4h, à la charge de l'équipe.

---

## P2 — Production Hardening — 11/12 corrigés (2026-07-18)

| # | Item | Statut | Détail |
|---|---|---|---|
| P2-01 | Index `stock_movements.ingredient_id` manquant | ✅ CORRIGÉ | Migration `alembic/versions/0032_stock_orders_indexes.py` |
| P2-02 | Index composite `orders(status, created_at)` | ✅ CORRIGÉ | Même migration 0032 |
| P2-03 | `.env.example` incomplet (SMTP, APP_BASE_URL) | ✅ CORRIGÉ | `.env.example` complété |
| P2-04 | Cache Redis absent (catalogue, tenant status) | ✅ CORRIGÉ | `app/core/services/cache.py` (nouveau) + intégration `catalog/router.py` (TTL 30s, invalidation sur mutations produit/CSV) et `admin/tenants/router.py` (TTL 10s, invalidation sur mutations config/heures/fermetures) |
| P2-05 | Timeout `expire_loyalty_points` incohérent | ✅ CORRIGÉ | `worker/main.py` — `cron(..., timeout=600)` ajouté, vérifié via inspection de `WorkerSettings.cron_jobs` (`timeout_s=600` confirmé) |
| P2-06 | Stratégie de scaling non documentée | ✅ CORRIGÉ | `RUNBOOK.md` §4bis — formule d'alignement pool/workers/réplicas + recommandation |
| P2-07 | Aucun test de charge (k6/Locust) | ✅ CORRIGÉ (script livré, non exécuté) | `scripts/load-test.js` (k6), documenté dans `README.md`. **k6 non installé dans cet environnement — script écrit et vérifié syntaxiquement (`node --check`), jamais exécuté contre une instance réelle.** |
| P2-08 | Catalog search sans filtres price/allergen | ✅ CORRIGÉ | `?price_min=`, `?price_max=`, `?allergen_free=` ajoutés (`catalog/router.py`, `catalog/service.py::search_products`) |
| P2-09 | Promotions sans filtre `?active_only=` | ✅ Déjà correct par conception | `list_active()` filtre déjà `is_active`, `is_public`, fenêtre de dates — le finding initial de l'audit features était imprécis, aucun changement nécessaire |
| P2-10 | Dossier `Microsoft/` non nettoyé | ✅ CORRIGÉ | Supprimé + ajouté à `.gitignore` |
| P2-11 | Logging non structuré (pas de correlation ID) | ✅ CORRIGÉ | `app/core/http/logging_config.py` (nouveau) — JSON structuré + `request_id` via ContextVar, propagé par le middleware existant, header `X-Request-ID` sur la réponse. Vérifié fonctionnellement en isolation (sortie JSON valide avec `request_id` confirmée). |
| P2-12 | Politique de confidentialité / export de données absente | ✅ CORRIGÉ | `PRIVACY.md` (nouveau, avec réserve explicite « à valider juridiquement ») + `GET /customer/me/export` (profil + jusqu'à 100 commandes), testé (`tests/test_customer.py::test_export_me_*`, **2/2 passent réellement en local**, pas de DB requise car service mocké) |

---

## P3 — Nice To Have (facultatif, non traité dans cette passe)

- `TrustedHostMiddleware` / `Cross-Origin-Opener-Policy`
- Middleware de latence dédié (largement redondant maintenant avec Sentry APM + le logging structuré ajouté en P2-11)
- Analytics event tracking, filtres date_from/date_to sur orders/me, filtre dietary_tag catalog, export CSV commandes, notification push expiration points
- Confirmer le nettoyage de `.venv312` dupliqué
- Étendre l'export de données client (P2-12) au-delà de 100 commandes / inclure fidélité et device tokens si un usage réel le justifie
- Purge physique différée des comptes supprimés (actuellement soft delete uniquement — voir `PRIVACY.md` §4)

---

**Total effort résiduel : P1 ≈ 3-4h (100% opérationnel, hors périmètre code) · P2 ≈ 0h (fermé, hors k6 à exécuter) · P3 ≈ variable, non bloquant.**

Il ne reste plus aucun point P0, P1 de code, ou P2 de code ouvert. Le seul travail restant qui ne peut
pas être fait par un agent de code est l'exécution réelle de la checklist de restore de backup
(`RUNBOOK.md` §4) et, en bonus, l'exécution du script `scripts/load-test.js` contre un environnement
réel pour obtenir une baseline chiffrée (non bloquant, contrairement au backup).
