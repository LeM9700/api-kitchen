from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
import contextlib

import pytest
from pydantic import ValidationError

from app.core.http.errors import AppError
from app.modules.kds import service
from app.modules.kds.models import KdsPairingCode, KdsRemoteSession, KdsScreen
from app.modules.kds.schemas import KdsPairRequest, KdsScreenCreate, KdsScreenUpdate


class _Result:
    def __init__(self, rows=None, rowcount: int = 0):
        self._rows = rows or []
        self.rowcount = rowcount

    def scalars(self):
        return self

    def all(self):
        return self._rows


def _screen(**overrides) -> KdsScreen:
    data = {
        "id": 12,
        "name": "Cuisine principale",
        "screen_key": "kitchen-main",
        "mode": "kitchen",
        "station": "kitchen",
        "interaction_mode": "wall",
        "tickets_per_page": 4,
        "is_active": True,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    data.update(overrides)
    return KdsScreen(**data)


def _pairing(**overrides) -> KdsPairingCode:
    data = {
        "id": 1,
        "screen_id": 12,
        "code_hash": service.hash_pairing_code("acme", "482731"),
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
        "used_at": None,
        "created_by_user_id": 7,
        "created_at": datetime.now(timezone.utc),
    }
    data.update(overrides)
    return KdsPairingCode(**data)


def _remote(**overrides) -> KdsRemoteSession:
    data = {
        "id": 5,
        "session_token_hash": service.hash_remote_session_token("acme", "plain-token"),
        "screen_id": 12,
        "paired_by_user_id": 7,
        "device_label": "iPhone Malik",
        "created_at": datetime.now(timezone.utc),
        "last_seen_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=12),
        "revoked_at": None,
    }
    data.update(overrides)
    return KdsRemoteSession(**data)


def test_kds_screen_schema_accepts_custom_station():
    body = KdsScreenCreate(
        name="Cuisine principale",
        screen_key="kitchen-main",
        mode="kitchen",
        station="oven-1",
        interaction_mode="touch",
        tickets_per_page=6,
    )

    assert body.station == "oven-1"


def test_kds_screen_schema_rejects_invalid_mode():
    with pytest.raises(ValidationError):
        KdsScreenCreate(name="X", screen_key="x", mode="remote")


def test_kds_screen_schema_rejects_invalid_interaction_mode():
    with pytest.raises(ValidationError):
        KdsScreenCreate(name="X", screen_key="x", interaction_mode="remote")


def test_kds_screen_schema_rejects_blank_station():
    with pytest.raises(ValidationError):
        KdsScreenCreate(name="X", screen_key="x", station="   ")


def test_kds_pair_request_keeps_code_as_six_digit_string():
    body = KdsPairRequest(code="004281", device_label=" iPhone Malik ")

    assert body.code == "004281"
    assert body.device_label == "iPhone Malik"


def test_kds_models_are_tenant_scoped_without_tenant_id():
    assert "tenant_id" not in KdsScreen.__table__.columns
    assert "tenant_id" not in KdsPairingCode.__table__.columns
    assert "tenant_id" not in KdsRemoteSession.__table__.columns


def test_kds_openapi_paths_are_registered_and_do_not_expose_hashes():
    from app.main import app

    schema = app.openapi()
    paths = set(schema["paths"])

    assert "/api/v1/kds/screens" in paths
    assert "/api/v1/kds/screens/{screen_id}/pairing-code" in paths
    assert "/api/v1/kds/pair" in paths
    assert "/api/v1/kds/remote/session" in paths
    assert "/api/v1/kds/remote/session/revoke" in paths
    assert "/api/v1/kds/screens/{screen_id}/revoke-sessions" in paths
    serialized = str(schema)
    assert "code_hash" not in serialized
    assert "session_token_hash" not in serialized


async def test_list_screens_returns_active_screen_rows():
    screen = _screen()
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_Result([screen]))

    result = await service.list_screens(session)

    assert result == [screen]


async def test_admin_creates_kitchen_screen():
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=None)
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    created = await service.create_screen(
        session,
        KdsScreenCreate(name="Cuisine principale", screen_key="kitchen-main"),
    )

    assert created.name == "Cuisine principale"
    assert created.mode == "kitchen"
    assert created.station == "kitchen"
    assert created.interaction_mode == "wall"
    session.commit.assert_awaited_once()


async def test_screen_key_unique_is_enforced_before_insert():
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=99)
    session.commit = AsyncMock()

    with pytest.raises(AppError) as exc_info:
        await service.create_screen(session, KdsScreenCreate(name="Cuisine", screen_key="kitchen-main"))

    assert exc_info.value.code == "KDS_SCREEN_KEY_ALREADY_EXISTS"
    assert exc_info.value.status_code == 409
    session.commit.assert_not_called()


async def test_update_screen_rejects_duplicate_screen_key():
    session = AsyncMock()
    session.get = AsyncMock(return_value=_screen(screen_key="kitchen-main"))
    session.scalar = AsyncMock(return_value=8)

    with pytest.raises(AppError) as exc_info:
        await service.update_screen(session, 12, KdsScreenUpdate(screen_key="counter-main"))

    assert exc_info.value.code == "KDS_SCREEN_KEY_ALREADY_EXISTS"


async def test_staff_without_admin_cannot_create_screen(client):
    from app.core.http.deps import get_current_user
    from app.main import app

    async def _staff():
        return {"id": "7", "tenant_slug": "acme", "role": "staff", "permissions": ["orders:preparation"]}

    app.dependency_overrides[get_current_user] = _staff
    try:
        response = await client.post("/api/v1/kds/screens", json={"name": "Cuisine", "screen_key": "kitchen"})
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 403


async def test_staff_preparation_can_list_screens(client, monkeypatch):
    from app.core.http.deps import get_current_user
    from app.main import app
    from app.modules.kds import router as kds_router

    async def _staff():
        return {"id": "7", "tenant_slug": "acme", "role": "staff", "permissions": ["orders:preparation"]}

    @contextlib.asynccontextmanager
    async def _tenant_session(_tenant_slug):
        yield object()

    monkeypatch.setattr(kds_router, "get_tenant_session", _tenant_session)
    monkeypatch.setattr(kds_router.service, "list_screens", AsyncMock(return_value=[_screen()]))
    app.dependency_overrides[get_current_user] = _staff
    try:
        response = await client.get("/api/v1/kds/screens")
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 200
    assert response.json()[0]["screen_key"] == "kitchen-main"


async def test_create_pairing_code_returns_six_digits_and_stores_only_hash(monkeypatch):
    session = AsyncMock()
    session.get = AsyncMock(return_value=_screen())
    session.scalar = AsyncMock(return_value=None)
    session.execute = AsyncMock(return_value=_Result())
    session.add = MagicMock()
    session.commit = AsyncMock()
    monkeypatch.setattr(service, "_generate_pairing_code", lambda: "004281")

    result = await service.create_pairing_code(
        session,
        tenant_slug="acme",
        screen_id=12,
        created_by_user_id=7,
    )

    pairing = session.add.call_args.args[0]
    assert result["code"] == "004281"
    assert result["code"].isdigit()
    assert len(result["code"]) == 6
    assert pairing.code_hash != "004281"
    assert len(pairing.code_hash) == 64


async def test_new_pairing_code_invalidates_previous_active_code(monkeypatch):
    session = AsyncMock()
    session.get = AsyncMock(return_value=_screen())
    session.scalar = AsyncMock(return_value=None)
    session.execute = AsyncMock(return_value=_Result())
    session.add = MagicMock()
    session.commit = AsyncMock()
    monkeypatch.setattr(service, "_generate_pairing_code", lambda: "111222")

    await service.create_pairing_code(
        session,
        tenant_slug="acme",
        screen_id=12,
        created_by_user_id=7,
    )

    stmt = str(session.execute.await_args.args[0])
    assert "UPDATE kds_pairing_codes" in stmt
    assert "used_at" in stmt


async def test_create_pairing_code_rejects_inactive_screen():
    session = AsyncMock()
    session.get = AsyncMock(return_value=_screen(is_active=False))

    with pytest.raises(AppError) as exc_info:
        await service.create_pairing_code(
            session,
            tenant_slug="acme",
            screen_id=12,
            created_by_user_id=7,
        )

    assert exc_info.value.code == "KDS_SCREEN_INACTIVE"


async def test_create_pairing_code_rejects_missing_screen():
    session = AsyncMock()
    session.get = AsyncMock(return_value=None)

    with pytest.raises(AppError) as exc_info:
        await service.create_pairing_code(
            session,
            tenant_slug="acme",
            screen_id=404,
            created_by_user_id=7,
        )

    assert exc_info.value.code == "KDS_SCREEN_NOT_FOUND"


async def test_pair_remote_valid_code_creates_session_and_returns_raw_token(monkeypatch):
    pairing = _pairing()
    screen = _screen()
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_Result([pairing]))
    session.get = AsyncMock(return_value=screen)
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    monkeypatch.setattr(service, "_generate_unique_remote_token", AsyncMock(return_value=("raw-token", "hash-token")))

    result = await service.pair_remote(
        session,
        tenant_slug="acme",
        code="482731",
        paired_by_user_id=7,
        device_label="iPhone Malik",
    )

    remote = session.add.call_args.args[0]
    assert result["session_token"] == "raw-token"
    assert remote.session_token_hash == "hash-token"
    assert remote.session_token_hash != result["session_token"]
    assert remote.expires_at - remote.created_at == service.REMOTE_SESSION_TTL
    assert result["screen"]["screen_key"] == "kitchen-main"


async def test_pair_remote_locks_pairing_code_row_for_concurrency(monkeypatch):
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_Result([_pairing()]))
    session.get = AsyncMock(return_value=_screen())
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    monkeypatch.setattr(service, "_generate_unique_remote_token", AsyncMock(return_value=("raw-token", "hash-token")))

    await service.pair_remote(session, tenant_slug="acme", code="482731", paired_by_user_id=7)

    stmt = str(session.execute.await_args.args[0])
    assert "FOR UPDATE" in stmt


async def test_pair_remote_marks_code_used_before_commit(monkeypatch):
    pairing = _pairing()
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_Result([pairing]))
    session.get = AsyncMock(return_value=_screen())
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    monkeypatch.setattr(service, "_generate_unique_remote_token", AsyncMock(return_value=("raw-token", "hash-token")))

    await service.pair_remote(session, tenant_slug="acme", code="482731", paired_by_user_id=7)

    assert pairing.used_at is not None
    session.commit.assert_awaited_once()


async def test_pair_remote_reuses_same_code_conflicts():
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_Result([_pairing(used_at=datetime.now(timezone.utc))]))

    with pytest.raises(AppError) as exc_info:
        await service.pair_remote(session, tenant_slug="acme", code="482731", paired_by_user_id=7)

    assert exc_info.value.code == "PAIRING_CODE_ALREADY_USED"
    assert exc_info.value.status_code == 409


async def test_pair_remote_expired_code_is_refused():
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=_Result([_pairing(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))])
    )

    with pytest.raises(AppError) as exc_info:
        await service.pair_remote(session, tenant_slug="acme", code="482731", paired_by_user_id=7)

    assert exc_info.value.code == "PAIRING_CODE_EXPIRED"


async def test_pair_remote_invalid_code_is_refused():
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_Result([]))

    with pytest.raises(AppError) as exc_info:
        await service.pair_remote(session, tenant_slug="acme", code="000000", paired_by_user_id=7)

    assert exc_info.value.code == "PAIRING_CODE_INVALID"


async def test_pair_remote_inactive_screen_is_refused():
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_Result([_pairing()]))
    session.get = AsyncMock(return_value=_screen(is_active=False))

    with pytest.raises(AppError) as exc_info:
        await service.pair_remote(session, tenant_slug="acme", code="482731", paired_by_user_id=7)

    assert exc_info.value.code == "KDS_SCREEN_INACTIVE"


async def test_resolve_remote_session_valid_updates_last_seen():
    remote = _remote()
    previous_last_seen = remote.last_seen_at
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=remote)
    session.get = AsyncMock(return_value=_screen())
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    result = await service.resolve_remote_session(session, tenant_slug="acme", token="plain-token")

    assert result["id"] == remote.id
    assert result["screen"]["id"] == 12
    assert remote.last_seen_at >= previous_last_seen
    session.commit.assert_awaited_once()


async def test_resolve_remote_session_invalid_token_is_refused():
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=None)

    with pytest.raises(AppError) as exc_info:
        await service.resolve_remote_session(session, tenant_slug="acme", token="bad-token")

    assert exc_info.value.code == "KDS_SESSION_INVALID"
    assert exc_info.value.status_code == 401


async def test_resolve_remote_session_expired_token_is_refused():
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=_remote(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))

    with pytest.raises(AppError) as exc_info:
        await service.resolve_remote_session(session, tenant_slug="acme", token="plain-token")

    assert exc_info.value.code == "KDS_SESSION_EXPIRED"


async def test_resolve_remote_session_revoked_token_is_refused():
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=_remote(revoked_at=datetime.now(timezone.utc)))

    with pytest.raises(AppError) as exc_info:
        await service.resolve_remote_session(session, tenant_slug="acme", token="plain-token")

    assert exc_info.value.code == "KDS_SESSION_REVOKED"


async def test_revoke_current_remote_session_sets_revoked_at():
    remote = _remote()
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=remote)
    session.commit = AsyncMock()

    result = await service.revoke_remote_session(session, tenant_slug="acme", token="plain-token")

    assert result == {"revoked": True}
    assert remote.revoked_at is not None
    session.commit.assert_awaited_once()


async def test_revoke_current_remote_session_is_idempotent():
    remote = _remote(revoked_at=datetime.now(timezone.utc))
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=remote)
    session.commit = AsyncMock()

    result = await service.revoke_remote_session(session, tenant_slug="acme", token="plain-token")

    assert result == {"revoked": True}
    session.commit.assert_not_called()


async def test_admin_revoke_all_screen_sessions_returns_count():
    session = AsyncMock()
    session.get = AsyncMock(return_value=_screen())
    session.execute = AsyncMock(return_value=_Result(rowcount=3))
    session.commit = AsyncMock()

    result = await service.revoke_screen_sessions(session, screen_id=12)

    assert result == {"revoked_count": 3}
    session.commit.assert_awaited_once()


def test_pairing_and_session_hashes_are_tenant_scoped():
    assert service.hash_pairing_code("tenant-a", "482731") != service.hash_pairing_code("tenant-b", "482731")
    assert service.hash_remote_session_token("tenant-a", "token") != service.hash_remote_session_token("tenant-b", "token")


async def test_pair_requires_jwt(client):
    response = await client.post("/api/v1/kds/pair", json={"code": "482731"})

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


async def test_pair_requires_orders_preparation_permission(client):
    from app.core.http.deps import get_current_user
    from app.main import app

    async def _staff_without_preparation():
        return {"id": "7", "tenant_slug": "acme", "role": "staff", "permissions": ["orders:read"]}

    app.dependency_overrides[get_current_user] = _staff_without_preparation
    try:
        response = await client.post("/api/v1/kds/pair", json={"code": "482731"})
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 403


async def test_remote_session_requires_jwt_even_with_kds_token(client):
    response = await client.get("/api/v1/kds/remote/session", headers={"X-KDS-Session": "raw-token"})

    assert response.status_code == 401


async def test_remote_session_requires_x_kds_session_header(client):
    from app.core.http.deps import get_current_user
    from app.main import app

    async def _staff():
        return {"id": "7", "tenant_slug": "acme", "role": "staff", "permissions": ["orders:preparation"]}

    app.dependency_overrides[get_current_user] = _staff
    try:
        response = await client.get("/api/v1/kds/remote/session")
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 401
    assert response.json()["code"] == "KDS_SESSION_INVALID"
