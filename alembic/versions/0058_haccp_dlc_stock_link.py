"""Link haccp_dlc_checks to real stock ingredients/batches via FK

Revision ID: 0058
Revises: 0057
Create Date: 2026-09-13

Contexte : haccp_dlc_checks.ingredient_id/batch_id existaient deja comme
simples colonnes Integer, jamais reliees par contrainte, et jamais peuplees
par l'interface (formulaire texte libre). Cette migration :

1. Rend session_id nullable -- une verification DLC loguee depuis l'onglet
   Stock (hors d'une session ouverture/fermeture) n'a pas de session.
2. NULL les valeurs orphelines existantes de ingredient_id/batch_id (aucune
   ligne ingredients/ingredient_batches correspondante) avant d'ajouter les
   contraintes -- defensif, ces colonnes n'ont jamais ete validees jusqu'ici
   donc rien ne garantit qu'elles pointent vers une ligne reelle.
3. Ajoute les FK ingredient_id -> ingredients.id et batch_id ->
   ingredient_batches.id, ON DELETE SET NULL (un ingredient/lot supprime ne
   doit pas faire disparaitre l'historique de controle DLC).
"""

from alembic import op
import sqlalchemy as sa

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None


def _get_tenant_slugs(bind) -> list[str]:
    result = bind.execute(sa.text("SELECT slug FROM public.tenants"))
    return [row[0] for row in result]


def upgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"

        op.alter_column(
            "haccp_dlc_checks",
            "session_id",
            existing_type=sa.Integer(),
            nullable=True,
            schema=schema,
        )

        bind.execute(sa.text(
            f'UPDATE "{schema}".haccp_dlc_checks c '
            f'SET ingredient_id = NULL '
            f'WHERE c.ingredient_id IS NOT NULL '
            f'AND NOT EXISTS (SELECT 1 FROM "{schema}".ingredients i WHERE i.id = c.ingredient_id)'
        ))
        bind.execute(sa.text(
            f'UPDATE "{schema}".haccp_dlc_checks c '
            f'SET batch_id = NULL '
            f'WHERE c.batch_id IS NOT NULL '
            f'AND NOT EXISTS (SELECT 1 FROM "{schema}".ingredient_batches b WHERE b.id = c.batch_id)'
        ))

        op.create_foreign_key(
            f"fk_haccp_dlc_checks_ingredient_{slug}",
            "haccp_dlc_checks",
            "ingredients",
            ["ingredient_id"],
            ["id"],
            source_schema=schema,
            referent_schema=schema,
            ondelete="SET NULL",
        )
        op.create_foreign_key(
            f"fk_haccp_dlc_checks_batch_{slug}",
            "haccp_dlc_checks",
            "ingredient_batches",
            ["batch_id"],
            ["id"],
            source_schema=schema,
            referent_schema=schema,
            ondelete="SET NULL",
        )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"

        op.drop_constraint(
            f"fk_haccp_dlc_checks_batch_{slug}",
            "haccp_dlc_checks",
            schema=schema,
            type_="foreignkey",
        )
        op.drop_constraint(
            f"fk_haccp_dlc_checks_ingredient_{slug}",
            "haccp_dlc_checks",
            schema=schema,
            type_="foreignkey",
        )
        op.alter_column(
            "haccp_dlc_checks",
            "session_id",
            existing_type=sa.Integer(),
            nullable=False,
            schema=schema,
        )
