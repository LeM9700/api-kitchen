"""Tests de fumée — module HACCP.

Le module HACCP existait uniquement en fichiers non suivis (untracked) avant
ce correctif (voir spec-almost-ready-api-pizza.md). Ce fichier couvre le
chemin heureux de chaque famille de route pour garantir que le router est
bien branche sur l'app (app/main.py) et que les endpoints repondent sans
lever d'exception serveur -- notamment la regression `current_user["sub"]`
(cle inexistante, la bonne cle est "id") qui faisait planter en 500 tout
endpoint d'ecriture avant ce correctif.

L'export PDF (GET /haccp/export/pdf) n'est volontairement pas teste ici :
WeasyPrint necessite les libs systeme Cairo/Pango, indisponibles dans cet
environnement de test Windows. generate_pdf() partage sa collecte de
donnees (_collect_data) avec generate_csv(), deja couvert plus bas.
"""

from datetime import date, datetime, timezone


async def _register_admin(client, slug: str) -> dict:
    resp = await client.post("/api/v1/auth/register", json={
        "tenant_slug": slug,
        "tenant_name": f"Pizzeria {slug}",
        "email": f"admin-{slug}@test.com",
        "password": "Valid1!aa",
    })
    assert resp.status_code == 201, resp.text
    token = resp.json()["access_token"]
    return {"token": token, "headers": {"Authorization": f"Bearer {token}"}}


async def test_haccp_full_happy_path(client, unique_slug):
    """Parcours complet : gate -> session -> equipement -> temperature -> DLC
    -> nettoyage -> non-conformite -> reception -> refroidissement ->
    formation -> huile -> completion de session -> export CSV."""
    slug = f"hc{unique_slug}"
    admin = await _register_admin(client, slug)
    h = admin["headers"]

    # ── Gate bloquant ────────────────────────────────────────────────────
    status_resp = await client.get("/api/v1/haccp/status/today", headers=h)
    assert status_resp.status_code == 200, status_resp.text
    assert status_resp.json()["can_open"] is False

    # ── Session ──────────────────────────────────────────────────────────
    session_resp = await client.post(
        "/api/v1/haccp/sessions", json={"session_type": "opening"}, headers=h
    )
    assert session_resp.status_code == 201, session_resp.text
    session_id = session_resp.json()["id"]

    today_sessions = await client.get("/api/v1/haccp/sessions/today", headers=h)
    assert today_sessions.status_code == 200
    assert any(s["id"] == session_id for s in today_sessions.json())

    # ── Equipement ───────────────────────────────────────────────────────
    equip_resp = await client.post(
        "/api/v1/haccp/equipment",
        json={
            "name": "Frigo principal",
            "type": "fridge",
            "target_min_temp": 0,
            "target_max_temp": 4,
        },
        headers=h,
    )
    assert equip_resp.status_code == 201, equip_resp.text
    equipment_id = equip_resp.json()["id"]

    equip_update = await client.patch(
        f"/api/v1/haccp/equipment/{equipment_id}",
        json={"location": "Cuisine"},
        headers=h,
    )
    assert equip_update.status_code == 200, equip_update.text

    assert (await client.get("/api/v1/haccp/equipment", headers=h)).status_code == 200

    # ── Temperature ──────────────────────────────────────────────────────
    temp_resp = await client.post(
        f"/api/v1/haccp/sessions/{session_id}/temperatures",
        json={"equipment_id": equipment_id, "measured_temp": 3.0},
        headers=h,
    )
    assert temp_resp.status_code == 201, temp_resp.text
    assert temp_resp.json()["is_compliant"] is True

    assert (
        await client.get(f"/api/v1/haccp/sessions/{session_id}/temperatures", headers=h)
    ).status_code == 200

    # ── DLC ──────────────────────────────────────────────────────────────
    dlc_resp = await client.post(
        f"/api/v1/haccp/sessions/{session_id}/dlc",
        json={
            "ingredient_name": "Mozzarella",
            "dlc_level": 2,
            "dlc_date": str(date.today()),
            "is_compliant": True,
        },
        headers=h,
    )
    assert dlc_resp.status_code == 201, dlc_resp.text

    assert (
        await client.get(f"/api/v1/haccp/sessions/{session_id}/dlc", headers=h)
    ).status_code == 200

    # ── Nettoyage ────────────────────────────────────────────────────────
    task_resp = await client.post(
        "/api/v1/haccp/cleaning-tasks",
        json={"name": "Plan de travail", "zone": "Cuisine", "frequency": "daily"},
        headers=h,
    )
    assert task_resp.status_code == 201, task_resp.text
    task_id = task_resp.json()["id"]

    task_update = await client.patch(
        f"/api/v1/haccp/cleaning-tasks/{task_id}",
        json={"product_used": "Desinfectant X"},
        headers=h,
    )
    assert task_update.status_code == 200, task_update.text

    assert (await client.get("/api/v1/haccp/cleaning-tasks", headers=h)).status_code == 200

    cleaning_log = await client.post(
        f"/api/v1/haccp/sessions/{session_id}/cleaning",
        json={"task_id": task_id},
        headers=h,
    )
    assert cleaning_log.status_code == 201, cleaning_log.text

    assert (
        await client.get(f"/api/v1/haccp/sessions/{session_id}/cleaning", headers=h)
    ).status_code == 200

    # ── Non-conformite ───────────────────────────────────────────────────
    nc_resp = await client.post(
        "/api/v1/haccp/non-conformities",
        json={"source_type": "other", "description": "Test NC"},
        headers=h,
    )
    assert nc_resp.status_code == 201, nc_resp.text
    nc_id = nc_resp.json()["id"]

    assert (await client.get("/api/v1/haccp/non-conformities", headers=h)).status_code == 200

    nc_update = await client.patch(
        f"/api/v1/haccp/non-conformities/{nc_id}",
        json={"status": "closed", "corrective_action": "Corrige"},
        headers=h,
    )
    assert nc_update.status_code == 200, nc_update.text

    # ── Reception ────────────────────────────────────────────────────────
    reception_resp = await client.post(
        "/api/v1/haccp/reception-controls",
        json={
            "supplier_name": "Fournisseur A",
            "delivery_date": str(date.today()),
            "temperature_on_arrival": 3.5,
            "packaging_ok": True,
            "labeling_ok": True,
            "dlc_ok": True,
            "is_accepted": True,
        },
        headers=h,
    )
    assert reception_resp.status_code == 201, reception_resp.text

    assert (await client.get("/api/v1/haccp/reception-controls", headers=h)).status_code == 200

    # ── Refroidissement ──────────────────────────────────────────────────
    cooling_resp = await client.post(
        "/api/v1/haccp/cooling",
        json={
            "product_name": "Sauce tomate",
            "temp_start": 65.0,
            "started_at": datetime.now(timezone.utc).isoformat(),
        },
        headers=h,
    )
    assert cooling_resp.status_code == 201, cooling_resp.text
    cooling_id = cooling_resp.json()["id"]

    cooling_update = await client.patch(
        f"/api/v1/haccp/cooling/{cooling_id}",
        json={"temp_final": 8.0},
        headers=h,
    )
    assert cooling_update.status_code == 200, cooling_update.text

    assert (await client.get("/api/v1/haccp/cooling", headers=h)).status_code == 200

    # ── Formation ────────────────────────────────────────────────────────
    training_resp = await client.post(
        "/api/v1/haccp/training",
        json={
            "user_id": 1,
            "training_type": "hygiene_14h",
            "training_date": str(date.today()),
        },
        headers=h,
    )
    assert training_resp.status_code == 201, training_resp.text

    assert (await client.get("/api/v1/haccp/training", headers=h)).status_code == 200

    # ── Huile de friture (optionnel) ─────────────────────────────────────
    oil_resp = await client.post(
        f"/api/v1/haccp/sessions/{session_id}/oil",
        json={"fryer_name": "Friteuse 1", "polarity_percent": 15.0},
        headers=h,
    )
    assert oil_resp.status_code == 201, oil_resp.text

    # ── Completion de session (gate) ─────────────────────────────────────
    complete_resp = await client.patch(
        f"/api/v1/haccp/sessions/{session_id}/complete",
        json={"force": True},
        headers=h,
    )
    assert complete_resp.status_code == 200, complete_resp.text

    final_status = await client.get("/api/v1/haccp/status/today", headers=h)
    assert final_status.status_code == 200
    assert final_status.json()["can_open"] is True

    # ── Export CSV (admin) ───────────────────────────────────────────────
    today = str(date.today())
    csv_resp = await client.get(
        f"/api/v1/haccp/export/csv?from={today}&to={today}&data_type=all",
        headers=h,
    )
    assert csv_resp.status_code == 200, csv_resp.text
    assert csv_resp.headers["content-type"].startswith("text/csv")
    assert "Mozzarella" in csv_resp.text  # ingredient DLC exporte dans la section correspondante
    assert "RELEV" in csv_resp.text  # en-tete section temperature (accents encodes utf-8-sig)

    # ── Stats (admin/staff) ──────────────────────────────────────────────
    stats_resp = await client.get(
        f"/api/v1/haccp/stats?from={today}&to={today}",
        headers=h,
    )
    assert stats_resp.status_code == 200, stats_resp.text
    assert 0.0 <= stats_resp.json()["overall_score"] <= 100.0


async def test_haccp_status_requires_auth(client):
    resp = await client.get("/api/v1/haccp/status/today")
    assert resp.status_code in (401, 403)


async def test_haccp_equipment_create_requires_valid_auth(client):
    resp = await client.post(
        "/api/v1/haccp/equipment",
        json={"name": "Frigo", "type": "fridge"},
        headers={"Authorization": "Bearer invalid-token"},
    )
    assert resp.status_code in (401, 403)
