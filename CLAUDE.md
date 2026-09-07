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
- **`worker/tasks/*.py` do NOT share `app/core/database/session.py`'s tenant search_path safety
  net.** Each worker task file opens its own ad hoc `create_async_engine`/session (e.g.
  `hr_alerts.py::_open_tenant_session`) with a single `SET search_path TO "{schema}", public` —
  the `, public` fallback, and the missing `SessionEvents.after_begin`/`PoolEvents.reset`
  protections, that the API side deliberately removed (see git history around
  `app/core/database/session.py`). Known, unfixed gap as of this writing — treat any worker task
  that commits mid-session and keeps querying as suspect, and don't assume the API's isolation
  guarantees extend here without checking the specific task file.

## Planning state (`.planning/`, GSD)

`.planning/STATE.md`, `PROJECT.md`, and `ROADMAP.md` track the stock-module milestone (v1.0,
complete) plus a running ledger of work done outside that milestone (payments, loyalty, customer,
admin, auth — some via further GSD phases, some via ad-hoc Superpowers specs, documented honestly
in ROADMAP.md rather than retrofitted). If you resume work via `/gsd:resume-work` or
`/gsd:progress`, these files were corrected on 2026-07-20 after being stale for weeks — trust them,
but re-verify against actual code before reporting status on anything older than that date.
