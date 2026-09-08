# CLAUDE.md — api-pizza

FastAPI multi-tenant SaaS backend for pizzeria/restaurant management. Full stack, setup, test, and
migration commands are in [README.md](README.md) — read that first for anything code-related.
Ops runbook: [RUNBOOK.md](RUNBOOK.md). This file only holds constraints that aren't obvious from
reading the code and that a session should know before touching it.

## Repo identity

This directory is its own git repository (remote: `LeM9700/api-kitchen`), independent from the
`pizza` workspace repo one level up. It also has its own CI (`.github/workflows/ci.yml`: Alembic
migrations, pytest + coverage, `pip-audit` — no `alembic check` step) and is deployed via Railway
(`railpack.json`). Commit and push from *inside* `api-pizza/`, not from the workspace root.

## Hidden constraints

- **Tenant isolation is per-schema, not per-row.** Each tenant gets a dedicated PostgreSQL schema
  (`tenant_{slug}`). There is no `tenant_id` column to filter on in most tables — isolation is
  structural. Never assume a shared-schema pattern when reviewing or writing queries here.
- **Every schema change must be made in two places.** New-tenant provisioning goes through
  `provision_tenant()` in `app/core/tenancy/provisioning.py` — a single atomic function (one
  PostgreSQL transaction: `public.tenants` row, schema, all tables via `Base.metadata.create_all`,
  seed data, first admin) shared by `POST /auth/register` and `POST /admin/tenants`. It does
  **not** go through Alembic. Existing tenants get the change via a normal Alembic migration,
  looped over all rows in `public.tenants`. Forgetting either half leaves new and existing tenants
  with diverging schemas — `tools/audit_tenant_schemas.py` (read-only, see `RUNBOOK.md` §8) detects
  this after the fact by diffing each tenant schema's actual tables against `Base.metadata`.
  New model classes must be imported into `app/core/database/tenant_models.py` to reach both paths.
- **Stock deduction/restoration must stay inside the `update_status` transaction.** Atomicity of
  `deduct_for_order` / `restore_for_order` is the module's core value (no negative stock, no silent
  loss) — don't refactor these into separate commits.
- **The ARQ worker is a second deployable process**, not a background thread of the API. It needs
  its own Railway service with `python -m arq worker.main.WorkerSettings` as start command.
- **`worker/tasks/*.py` share `app/core/database/session.py`'s tenant search_path safety net.**
  Fixed — every worker task now opens sessions via `get_tenant_session()`/`get_public_session()`
  (the same shared, hardened `engine` the API imports), instead of each file building its own ad
  hoc `create_async_engine` + a single `SET search_path TO "{schema}", public` per invocation. That
  old pattern had two problems: the `, public` fallback (silent resolution to the legacy homonymous
  tables in `public`, see the no-fallback rationale on `get_tenant_session()`'s docstring) and no
  `SessionEvents.after_begin`/`PoolEvents.reset` protection, so a task that committed mid-session
  and kept querying (`worker/tasks/stock_alerts.py::send_stock_alert`, most cron jobs looping over
  `_get_all_tenant_slugs`) risked the search_path silently reverting once the pool recycled the
  connection — worst for `worker/tasks/loyalty.py::expire_loyalty_points`, which processes tenants
  **concurrently** (`asyncio.gather` + `Semaphore(10)`), the exact shape most likely to hit it.
  `tests/test_search_path_pool_safety.py::test_concurrent_gather_across_tenants_never_cross_contaminates`
  proves the fix holds under genuine concurrent contention on a single-connection pool. Any *new*
  worker task must use `get_tenant_session()`/`get_public_session()` too — never re-introduce a
  local `create_async_engine` + manual `SET search_path`.

## Planning state (`.planning/`, GSD)

`.planning/STATE.md`, `PROJECT.md`, and `ROADMAP.md` track the stock-module milestone (v1.0,
complete) plus a running ledger of work done outside that milestone (payments, loyalty, customer,
admin, auth — some via further GSD phases, some via ad-hoc Superpowers specs, documented honestly
in ROADMAP.md rather than retrofitted). If you resume work via `/gsd:resume-work` or
`/gsd:progress`, these files were corrected on 2026-07-20 after being stale for weeks — trust them,
but re-verify against actual code before reporting status on anything older than that date.
