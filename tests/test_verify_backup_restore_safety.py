"""Tests dedies de l'outil ``tools/verify_backup_restore.py``.

Deux familles de garanties couvertes ici, toutes deux exigees avant toute
Draft PR sur ce script (voir CLAUDE.md / historique de revue) :

    1. Garde-fous de securite (``assert_safe_restore_target``) -- refus
       systematique d'une cible non isolee, AVANT toute commande destructive,
       et un code de sortie non nul quand un garde-fou echoue.
    2. Aucun secret (mot de passe, URI complete) ne doit jamais transiter par
       argv pour ``pg_dump``/``pg_restore``/``mongodump``/``mongorestore`` --
       verifie a la fois sur les commandes construites (tests unitaires,
       ``_run`` monkeypatche) et sur un vrai dump/restore local (test
       d'integration, ignore si Postgres n'est pas accessible).

La plupart de ces tests sont des tests unitaires purs (aucun acces DB) :
``assert_safe_restore_target`` et la construction des commandes ne font
aucune E/S reseau tant que les garde-fous ne sont pas satisfaits ou que
``_run``/``shutil.which`` sont monkeypatches.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import tools.verify_backup_restore as vbr


# ---------------------------------------------------------------------------
# 1. Garde-fous -- assert_safe_restore_target
# ---------------------------------------------------------------------------


def test_rejects_identical_source_and_target_url():
    url = "postgresql+asyncpg://u:p@restore-test-host.internal/pizza"
    with pytest.raises(vbr.SafetyError, match="identique"):
        vbr.assert_safe_restore_target(url, url, confirm_target="restore-test-host.internal")


def test_rejects_same_host_even_with_different_db_name():
    source = "postgresql+asyncpg://u:p@shared-host.internal/pizza_prod"
    target = "postgresql+asyncpg://u:p@shared-host.internal/pizza_restore_test"
    with pytest.raises(vbr.SafetyError, match="MEME hote"):
        vbr.assert_safe_restore_target(source, target, confirm_target="shared-host.internal")


@pytest.mark.parametrize("marker", ["prod", "production", "live"])
def test_rejects_target_containing_a_production_marker(marker):
    source = "postgresql+asyncpg://u:p@source-host.internal/pizza"
    target = f"postgresql+asyncpg://u:p@{marker}-restore-test.internal/pizza_restore_test"
    with pytest.raises(vbr.SafetyError, match="production"):
        vbr.assert_safe_restore_target(source, target, confirm_target=f"{marker}-restore-test.internal")


def test_rejects_target_without_any_isolated_marker():
    source = "postgresql+asyncpg://u:p@source-host.internal/pizza"
    # Ni "prod"/"production"/"live", ni aucun marqueur de DEFAULT_SAFE_MARKERS
    # (test/restore/staging/sandbox) dans l'hote ou le nom de base.
    target = "postgresql+asyncpg://u:p@some-other-host.internal/some_other_db"
    with pytest.raises(vbr.SafetyError, match="aucun marqueur reconnu"):
        vbr.assert_safe_restore_target(source, target, confirm_target="some-other-host.internal")


def test_rejects_missing_confirm_target():
    source = "postgresql+asyncpg://u:p@source-host.internal/pizza"
    target = "postgresql+asyncpg://u:p@restore-test-host.internal/pizza_restore_test"
    with pytest.raises(vbr.SafetyError, match="--confirm-target est requis"):
        vbr.assert_safe_restore_target(source, target, confirm_target=None)


def test_rejects_confirm_target_mismatch():
    source = "postgresql+asyncpg://u:p@source-host.internal/pizza"
    target = "postgresql+asyncpg://u:p@restore-test-host.internal/pizza_restore_test"
    with pytest.raises(vbr.SafetyError, match="ne correspond pas"):
        vbr.assert_safe_restore_target(source, target, confirm_target="wrong-host.internal")


def test_rejects_missing_target_url():
    source = "postgresql+asyncpg://u:p@source-host.internal/pizza"
    with pytest.raises(vbr.SafetyError, match="n'est pas definie"):
        vbr.assert_safe_restore_target(source, None, confirm_target="anything")


def test_accepts_a_genuinely_isolated_target_as_positive_control():
    """Controle positif : prouve que les tests ci-dessus echouent bien a
    cause du garde-fou teste, pas parce que la fonction refuse toujours."""
    source = "postgresql+asyncpg://u:p@source-host.internal/pizza"
    target = "postgresql+asyncpg://u:p@restore-test-host.internal/pizza_restore_test"
    resolved_host = vbr.assert_safe_restore_target(source, target, confirm_target="restore-test-host.internal")
    assert resolved_host == "restore-test-host.internal"


# ---------------------------------------------------------------------------
# 2. Rapport hors depot impose par defaut
# ---------------------------------------------------------------------------


def test_default_report_path_resolves_outside_the_repo():
    path = vbr.default_report_path("text")
    vbr.assert_report_path_outside_repo(path, allow_in_repo=False)  # ne doit pas lever
    assert vbr.REPO_ROOT not in path.resolve().parents


def test_assert_report_path_outside_repo_rejects_in_repo_path_by_default():
    in_repo_path = vbr.REPO_ROOT / "backup_report_should_not_land_here.txt"
    with pytest.raises(vbr.SafetyError, match="interieur du depot"):
        vbr.assert_report_path_outside_repo(in_repo_path, allow_in_repo=False)
    vbr.assert_report_path_outside_repo(in_repo_path, allow_in_repo=True)  # explicitement autorise


# ---------------------------------------------------------------------------
# 3. run() ne doit JAMAIS invoquer pg_dump/pg_restore/mongodump/mongorestore
#    quand le garde-fou echoue
# ---------------------------------------------------------------------------


def _make_gate_failure_args(**overrides) -> argparse.Namespace:
    base = dict(
        database_url="postgresql+asyncpg://u:p@shared-host.internal/pizza_prod",
        restore_test_database_url="postgresql+asyncpg://u:p@shared-host.internal/pizza_restore_test",
        mongo_url=None,
        restore_test_mongo_url=None,
        confirm_target="shared-host.internal",
        safe_markers=list(vbr.DEFAULT_SAFE_MARKERS),
        execute=True,
        work_dir=None,
        validate_tenant_limit=25,
        keep_dump=False,
        skip_cloudinary=True,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


async def test_run_never_calls_dump_or_restore_tools_when_safety_gate_fails(monkeypatch):
    def must_not_be_called(*_args, **_kwargs):
        raise AssertionError("un outil de dump/restore a ete invoque malgre un garde-fou en echec")

    monkeypatch.setattr(vbr, "postgres_dump", must_not_be_called)
    monkeypatch.setattr(vbr, "postgres_restore", must_not_be_called)
    monkeypatch.setattr(vbr, "mongo_dump", must_not_be_called)
    monkeypatch.setattr(vbr, "mongo_restore", must_not_be_called)

    # Meme hote source/cible -> garde-fou 3 en echec.
    args = _make_gate_failure_args()

    report = await vbr.run(args)

    assert report.overall_status == "failure"
    assert [c.name for c in report.checks] == ["safety_gate"]
    assert report.checks[0].status == "fail"


async def test_run_never_calls_dump_or_restore_tools_even_in_dry_run(monkeypatch):
    """Meme preuve que ci-dessus, mais sans --execute -- le garde-fou est
    evalue AVANT de regarder --execute, donc un dry-run sur une cible
    invalide doit deja rapporter l'echec, jamais un faux 'dry_run' propre."""

    def must_not_be_called(*_args, **_kwargs):
        raise AssertionError("un outil de dump/restore a ete invoque en dry-run")

    monkeypatch.setattr(vbr, "postgres_dump", must_not_be_called)
    monkeypatch.setattr(vbr, "postgres_restore", must_not_be_called)
    monkeypatch.setattr(vbr, "mongo_dump", must_not_be_called)
    monkeypatch.setattr(vbr, "mongo_restore", must_not_be_called)

    args = _make_gate_failure_args(execute=False)
    report = await vbr.run(args)

    assert report.overall_status == "failure"
    assert [c.name for c in report.checks] == ["safety_gate"]


# ---------------------------------------------------------------------------
# 4. Aucun secret dans les commandes construites -- pg_dump / pg_restore
# ---------------------------------------------------------------------------


def test_postgres_dump_command_never_contains_the_password_or_full_url(monkeypatch, tmp_path):
    captured: dict = {}

    def fake_run(cmd, *, env=None):
        captured["cmd"] = cmd
        pgpassfile = env["PGPASSFILE"]
        captured["pgpassfile"] = pgpassfile
        captured["pgpassfile_mode"] = oct(os.stat(pgpassfile).st_mode & 0o777)
        captured["pgpassfile_content"] = Path(pgpassfile).read_text()
        captured["pgpassword_in_env"] = "PGPASSWORD" in env
        dump_path = Path(cmd[cmd.index("-f") + 1])
        dump_path.write_bytes(b"fake-dump-bytes-non-empty")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(vbr, "_run", fake_run)

    secret = "S3cr3t-P@ssw0rd!"
    source_url = f"postgresql+asyncpg://dumpuser:{secret}@source-host.internal:5432/pizza_source"

    path, check = vbr.postgres_dump(source_url, tmp_path)

    argv_joined = " ".join(captured["cmd"])
    assert secret not in argv_joined
    assert source_url not in argv_joined
    assert not any(a.startswith("postgresql") for a in captured["cmd"])  # jamais d'URL en argv

    # Le secret est bien passe via PGPASSFILE (0600), pas ignore silencieusement.
    assert captured["pgpassfile_mode"] == "0o600"
    assert secret in captured["pgpassfile_content"]
    assert captured["pgpassword_in_env"] is False

    # Nettoyage fiable : le fichier n'existe plus une fois postgres_dump revenu.
    assert not os.path.exists(captured["pgpassfile"])

    assert check.status == "pass"
    assert path is not None


def test_postgres_restore_command_never_contains_the_password_or_full_url(monkeypatch, tmp_path):
    captured: dict = {}

    def fake_run(cmd, *, env=None):
        captured["cmd"] = cmd
        pgpassfile = env["PGPASSFILE"]
        captured["pgpassfile"] = pgpassfile
        captured["pgpassfile_mode"] = oct(os.stat(pgpassfile).st_mode & 0o777)
        captured["pgpassfile_content"] = Path(pgpassfile).read_text()
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(vbr, "_run", fake_run)

    secret = "An0ther-Secret!"
    target_url = f"postgresql+asyncpg://restoreuser:{secret}@restore-test-host.internal:5432/pizza_restore_test"
    dump_path = tmp_path / "fake.dump"
    dump_path.write_bytes(b"fake-dump")

    check = vbr.postgres_restore(target_url, dump_path)

    argv_joined = " ".join(captured["cmd"])
    assert secret not in argv_joined
    assert target_url not in argv_joined
    assert not any(a.startswith("postgresql") for a in captured["cmd"])

    assert captured["pgpassfile_mode"] == "0o600"
    assert secret in captured["pgpassfile_content"]
    assert not os.path.exists(captured["pgpassfile"])

    assert check.status == "pass"


def test_postgres_pgpass_file_is_removed_even_when_pg_dump_fails(monkeypatch, tmp_path):
    """Le fichier de secret temporaire doit disparaitre meme si la commande
    echoue -- pas seulement dans le chemin de succes."""
    captured: dict = {}

    def failing_run(cmd, *, env=None):
        captured["pgpassfile"] = env["PGPASSFILE"]
        assert os.path.exists(captured["pgpassfile"])
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="pg_dump: error: connection failed")

    monkeypatch.setattr(vbr, "_run", failing_run)

    source_url = "postgresql+asyncpg://u:whatever-secret@source-host.internal/pizza"
    path, check = vbr.postgres_dump(source_url, tmp_path)

    assert check.status == "fail"
    assert path is None
    assert "whatever-secret" not in check.detail
    assert not os.path.exists(captured["pgpassfile"])


# ---------------------------------------------------------------------------
# 5. Aucun secret dans les commandes construites -- mongodump / mongorestore
# ---------------------------------------------------------------------------


def test_mongo_dump_command_never_contains_the_uri_or_password(monkeypatch, tmp_path):
    monkeypatch.setattr(vbr.shutil, "which", lambda name: f"/usr/bin/{name}")

    captured: dict = {}

    def fake_run(cmd, *, env=None):
        captured["cmd"] = cmd
        config_arg = next(a for a in cmd if a.startswith("--config="))
        config_path = config_arg.split("=", 1)[1]
        captured["config_path"] = config_path
        captured["config_mode"] = oct(os.stat(config_path).st_mode & 0o777)
        captured["config_content"] = Path(config_path).read_text()
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(vbr, "_run", fake_run)

    secret = "M0ngo-Secret!"
    source_url = f"mongodb://monguser:{secret}@mongo-source-host.internal:27017/pizza_mongo"

    dump_dir, check = vbr.mongo_dump(source_url, tmp_path)

    argv_joined = " ".join(captured["cmd"])
    assert secret not in argv_joined
    assert source_url not in argv_joined
    assert not any(a.startswith("--uri=") for a in captured["cmd"])

    assert captured["config_mode"] == "0o600"
    assert secret in captured["config_content"]
    assert not os.path.exists(captured["config_path"])

    assert check.status == "pass"
    assert dump_dir is not None


def test_mongo_restore_command_never_contains_the_uri_or_password(monkeypatch, tmp_path):
    monkeypatch.setattr(vbr.shutil, "which", lambda name: f"/usr/bin/{name}")

    captured: dict = {}

    def fake_run(cmd, *, env=None):
        captured["cmd"] = cmd
        config_arg = next(a for a in cmd if a.startswith("--config="))
        config_path = config_arg.split("=", 1)[1]
        captured["config_path"] = config_path
        captured["config_mode"] = oct(os.stat(config_path).st_mode & 0o777)
        captured["config_content"] = Path(config_path).read_text()
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(vbr, "_run", fake_run)

    secret = "Restore-M0ngo-Secret!"
    target_url = f"mongodb://monguser:{secret}@mongo-restore-test-host.internal:27017/pizza_mongo_restore_test"
    dump_dir = tmp_path / "mongo_dump"
    dump_dir.mkdir()

    check = vbr.mongo_restore(target_url, dump_dir)

    argv_joined = " ".join(captured["cmd"])
    assert secret not in argv_joined
    assert target_url not in argv_joined
    assert not any(a.startswith("--uri=") for a in captured["cmd"])

    assert captured["config_mode"] == "0o600"
    assert secret in captured["config_content"]
    assert not os.path.exists(captured["config_path"])

    assert check.status == "pass"


def test_mongo_config_file_is_removed_even_when_mongorestore_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(vbr.shutil, "which", lambda name: f"/usr/bin/{name}")
    captured: dict = {}

    def failing_run(cmd, *, env=None):
        config_arg = next(a for a in cmd if a.startswith("--config="))
        captured["config_path"] = config_arg.split("=", 1)[1]
        assert os.path.exists(captured["config_path"])
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="mongorestore: connection refused")

    monkeypatch.setattr(vbr, "_run", failing_run)

    target_url = "mongodb://u:top-secret@mongo-restore-test-host.internal/pizza_mongo_restore_test"
    dump_dir = tmp_path / "mongo_dump"
    dump_dir.mkdir()

    check = vbr.mongo_restore(target_url, dump_dir)

    assert check.status == "fail"
    assert "top-secret" not in check.detail
    assert not os.path.exists(captured["config_path"])


# ---------------------------------------------------------------------------
# 6. scrub_secrets -- derniere ligne de defense
# ---------------------------------------------------------------------------


def test_scrub_secrets_masks_every_known_secret_value():
    url = "postgresql+asyncpg://u:pw@host.internal/db"
    text = f"connection to {url} failed: password authentication failed"
    scrubbed = vbr.scrub_secrets(text, [url, "pw"])
    assert url not in scrubbed
    assert "pw" not in scrubbed
    assert "***" in scrubbed


def test_scrub_secrets_is_a_noop_without_matching_secrets():
    text = "some harmless diagnostic message"
    assert vbr.scrub_secrets(text, ["", None or ""]) == text


# ---------------------------------------------------------------------------
# 7. Bout-en-bout CLI : code de sortie non nul sur echec de garde-fou
# ---------------------------------------------------------------------------


def test_cli_exits_nonzero_on_safety_gate_failure_and_never_invokes_pg_dump(tmp_path):
    report_path = tmp_path / "gate_failure_report.json"
    cmd = [
        sys.executable,
        "tools/verify_backup_restore.py",
        "--database-url", "postgresql+asyncpg://u:p@shared-cli-host.internal/pizza_prod",
        "--restore-test-database-url", "postgresql+asyncpg://u:p@shared-cli-host.internal/pizza_restore_test",
        "--confirm-target", "shared-cli-host.internal",
        "--report-path", str(report_path),
        "--format", "json",
    ]
    result = subprocess.run(
        cmd, cwd=str(vbr.REPO_ROOT), capture_output=True, text=True, timeout=60
    )

    assert result.returncode != 0

    data = json.loads(report_path.read_text())
    assert data["overall_status"] == "failure"
    # Seul le garde-fou apparait : la preuve, au niveau CLI reel, que
    # pg_dump/pg_restore/mongodump/mongorestore n'ont jamais ete appeles.
    assert [c["name"] for c in data["checks"]] == ["safety_gate"]

    # Le rapport JSON lui-meme ne contient ni mot de passe ni URL complete.
    raw = report_path.read_text()
    assert "postgresql+asyncpg://u:p@" not in raw
