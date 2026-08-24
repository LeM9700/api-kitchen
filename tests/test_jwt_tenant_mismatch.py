"""Test d'isolation multi-tenant : un token JWT valide (signature correcte)
mais dont le claim ``tenant_slug`` ne correspond pas au tenant reel de
l'utilisateur ``sub`` -- est-il rejete ?

Contexte (voir rapport d'audit d'isolation multi-tenant) :
    ``get_current_user()`` (app/core/http/deps.py) construisait
    ``current_user["tenant_slug"]`` directement depuis le claim JWT et
    l'utilisait pour choisir le schema Postgres actif (``get_tenant_session``),
    sans jamais verifier que le ``sub`` appartient reellement a ce tenant
    (pas de verification que ``sub`` existe bien dans
    ``tenant_{tenant_slug}.users``). Corrige par
    ``app.core.tenancy.tenant.user_belongs_to_tenant``, appele depuis
    ``get_current_user`` (HTTP) et ``notifications.ws_router.notifications_ws``
    (WebSocket).

    Un attaquant externe ne peut pas forger un tel token sans connaitre
    ``settings.jwt_secret`` (HMAC-SHA256) -- ce n'est donc pas un vecteur
    d'exploitation direct. Ce test documente plutot l'ABSENCE de defense en
    profondeur : si un token de ce type est un jour emis par erreur (bug de
    mint, confusion de contexte ailleurs dans le code, token vole et
    rejoue avec un tenant_slug modifie server-side par un bug), rien ne
    l'intercepte aujourd'hui.

Ce fichier ne modifie aucun code applicatif. Utilise
``create_access_token()`` (la vraie fonction de signature de l'app, pas une
reimplementation) pour produire un token dont la signature est authentique
-- seul le CONTENU du payload est incoherent, pas la signature.
"""

from app.core.auth.security import create_access_token


async def _register_tenant(client, slug: str, label: str) -> dict:
    email = f"{label}-{slug}@test.com"
    resp = await client.post("/api/v1/auth/register", json={
        "tenant_slug": slug,
        "tenant_name": f"Pizzeria {label}",
        "email": email,
        "password": "Valid1!aa",
    })
    assert resp.status_code == 201, resp.text
    token = resp.json()["access_token"]

    me = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert me.status_code == 200, me.text

    return {
        "token": token,
        "user_id": int(me.json()["id"]),
        "email": me.json()["email"],
        "slug": slug,
    }


async def test_token_minted_for_tenant_a_user_but_claiming_tenant_b_is_rejected(
    client, unique_slug
):
    """Un token dont la signature est authentique, mais dont le ``sub`` est
    l'id d'un utilisateur du tenant A alors que ``tenant_slug`` pointe vers
    le tenant B, ne doit jamais etre traite comme une session valide pour un
    utilisateur du tenant B.

    Risque concret si ce test echoue : comme les identifiants utilisateur
    sont attribues par une sequence propre a chaque schema tenant (le
    premier utilisateur cree dans n'importe quel tenant recoit id=1), le
    ``sub`` du tenant A coincide tres probablement avec un ``sub`` existant
    et valide dans le tenant B -- le serveur traiterait alors la requete
    comme si elle emanait de CET utilisateur B precis (identite usurpee),
    sans jamais avoir verifie que le titulaire original du token appartient
    reellement a B.
    """
    slug_a = f"ja{unique_slug}"
    slug_b = f"jb{unique_slug}"

    tenant_a = await _register_tenant(client, slug_a, "a")
    tenant_b = await _register_tenant(client, slug_b, "b")

    # Token dont la signature est authentique (mint via la vraie fonction
    # applicative) mais dont le payload est incoherent : sub = utilisateur
    # reel du tenant A, tenant_slug = tenant B.
    forged_payload = {
        "sub": str(tenant_a["user_id"]),
        "email": tenant_a["email"],
        "role": "admin",
        "permissions": None,
        "tenant_id": 0,
        "tenant_slug": slug_b,
        "must_change_password": False,
    }
    forged_token = create_access_token(forged_payload)

    resp = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {forged_token}"},
    )

    assert resp.status_code == 401, (
        f"RISQUE CONFIRME : un token signe pour l'utilisateur du tenant A "
        f"mais reclamant le tenant B a recu le statut {resp.status_code} "
        f"(corps : {resp.text}) au lieu d'un rejet 401 -- aucune "
        f"revalidation serveur de l'appartenance utilisateur/tenant "
        f"n'intercepte cette incoherence."
    )
    assert tenant_b["email"] not in resp.text, (
        "RISQUE CONFIRME : la reponse contient les donnees reelles d'un "
        "utilisateur du tenant B, obtenues via un token mint pour un "
        "utilisateur du tenant A."
    )
