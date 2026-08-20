"""Script de création du compte super-admin initial.

Usage:
    python tools/seed_super_admin.py --email admin@example.com --password MonMotDePasse1!

[🔒 SÉCURITÉ] À exécuter UNE SEULE FOIS sur la base de production depuis Railway
ou un shell sécurisé. Ne jamais committer de credentials en clair.
"""

import argparse
import asyncio
import sys
import os

# Ajoute la racine du projet au PYTHONPATH
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

from app.core.auth.security import get_password_hash
from app.core.config import settings


async def seed(email: str, password: str, force: bool = False) -> None:
    engine = create_async_engine(settings.database_url, echo=False)

    async with engine.begin() as conn:
        existing = await conn.execute(
            text("SELECT id FROM public.super_admins WHERE email = :email"),
            {"email": email},
        )
        row = existing.scalar_one_or_none()

        if row is not None and not force:
            print(f"[SKIP] Un super-admin avec l'email '{email}' existe déjà (id={row}).")
            print("       Utilisez --force pour écraser le mot de passe.")
            return

        password_hash = get_password_hash(password)

        if row is not None:
            await conn.execute(
                text(
                    "UPDATE public.super_admins SET password_hash = :hash, is_active = TRUE "
                    "WHERE email = :email"
                ),
                {"hash": password_hash, "email": email},
            )
            print(f"[OK] Mot de passe mis à jour pour le super-admin '{email}'.")
        else:
            await conn.execute(
                text(
                    "INSERT INTO public.super_admins (email, password_hash, is_active) "
                    "VALUES (:email, :hash, TRUE)"
                ),
                {"email": email, "hash": password_hash},
            )
            print(f"[OK] Super-admin créé : {email}")

    await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed du compte super-admin.")
    parser.add_argument("--email", required=True, help="Email du super-admin")
    parser.add_argument("--password", required=True, help="Mot de passe (min 8 chars)")
    parser.add_argument("--force", action="store_true", help="Écrase si existe déjà")
    args = parser.parse_args()

    if len(args.password) < 8:
        print("[ERREUR] Le mot de passe doit faire au moins 8 caractères.")
        sys.exit(1)

    asyncio.run(seed(args.email, args.password, force=args.force))


if __name__ == "__main__":
    main()
