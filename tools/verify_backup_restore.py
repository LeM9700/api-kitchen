"""Verification outillee de la procedure de backup/restore -- RUNBOOK.md section 4.

Usage (dry-run, AUCUNE commande destructive -- decrit ce qui serait fait) :
    uv run python tools/verify_backup_restore.py

Usage reel (apres avoir verifie le dry-run) :
    uv run python tools/verify_backup_restore.py --execute --confirm-target <host-cible>

Variables d'environnement requises pour --execute :
    DATABASE_URL                 Source a dumper (deja connue du projet).
    RESTORE_TEST_DATABASE_URL    Cible de restauration -- DOIT etre distincte de
                                  DATABASE_URL, DOIT etre une instance Postgres isolee
                                  dediee aux tests de restore (jamais la base de test
                                  pytest -- voir TEST_DATABASE_URL, un usage different).

Variables optionnelles :
    RESTORE_TEST_MONGO_URL       Cible de restauration Mongo (dedie, distincte de
                                  MONGO_URL). Absent = section Mongo marquee SKIPPED,
                                  jamais faussement PASSED.

Ce que ce script NE fait PAS et ne pretend PAS faire :
    - Il ne prouve PAS que les backups automatiques Railway sont actives ou fiables --
      cela reste une verification humaine au dashboard Railway (RUNBOOK.md section 4,
      etape 1). Ce script couvre les etapes 2, 4, 5 (dump/restore/validation) ; il ne
      couvre PAS l'etape 1 (verification du plan Railway) ni l'etape 8 (planification
      recurrente), qui ne peuvent pas etre constatees depuis ce depot.
    - Une execution reussie de ce script prouve qu'UN dump pris a l'instant T se
      restaure et valide correctement sur la cible fournie -- pas que les backups
      *automatiques* du fournisseur fonctionnent, pas qu'une restauration future
      fonctionnera de la meme maniere. Le rapport produit distingue explicitement
      "preuve d'execution reelle" (ce dump-ci, cette restauration-ci, a cet instant)
      de "procedure disponible" (le script existe et peut etre relance).
    - Redis n'est PAS couvert par un dump/restore ici : les donnees Redis de ce
      projet (pub/sub WebSocket, compteurs de rate limit, flags de revocation de
      session, connexions WS actives) sont explicitement ephemeres/reconstructibles
      -- voir CLAUDE.md. Le rapport le documente comme N/A avec la raison, plutot que
      de simuler un test sans objet.
    - Cloudinary n'a PAS de notion de "restore" applicable ici : Cloudinary EST deja
      le stockage manage source de verite pour les medias (pas une copie qu'on
      restaurerait depuis un dump applicatif). La section Cloudinary est un controle
      d'INTEGRITE en lecture seule (l'API repond, un echantillon de medias references
      resout reellement) -- explicitement etiquete comme tel dans le rapport, jamais
      confondu avec un test de restore.

Garde-fous de securite (dans l'ordre, TOUS doivent passer avant la moindre commande
destructive -- pg_restore --clean / mongorestore --drop) :
    1. RESTORE_TEST_DATABASE_URL doit etre definie (sinon : abandon).
    2. RESTORE_TEST_DATABASE_URL doit differer de DATABASE_URL (sinon : abandon --
       jamais de --clean sur la source).
    3. RESTORE_TEST_DATABASE_URL doit pointer un HOTE different de DATABASE_URL
       (meme hote = probablement la meme instance Postgres managee, meme avec un nom
       de base different -- refuse : RUNBOOK.md exige une instance isolee).
    4. Ni l'hote ni le nom de base de RESTORE_TEST_DATABASE_URL ne doivent contenir un
       marqueur de production (liste : prod, production, live -- insensible a la
       casse). Heuristique de defense en profondeur, PAS une garantie a elle seule.
    5. L'hote ou le nom de base de RESTORE_TEST_DATABASE_URL doit contenir au moins un
       marqueur "isole" reconnu (par defaut : test, restore, staging, sandbox --
       ajustable via --safe-markers). Refuse si aucun marqueur present.
    6. --confirm-target <host> doit etre fourni ET correspondre EXACTEMENT (sensible a
       la casse) a l'hote reellement resolu de RESTORE_TEST_DATABASE_URL -- oblige un
       humain a nommer explicitement la cible a chaque execution reelle, plutot que de
       ne dependre que d'une variable d'environnement qui pourrait etre perimee.
    7. --execute doit etre passe explicitement. Sans lui : dry-run, aucune commande
       destructive, le rapport decrit ce qui SERAIT fait.

Rapport : JSON ou texte, horodate, ecrit par defaut HORS du depot
(~/.api-kitchen-backup-reports/) pour pouvoir etre archive sans jamais transiter par
git. Contient : source et cible ANONYMISEES (identifiants et details d'hote masques),
duree totale et par etape, liste des controles effectues avec leur resultat, statut
global. Ne contient jamais d'identifiants, de mot de passe, ni le contenu des donnees.

Aucun secret, aucune URL de production, aucun dump ne doit jamais etre commit -- ce
script n'effectue lui-meme aucune operation git ; les fichiers qu'il produit (dumps,
rapports) vont dans --work-dir / --report-dir, tous deux HORS du depot par defaut.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPORT_DIR = Path.home() / ".api-kitchen-backup-reports"
DEFAULT_SAFE_MARKERS = ("test", "restore", "staging", "sandbox")
PRODUCTION_DENYLIST = ("prod", "production", "live")


class SafetyError(Exception):
    """Un garde-fou de securite a bloque l'execution -- jamais contourne silencieusement."""


# ---------------------------------------------------------------------------
# Masquage d'URL -- jamais de credentials/hote complet dans le rapport ou les logs
# ---------------------------------------------------------------------------


def mask_url(url: str) -> str:
    """Retourne une representation sure a logger/archiver : jamais de credentials,
    hote et base tronques (assez pour distinguer deux cibles, pas assez pour cibler
    l'infra reelle depuis un rapport archive).

    Args:
        url: URL de connexion complete (peut contenir user:password@host/db).

    Returns:
        Chaine masquee, ex. ``postgresql+asyncpg://***@ra***.railway.internal/pi***``.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "***invalid-url***"
    host = parts.hostname or ""
    db = parts.path.lstrip("/")
    masked_host = (host[:2] + "***") if len(host) > 2 else "***"
    masked_db = (db[:2] + "***") if len(db) > 2 else "***"
    scheme = parts.scheme or "?"
    return f"{scheme}://***@{masked_host}/{masked_db}"


def _to_libpq_url(url: str) -> str:
    """Convertit une URL SQLAlchemy (``postgresql+asyncpg://...``) vers le format
    que ``pg_dump``/``pg_restore`` (libpq natif) comprennent reellement.

    [SECURITE / CORRECTNESS] libpq ne reconnait PAS le suffixe ``+driver`` du
    schema : passe tel quel, il ne leve PAS d'erreur claire -- il retombe
    silencieusement sur l'authentification peer/socket (ignorant user/password de
    l'URL), produisant un dump de 0 octet ou une connexion vers le mauvais
    compte selon l'environnement. Verifie empiriquement contre ce depot :
    ``DATABASE_URL``/``RESTORE_TEST_DATABASE_URL`` utilisent tous deux
    ``postgresql+asyncpg://`` (voir app/core/config/settings.py). Seul ce
    schema est reecrit ici -- les credentials/hote/base/port restent inchanges.
    """
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    base_scheme = scheme.split("+", 1)[0]
    return f"{base_scheme}://{rest}"


def _hostname(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _dbname(url: str) -> str:
    try:
        return urlsplit(url).path.lstrip("/").lower()
    except ValueError:
        return ""


# ---------------------------------------------------------------------------
# Garde-fous -- executes AVANT toute commande destructive, jamais apres
# ---------------------------------------------------------------------------


def assert_safe_restore_target(
    source_url: str,
    target_url: str | None,
    confirm_target: str | None,
    safe_markers: tuple[str, ...] = DEFAULT_SAFE_MARKERS,
) -> str:
    """Verifie les garde-fous 1 a 6 du docstring de module. Leve SafetyError avec un
    message explicite au premier echec -- jamais un abandon silencieux.

    Args:
        source_url: DATABASE_URL (source du dump).
        target_url: RESTORE_TEST_DATABASE_URL (cible de restauration), ou None si absente.
        confirm_target: Valeur de --confirm-target fournie par l'appelant, ou None.
        safe_markers: Marqueurs "isole" reconnus (garde-fou 5).

    Returns:
        L'hote resolu de la cible (utile pour le rapport), si tous les garde-fous passent.

    Raises:
        SafetyError: au premier garde-fou non satisfait.
    """
    if not target_url:
        raise SafetyError(
            "RESTORE_TEST_DATABASE_URL n'est pas definie. Ce script ne restaure "
            "jamais sur une cible implicite -- definir explicitement une instance "
            "Postgres isolee dediee aux tests de restore (jamais TEST_DATABASE_URL, "
            "qui sert a la suite pytest)."
        )

    if target_url.strip() == source_url.strip():
        raise SafetyError(
            "RESTORE_TEST_DATABASE_URL est identique a DATABASE_URL -- refus "
            "categorique d'executer pg_restore --clean sur la source."
        )

    source_host = _hostname(source_url)
    target_host = _hostname(target_url)
    target_db = _dbname(target_url)

    if not target_host:
        raise SafetyError("RESTORE_TEST_DATABASE_URL ne contient pas d'hote resolvable.")

    if target_host == source_host:
        raise SafetyError(
            f"RESTORE_TEST_DATABASE_URL pointe le MEME hote ({target_host!r}) que "
            "DATABASE_URL -- probablement la meme instance Postgres managee, meme "
            "avec un nom de base different. RUNBOOK.md section 4 etape 3 exige une "
            "instance isolee distincte. Refus."
        )

    haystack = f"{target_host} {target_db}"
    hit = next((marker for marker in PRODUCTION_DENYLIST if marker in haystack), None)
    if hit:
        raise SafetyError(
            f"RESTORE_TEST_DATABASE_URL contient le marqueur {hit!r} (hote/base : "
            f"{target_host!r}/{target_db!r}), evocateur d'un environnement de "
            "production. Refus categorique -- si c'est un faux positif, renommer "
            "la ressource cible plutot que de contourner ce controle."
        )

    if not any(marker in haystack for marker in safe_markers):
        raise SafetyError(
            f"RESTORE_TEST_DATABASE_URL (hote/base : {target_host!r}/{target_db!r}) "
            f"ne contient aucun marqueur reconnu d'environnement isole "
            f"({', '.join(safe_markers)}). Ajuster --safe-markers si ce nommage est "
            "legitime, ou renommer la ressource cible pour qu'elle porte "
            "explicitement sa nature de cible de restore isolee."
        )

    if not confirm_target:
        raise SafetyError(
            "--confirm-target est requis pour toute execution reelle (--execute) -- "
            f"passer --confirm-target {target_host!r} pour prouver que la cible a "
            "ete nommee explicitement, pas seulement lue depuis une variable "
            "d'environnement qui pourrait etre perimee."
        )

    if confirm_target != target_host:
        raise SafetyError(
            f"--confirm-target {confirm_target!r} ne correspond pas a l'hote reel "
            f"de RESTORE_TEST_DATABASE_URL ({target_host!r}). Refus -- verifier "
            "qu'aucune variable d'environnement perimee n'est en jeu."
        )

    return target_host


# ---------------------------------------------------------------------------
# Rapport
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    name: str
    status: str  # "pass" | "fail" | "skip"
    detail: str
    duration_s: float = 0.0


@dataclass
class RestoreReport:
    started_at: str
    finished_at: str = ""
    dry_run: bool = True
    source_masked: str = ""
    target_masked: str = ""
    checks: list[CheckResult] = field(default_factory=list)
    total_duration_s: float = 0.0

    @property
    def overall_status(self) -> str:
        # Un garde-fou en echec est un vrai probleme a corriger avant --execute,
        # meme en dry-run -- ne jamais le maquiller en "dry_run" (qui suggere a
        # tort "rien d'anormal, juste pas execute"), sous peine de faire sortir
        # ce script en code 0 alors que la cible/configuration est invalide.
        if any(c.status == "fail" for c in self.checks):
            return "failure"
        if self.dry_run:
            return "dry_run"
        return "success"

    def to_dict(self) -> dict:
        return {
            "tool": "verify_backup_restore.py",
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "dry_run": self.dry_run,
            "overall_status": self.overall_status,
            "source": self.source_masked,
            "target": self.target_masked,
            "total_duration_s": round(self.total_duration_s, 2),
            "checks": [
                {
                    "name": c.name,
                    "status": c.status,
                    "detail": c.detail,
                    "duration_s": round(c.duration_s, 2),
                }
                for c in self.checks
            ],
            "disclaimer": (
                "Une execution reussie prouve que CE dump, pris a cet instant, se "
                "restaure et se valide correctement sur la cible indiquee. Elle ne "
                "prouve PAS que les backups automatiques du fournisseur (Railway) "
                "sont actives/fiables (RUNBOOK.md section 4 etape 1, verification "
                "humaine au dashboard) ni qu'une execution future reussira de la "
                "meme maniere."
            ),
        }


def format_report_text(report: RestoreReport) -> str:
    lines = [
        "=== Rapport de verification backup/restore ===",
        f"Debut       : {report.started_at}",
        f"Fin         : {report.finished_at or '(en cours)'}",
        f"Mode        : {'DRY-RUN (aucune commande destructive executee)' if report.dry_run else 'EXECUTION REELLE'}",
        f"Source      : {report.source_masked}",
        f"Cible       : {report.target_masked}",
        f"Duree totale: {report.total_duration_s:.2f}s",
        "",
        "Controles :",
    ]
    for c in report.checks:
        marker = {"pass": "OK", "fail": "ECHEC", "skip": "SKIP"}.get(c.status, c.status.upper())
        lines.append(f"  [{marker}] {c.name} ({c.duration_s:.2f}s) -- {c.detail}")
    lines.append("")
    lines.append(f"RESULTAT GLOBAL : {report.overall_status.upper()}")
    lines.append(
        "Rappel : ceci prouve un dump+restore reussi a cet instant, pas que les "
        "backups automatiques du fournisseur sont actifs/fiables (voir RUNBOOK.md "
        "section 4, etape 1 -- verification humaine requise separement)."
    )
    return "\n".join(lines)


def default_report_path(fmt: str) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ext = "json" if fmt == "json" else "txt"
    return DEFAULT_REPORT_DIR / f"backup_restore_report_{ts}.{ext}"


def assert_report_path_outside_repo(path: Path, allow_in_repo: bool) -> None:
    """[SECURITE] Le rapport (source/cible masquees, mais quand meme des metadonnees
    d'infra) doit pouvoir etre archive hors depot -- refuse par defaut un chemin a
    l'interieur du repo pour eviter qu'il finisse commit par accident."""
    resolved = path.resolve()
    if allow_in_repo:
        return
    try:
        resolved.relative_to(REPO_ROOT)
    except ValueError:
        return
    raise SafetyError(
        f"--report-path ({path}) resout a l'interieur du depot ({REPO_ROOT}) -- "
        "refuse par defaut pour eviter qu'un rapport finisse commit par accident. "
        "Choisir un chemin hors depot, ou passer --allow-in-repo-report si c'est "
        "deliberement voulu (le rapport devra alors etre explicitement exclu de "
        "tout commit)."
    )


# ---------------------------------------------------------------------------
# PostgreSQL -- dump / restore / validation (RUNBOOK.md section 4, etapes 2/4/5)
# ---------------------------------------------------------------------------


def _run(cmd: list[str], *, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=1800)


def postgres_dump(source_url: str, work_dir: Path) -> tuple[Path, CheckResult]:
    if shutil.which("pg_dump") is None:
        return None, CheckResult("postgres_dump", "skip", "pg_dump introuvable dans PATH.")

    dump_path = work_dir / f"postgres_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.dump"
    start = time.monotonic()
    result = _run(["pg_dump", _to_libpq_url(source_url), "-F", "c", "-f", str(dump_path)])
    duration = time.monotonic() - start

    if result.returncode != 0:
        return None, CheckResult(
            "postgres_dump", "fail",
            f"pg_dump a echoue (code {result.returncode}) -- stderr tronque : "
            f"{result.stderr[-300:]}",
            duration,
        )
    size = dump_path.stat().st_size if dump_path.exists() else 0
    if size == 0:
        return None, CheckResult(
            "postgres_dump", "fail",
            "Dump produit mais de taille 0 octet -- probleme de connexion/droits "
            "silencieux (voir RUNBOOK.md section 4 etape 2).",
            duration,
        )
    return dump_path, CheckResult("postgres_dump", "pass", f"Dump de {size} octets produit.", duration)


def postgres_restore(target_url: str, dump_path: Path) -> CheckResult:
    start = time.monotonic()
    result = _run(["pg_restore", "-d", _to_libpq_url(target_url), "--clean", "--if-exists", str(dump_path)])
    duration = time.monotonic() - start

    # pg_restore peut retourner un code non-nul pour des warnings benins (objets
    # absents lors du --clean d'une base vide) -- on inspecte aussi stderr pour de
    # vraies erreurs plutot que de se fier uniquement au code de sortie, tout en
    # remontant le code de sortie dans le detail pour investigation humaine.
    has_error_lines = any("ERROR" in line for line in result.stderr.splitlines())
    if result.returncode != 0 or has_error_lines:
        return CheckResult(
            "postgres_restore", "fail",
            f"pg_restore code={result.returncode}, lignes ERROR presentes={has_error_lines} "
            f"-- stderr tronque : {result.stderr[-500:]}",
            duration,
        )
    return CheckResult("postgres_restore", "pass", "Restauration sans code d'erreur ni ligne ERROR.", duration)


async def postgres_validate(target_url: str) -> CheckResult:
    """RUNBOOK.md section 4 etape 5 -- comptages de sante, pas une comparaison
    exhaustive avec la source (une approximation suffit a detecter un dump vide)."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    start = time.monotonic()
    engine = create_async_engine(target_url, echo=False)
    try:
        async with engine.connect() as conn:
            tenants = (await conn.execute(text("SELECT slug FROM public.tenants"))).all()
            if not tenants:
                return CheckResult(
                    "postgres_validate", "fail",
                    "public.tenants est vide apres restauration -- dump probablement vide/partiel.",
                    time.monotonic() - start,
                )
            sample_slug = tenants[0][0]
            schema = f'"tenant_{sample_slug}"'
            user_count = await conn.scalar(text(f"SELECT count(*) FROM {schema}.users"))
        return CheckResult(
            "postgres_validate", "pass",
            f"{len(tenants)} tenant(s) restaure(s), {user_count} user(s) dans un schema echantillon.",
            time.monotonic() - start,
        )
    except Exception as exc:
        return CheckResult(
            "postgres_validate", "fail",
            f"Erreur de validation ({type(exc).__name__}) -- voir logs locaux pour le detail complet.",
            time.monotonic() - start,
        )
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# MongoDB -- optionnel, jamais faussement PASSED si non configure/disponible
# ---------------------------------------------------------------------------


def mongo_dump(source_url: str, work_dir: Path) -> tuple[Path | None, CheckResult]:
    if shutil.which("mongodump") is None:
        return None, CheckResult(
            "mongo_dump", "skip",
            "mongodump introuvable dans PATH -- section Mongo non executee (pas un succes implicite).",
        )
    dump_dir = work_dir / "mongo_dump"
    start = time.monotonic()
    result = _run(["mongodump", f"--uri={source_url}", f"--out={dump_dir}"])
    duration = time.monotonic() - start
    if result.returncode != 0:
        return None, CheckResult(
            "mongo_dump", "fail",
            f"mongodump a echoue (code {result.returncode}) -- stderr tronque : {result.stderr[-300:]}",
            duration,
        )
    return dump_dir, CheckResult("mongo_dump", "pass", f"Dump ecrit dans {dump_dir.name}/.", duration)


def mongo_restore(target_url: str, dump_dir: Path) -> CheckResult:
    start = time.monotonic()
    result = _run(["mongorestore", f"--uri={target_url}", "--drop", str(dump_dir)])
    duration = time.monotonic() - start
    if result.returncode != 0:
        return CheckResult(
            "mongo_restore", "fail",
            f"mongorestore a echoue (code {result.returncode}) -- stderr tronque : {result.stderr[-500:]}",
            duration,
        )
    return CheckResult("mongo_restore", "pass", "Restauration Mongo sans code d'erreur.", duration)


def assert_safe_mongo_target(source_url: str, target_url: str | None, safe_markers: tuple[str, ...]) -> CheckResult | None:
    """Meme famille de garde-fous que Postgres (host different, pas de marqueur
    prod, marqueur isole present) mais SANS bloquer tout le script si absent --
    Mongo est optionnel (RESTORE_TEST_MONGO_URL peut ne pas etre configure)."""
    if not target_url:
        return CheckResult(
            "mongo_safety_gate", "skip",
            "RESTORE_TEST_MONGO_URL non definie -- section Mongo non executee.",
        )
    if target_url.strip() == source_url.strip():
        return CheckResult("mongo_safety_gate", "fail", "RESTORE_TEST_MONGO_URL identique a MONGO_URL -- refus.")
    source_host = _hostname(source_url)
    target_host = _hostname(target_url)
    target_db = _dbname(target_url)
    if target_host == source_host:
        return CheckResult(
            "mongo_safety_gate", "fail",
            f"RESTORE_TEST_MONGO_URL pointe le meme hote ({target_host!r}) que MONGO_URL -- refus.",
        )
    haystack = f"{target_host} {target_db}"
    hit = next((m for m in PRODUCTION_DENYLIST if m in haystack), None)
    if hit:
        return CheckResult("mongo_safety_gate", "fail", f"Marqueur de production {hit!r} detecte dans la cible Mongo -- refus.")
    if not any(m in haystack for m in safe_markers):
        return CheckResult(
            "mongo_safety_gate", "fail",
            f"Aucun marqueur isole reconnu dans la cible Mongo ({', '.join(safe_markers)}) -- refus.",
        )
    return None  # tous les garde-fous passent


# ---------------------------------------------------------------------------
# Cloudinary -- controle d'INTEGRITE en lecture seule, PAS un restore
# ---------------------------------------------------------------------------


def cloudinary_integrity_check() -> CheckResult:
    """[SECURITE] Lecture seule -- n'appelle jamais destroy/rename/upload. Cloudinary
    est deja le stockage manage source de verite pour les medias : ce controle
    verifie seulement que l'API repond et qu'un echantillon de ressources existe,
    ce n'est PAS un test de backup/restore (aucune notion de restore applicable
    a un stockage manage tiers depuis ce depot)."""
    start = time.monotonic()
    try:
        from app.core.config import settings

        if not settings.cloudinary_cloud_name or not settings.cloudinary_api_key:
            return CheckResult(
                "cloudinary_integrity", "skip",
                "CLOUDINARY_* non configure -- controle non execute.",
                time.monotonic() - start,
            )

        import cloudinary
        import cloudinary.api

        cloudinary.config(
            cloud_name=settings.cloudinary_cloud_name,
            api_key=settings.cloudinary_api_key,
            api_secret=settings.cloudinary_api_secret,
            secure=True,
        )
        result = cloudinary.api.resources(max_results=5)
        count = len(result.get("resources", []))
        return CheckResult(
            "cloudinary_integrity", "pass",
            f"API Cloudinary joignable, {count} ressource(s) echantillonnee(s) "
            "(controle d'integrite en lecture seule -- Cloudinary n'a pas de "
            "notion de backup/restore applicable ici).",
            time.monotonic() - start,
        )
    except Exception as exc:
        return CheckResult(
            "cloudinary_integrity", "fail",
            f"Erreur ({type(exc).__name__}) -- voir logs locaux pour le detail complet.",
            time.monotonic() - start,
        )


# ---------------------------------------------------------------------------
# Redis -- documente comme N/A, jamais simule
# ---------------------------------------------------------------------------


def redis_note() -> CheckResult:
    return CheckResult(
        "redis_backup_restore", "skip",
        "N/A par conception -- les donnees Redis de ce projet (pub/sub WebSocket, "
        "compteurs de rate limit, flags de revocation de session, connexions WS "
        "actives) sont explicitement ephemeres/reconstructibles, pas une source de "
        "verite persistante (voir CLAUDE.md). Aucun test de backup/restore Redis "
        "n'est donc simule ici ; a revoir si un usage Redis persistant critique "
        "est introduit.",
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def run(args: argparse.Namespace) -> RestoreReport:
    from app.core.config import settings

    source_url = args.database_url or settings.database_url
    target_url = args.restore_test_database_url or os.environ.get("RESTORE_TEST_DATABASE_URL")

    report = RestoreReport(
        started_at=datetime.now(timezone.utc).isoformat(),
        dry_run=not args.execute,
        source_masked=mask_url(source_url),
        target_masked=mask_url(target_url) if target_url else "(non definie)",
    )
    overall_start = time.monotonic()

    # --- Garde-fous Postgres (bloquants -- toujours evalues, meme en dry-run,
    # pour que le dry-run soit un aperçu fidele de ce qui bloquerait --execute) ---
    try:
        target_host = assert_safe_restore_target(
            source_url, target_url, args.confirm_target, tuple(args.safe_markers)
        )
        report.checks.append(CheckResult("safety_gate", "pass", f"Cible validee : hote={target_host!r}."))
    except SafetyError as exc:
        report.checks.append(CheckResult("safety_gate", "fail", str(exc)))
        report.finished_at = datetime.now(timezone.utc).isoformat()
        report.total_duration_s = time.monotonic() - overall_start
        return report

    if not args.execute:
        report.checks.append(CheckResult(
            "dry_run_notice", "skip",
            "Mode dry-run (--execute non passe) -- aucune commande destructive "
            "n'a ete executee. Les etapes suivantes auraient ete tentees : "
            "pg_dump (source) -> pg_restore --clean (cible) -> validation.",
        ))
        report.finished_at = datetime.now(timezone.utc).isoformat()
        report.total_duration_s = time.monotonic() - overall_start
        return report

    work_dir = Path(args.work_dir) if args.work_dir else Path(tempfile.mkdtemp(prefix="backup_restore_"))
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        # --- PostgreSQL ---
        dump_path, dump_check = postgres_dump(source_url, work_dir)
        report.checks.append(dump_check)
        if dump_check.status == "pass":
            restore_check = postgres_restore(target_url, dump_path)
            report.checks.append(restore_check)
            if restore_check.status == "pass":
                report.checks.append(await postgres_validate(target_url))

        # --- MongoDB (optionnel) ---
        mongo_source = args.mongo_url or settings.mongo_url
        mongo_target = args.restore_test_mongo_url or os.environ.get("RESTORE_TEST_MONGO_URL")
        mongo_gate_failure = assert_safe_mongo_target(mongo_source, mongo_target, tuple(args.safe_markers))
        if mongo_gate_failure is not None:
            report.checks.append(mongo_gate_failure)
        else:
            mongo_dump_path, mongo_dump_check = mongo_dump(mongo_source, work_dir)
            report.checks.append(mongo_dump_check)
            if mongo_dump_check.status == "pass":
                report.checks.append(mongo_restore(mongo_target, mongo_dump_path))

        # --- Cloudinary (integrite, pas un restore) ---
        if not args.skip_cloudinary:
            report.checks.append(cloudinary_integrity_check())

        # --- Redis (documente, jamais simule) ---
        report.checks.append(redis_note())

    finally:
        if not args.keep_dump:
            shutil.rmtree(work_dir, ignore_errors=True)
        else:
            report.checks.append(CheckResult("dump_retention", "skip", f"--keep-dump : fichiers conserves dans {work_dir}"))

    report.finished_at = datetime.now(timezone.utc).isoformat()
    report.total_duration_s = time.monotonic() - overall_start
    return report


def write_report(report: RestoreReport, path: Path, fmt: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(report.to_dict(), indent=2, ensure_ascii=False) if fmt == "json" else format_report_text(report)
    path.write_text(content, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Verifie la procedure de backup/restore (RUNBOOK.md section 4). "
            "Dry-run par defaut -- passer --execute pour une execution reelle, "
            "apres avoir configure RESTORE_TEST_DATABASE_URL (instance isolee)."
        )
    )
    parser.add_argument("--database-url", default=None, help="Source (defaut : DATABASE_URL).")
    parser.add_argument(
        "--restore-test-database-url", default=None,
        help="Cible Postgres isolee (defaut : variable d'env RESTORE_TEST_DATABASE_URL).",
    )
    parser.add_argument("--mongo-url", default=None, help="Source Mongo (defaut : MONGO_URL).")
    parser.add_argument(
        "--restore-test-mongo-url", default=None,
        help="Cible Mongo isolee (defaut : variable d'env RESTORE_TEST_MONGO_URL). Optionnel.",
    )
    parser.add_argument(
        "--confirm-target", default=None,
        help="Doit correspondre exactement a l'hote resolu de la cible Postgres -- requis pour --execute.",
    )
    parser.add_argument(
        "--safe-markers", nargs="+", default=list(DEFAULT_SAFE_MARKERS),
        help=f"Marqueurs 'environnement isole' reconnus (defaut : {', '.join(DEFAULT_SAFE_MARKERS)}).",
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="Execute reellement dump+restore+validation. Sans ce flag : dry-run (garde-fous evalues, rien d'autre).",
    )
    parser.add_argument("--work-dir", default=None, help="Repertoire pour les dumps temporaires (defaut : dossier tmp, hors depot).")
    parser.add_argument("--keep-dump", action="store_true", help="Conserve les fichiers de dump apres execution (par defaut : supprimes).")
    parser.add_argument("--skip-cloudinary", action="store_true", help="Ignore le controle d'integrite Cloudinary.")
    parser.add_argument("--report-path", default=None, help="Chemin du rapport (defaut : ~/.api-kitchen-backup-reports/, hors depot).")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument(
        "--allow-in-repo-report", action="store_true",
        help="Autorise --report-path a l'interieur du depot (deconseille -- risque de commit accidentel).",
    )
    args = parser.parse_args()

    report_path = Path(args.report_path) if args.report_path else default_report_path(args.format)
    try:
        assert_report_path_outside_repo(report_path, args.allow_in_repo_report)
    except SafetyError as exc:
        print(f"[ERREUR] {exc}", file=sys.stderr)
        sys.exit(2)

    report = asyncio.run(run(args))
    write_report(report, report_path, args.format)

    output = json.dumps(report.to_dict(), indent=2, ensure_ascii=False) if args.format == "json" else format_report_text(report)
    print(output)
    print(f"\nRapport ecrit : {report_path}", file=sys.stderr)

    if report.overall_status == "failure":
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
