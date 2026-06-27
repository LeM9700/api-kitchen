"""Tests de la feature upload d'images Cloudinary.

Couverture :
- ``app.core.services.cloudinary`` : validation magic bytes, taille, upload, erreurs
- ``app.modules.catalog.image_service`` : CRUD images, auto-primary, reorder
- ``app.modules.catalog.image_router`` : endpoints REST (auth, réponses, erreurs)

Toutes les interactions Cloudinary sont mockées (aucun appel réseau réel).
Les tests service utilisent le fixture ``db_session`` avec savepoint rollback.
Les tests router utilisent un client ASGI httpx avec les couches service mockées.

[🔒 SÉCURITÉ] Extension et magic bytes doivent correspondre exactement :
    un fichier nommé .jpg avec un contenu PNG est rejeté avec le code EXTENSION_MISMATCH.
"""

import io
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.services.cloudinary import upload_image as cloudinary_upload
from app.core.http.errors import AppError
from app.core.auth.security import create_access_token
from app.main import app
from app.modules.catalog.image import image_service
from app.modules.catalog.image.image_model import MediaImage
from app.modules.catalog.models import Product


# ---------------------------------------------------------------------------
# Magic bytes helpers
# ---------------------------------------------------------------------------

def _jpg_bytes(size: int = 128) -> bytes:
    """Retourne un buffer minimal commençant par le magic bytes JPEG (FF D8 FF).

    Args:
        size: Taille totale du buffer en octets.

    Returns:
        Bytes simulant un fichier JPEG.
    """
    return b"\xff\xd8\xff" + b"\x00" * (size - 3)


def _png_bytes(size: int = 128) -> bytes:
    """Retourne un buffer minimal commençant par le magic bytes PNG (89 50 4E 47).

    Args:
        size: Taille totale du buffer en octets.

    Returns:
        Bytes simulant un fichier PNG.
    """
    return b"\x89PNG" + b"\x00" * (size - 4)


def _webp_bytes(size: int = 128) -> bytes:
    """Retourne un buffer valide WebP (RIFF....WEBP).

    Args:
        size: Taille totale du buffer en octets (minimum 12).

    Returns:
        Bytes simulant un fichier WebP.
    """
    return b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * (size - 12)


def _svg_bytes() -> bytes:
    """Retourne un document SVG minimal UTF-8.

    Returns:
        Bytes d'un SVG basique.
    """
    return b"<svg xmlns='http://www.w3.org/2000/svg'><rect width='10' height='10'/></svg>"


def _exe_bytes(size: int = 128) -> bytes:
    """Retourne des bytes commençant par le magic bytes PE/EXE (4D 5A).

    Args:
        size: Taille totale du buffer en octets.

    Returns:
        Bytes simulant un exécutable Windows.
    """
    return b"\x4d\x5a" + b"\x00" * (size - 2)


# ---------------------------------------------------------------------------
# Réponse Cloudinary par défaut (mock)
# ---------------------------------------------------------------------------

_CLOUD_RESULT: dict[str, Any] = {
    "public_id": "pizza/test/products/1/abc123",
    "secure_url": "https://res.cloudinary.com/demo/image/upload/v1/pizza/test/products/1/abc123",
    "format": "jpg",
    "width": 800,
    "height": 600,
    "bytes": 65_536,
}

_CLOUD_DATA_SERVICE: dict[str, Any] = {
    "url": "https://res.cloudinary.com/demo/image/upload/v1/pizza/test/products/1/abc123",
    "url_thumbnail": "https://res.cloudinary.com/demo/image/upload/c_fill,w_300,h_300,q_auto,f_auto/pizza/test/products/1/abc123",
    "url_medium": "https://res.cloudinary.com/demo/image/upload/w_800,q_auto,f_auto/pizza/test/products/1/abc123",
    "cloudinary_public_id": "pizza/test/products/1/abc123",
    "format": "jpg",
    "size_bytes": 65_536,
    "width": 800,
    "height": 600,
}


# ---------------------------------------------------------------------------
# Fixtures partagées
# ---------------------------------------------------------------------------

@pytest.fixture
def staff_token() -> str:
    """JWT access token avec rôle 'staff' pour les tests router.

    Returns:
        Chaîne JWT signée avec les claims nécessaires.
    """
    return create_access_token({
        "sub": "user-1",
        "tenant_id": "tenant-abc",
        "tenant_slug": "test",
        "role": "staff",
        "email": "staff@test.com",
    })


@pytest.fixture
def staff_headers(staff_token: str) -> dict[str, str]:
    """Headers HTTP Bearer avec token staff.

    Args:
        staff_token: JWT access token staff.

    Returns:
        Dictionnaire de headers HTTP.
    """
    return {"Authorization": f"Bearer {staff_token}"}


@pytest.fixture
def client_token() -> str:
    """JWT access token avec rôle 'client' (non autorisé sur les mutations).

    Returns:
        Chaîne JWT signée avec le rôle client.
    """
    return create_access_token({
        "sub": "user-2",
        "tenant_id": "tenant-abc",
        "tenant_slug": "test",
        "role": "client",
        "email": "client@test.com",
    })


@pytest.fixture
def mock_cloudinary_upload():
    """Mock de ``cloudinary_service.upload_image`` retournant _CLOUD_DATA_SERVICE.

    Yields:
        AsyncMock patché sur le module image_service.
    """
    with patch(
        "app.modules.catalog.image_service.cloudinary_service.upload_image",
        new_callable=AsyncMock,
        return_value=_CLOUD_DATA_SERVICE,
    ) as mock:
        yield mock


@pytest.fixture
def mock_cloudinary_delete():
    """Mock de ``cloudinary_service.delete_image`` (no-op).

    Yields:
        AsyncMock patché sur le module image_service.
    """
    with patch(
        "app.modules.catalog.image_service.cloudinary_service.delete_image",
        new_callable=AsyncMock,
    ) as mock:
        yield mock


@pytest.fixture
async def db_product(db_session):
    """Crée un Product persisté dans la transaction de test.

    Args:
        db_session: Session SQLAlchemy avec savepoint rollback.

    Yields:
        Instance ``Product`` persistée (rollback automatique en teardown).
    """
    product = Product(name="Pizza Margherita", base_price=12.50)
    db_session.add(product)
    await db_session.flush()
    return product


@pytest.fixture
async def async_client():
    """Client HTTP async branché directement sur l'app ASGI (sans réseau réel).

    Yields:
        Instance ``AsyncClient`` httpx.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _make_media_image(**overrides) -> MediaImage:
    """Construit une instance MediaImage avec des valeurs par défaut cohérentes.

    Args:
        **overrides: Attributs à surcharger par rapport aux valeurs par défaut.

    Returns:
        Instance ``MediaImage`` non persistée.
    """
    defaults = dict(
        id=1,
        entity_type="product",
        entity_id=1,
        cloudinary_public_id="pizza/test/products/1/abc123",
        url=_CLOUD_DATA_SERVICE["url"],
        url_thumbnail=_CLOUD_DATA_SERVICE["url_thumbnail"],
        url_medium=_CLOUD_DATA_SERVICE["url_medium"],
        format="jpg",
        size_bytes=65_536,
        width=800,
        height=600,
        is_primary=False,
        display_order=0,
        alt_text=None,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    img = MediaImage.__new__(MediaImage)
    for k, v in defaults.items():
        setattr(img, k, v)
    return img


@asynccontextmanager
async def _noop_tenant_session(_slug: str):
    """Async context manager de remplacement pour ``get_tenant_session`` dans les tests router.

    Args:
        _slug: Slug du tenant (ignoré).

    Yields:
        MagicMock simulant une AsyncSession.
    """
    yield MagicMock()


# ===========================================================================
# 1. CloudinaryService — tests unitaires purs
# ===========================================================================

class TestCloudinaryService:
    """Tests unitaires pour ``app.core.services.cloudinary.upload_image``.

    Aucune base de données. ``anyio.to_thread.run_sync`` est patché
    pour court-circuiter l'appel réseau Cloudinary.
    """

    @pytest.mark.asyncio
    async def test_upload_jpg_valid_returns_url_dict(self):
        """Upload JPG valide → retourne dict avec url, url_thumbnail, url_medium.

        Vérifie que les trois clés d'URL sont présentes et non vides.
        """
        with patch("anyio.to_thread.run_sync", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _CLOUD_RESULT

            result = await cloudinary_upload(
                file_bytes=_jpg_bytes(),
                filename="photo.jpg",
                tenant_slug="test",
                entity_type="products",
                entity_id=1,
            )

        assert result["url"] == _CLOUD_RESULT["secure_url"]
        assert result["url_thumbnail"].startswith("https://")
        assert result["url_medium"].startswith("https://")
        assert result["cloudinary_public_id"] == _CLOUD_RESULT["public_id"]

    @pytest.mark.asyncio
    async def test_upload_png_valid_returns_url_dict(self):
        """Upload PNG valide → retourne dict avec url, url_thumbnail, url_medium.

        Vérifie que le format détecté est bien ``png``.
        """
        cloud_result = {**_CLOUD_RESULT, "format": "png", "public_id": "pizza/test/products/1/png1"}
        with patch("anyio.to_thread.run_sync", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = cloud_result

            result = await cloudinary_upload(
                file_bytes=_png_bytes(),
                filename="image.png",
                tenant_slug="test",
                entity_type="products",
                entity_id=1,
            )

        assert "url" in result
        assert "url_thumbnail" in result
        assert "url_medium" in result
        assert result["format"] == "png"

    @pytest.mark.asyncio
    async def test_upload_webp_valid_returns_url_dict(self):
        """Upload WebP valide → retourne dict avec url, url_thumbnail, url_medium.

        Vérifie que les magic bytes RIFF...WEBP sont correctement détectés.
        """
        cloud_result = {**_CLOUD_RESULT, "format": "webp", "public_id": "pizza/test/products/1/webp1"}
        with patch("anyio.to_thread.run_sync", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = cloud_result

            result = await cloudinary_upload(
                file_bytes=_webp_bytes(),
                filename="image.webp",
                tenant_slug="test",
                entity_type="products",
                entity_id=1,
            )

        assert "url" in result
        assert "url_thumbnail" in result
        assert result["format"] == "webp"

    @pytest.mark.asyncio
    async def test_upload_svg_valid_uses_svg_transformation_urls(self):
        """Upload SVG valide → URLs de transformation sans crop, format svg.

        Les SVG n'utilisent pas ``c_fill`` mais ``w_300,h_300,f_svg``
        et ``w_800,f_svg`` pour préserver le format vectoriel.
        """
        cloud_result = {**_CLOUD_RESULT, "format": "svg", "public_id": "pizza/test/products/1/icon"}
        with patch("anyio.to_thread.run_sync", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = cloud_result

            result = await cloudinary_upload(
                file_bytes=_svg_bytes(),
                filename="icon.svg",
                tenant_slug="test",
                entity_type="products",
                entity_id=1,
            )

        assert "url_thumbnail" in result
        assert "url_medium" in result
        # Les URLs SVG ne doivent pas contenir c_fill (crop raster)
        assert "c_fill" not in result["url_thumbnail"]
        assert "f_svg" in result["url_thumbnail"]
        assert "f_svg" in result["url_medium"]

    @pytest.mark.asyncio
    async def test_file_too_large_raises_file_too_large(self):
        """Fichier > 8 Mo → AppError FILE_TOO_LARGE (status_code=413).

        Aucun appel Cloudinary ne doit être effectué.
        """
        # 8 Mo + 1 octet
        big_bytes = _jpg_bytes() + b"\x00" * (8 * 1024 * 1024)

        with patch("anyio.to_thread.run_sync", new_callable=AsyncMock) as mock_run:
            with pytest.raises(AppError) as exc_info:
                await cloudinary_upload(
                    file_bytes=big_bytes,
                    filename="big.jpg",
                    tenant_slug="test",
                    entity_type="products",
                    entity_id=1,
                )

        assert exc_info.value.code == "FILE_TOO_LARGE"
        assert exc_info.value.status_code == 413
        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_jpg_ext_with_png_bytes_raises_extension_mismatch(self):
        """Extension .jpg avec magic bytes PNG → AppError EXTENSION_MISMATCH (status 400).

        Le message doit mentionner l'extension fournie (``.jpg``) et le format
        détecté (``PNG``) pour guider l'utilisateur vers le bon renommage.
        """
        with pytest.raises(AppError) as exc_info:
            await cloudinary_upload(
                file_bytes=_png_bytes(),  # contenu PNG réel
                filename="photo.jpg",     # extension .jpg incorrecte
                tenant_slug="test",
                entity_type="products",
                entity_id=1,
            )

        err = exc_info.value
        assert err.code == "EXTENSION_MISMATCH"
        assert err.status_code == 400
        assert ".jpg" in err.detail
        assert "PNG" in err.detail

    @pytest.mark.asyncio
    async def test_png_ext_with_jpg_bytes_raises_extension_mismatch(self):
        """Extension .png avec magic bytes JPG → AppError EXTENSION_MISMATCH (status 400).

        Test miroir : vérifie la symétrie de la validation dans l'autre sens.
        Le message doit mentionner ``.png`` et ``JPG``.
        """
        with pytest.raises(AppError) as exc_info:
            await cloudinary_upload(
                file_bytes=_jpg_bytes(),  # contenu JPG réel
                filename="image.png",     # extension .png incorrecte
                tenant_slug="test",
                entity_type="products",
                entity_id=1,
            )

        err = exc_info.value
        assert err.code == "EXTENSION_MISMATCH"
        assert err.status_code == 400
        assert ".png" in err.detail
        assert "JPG" in err.detail

    @pytest.mark.asyncio
    async def test_exe_ext_with_jpg_bytes_raises_invalid_format(self):
        """Extension .exe avec magic bytes JPG → AppError INVALID_IMAGE_FORMAT.

        L'extension ``.exe`` n'est pas dans ``ALLOWED_FORMATS``, quelle que soit
        la teneur réelle du fichier. Le rejet intervient avant la lecture des magic bytes.
        """
        with pytest.raises(AppError) as exc_info:
            await cloudinary_upload(
                file_bytes=_jpg_bytes(),
                filename="virus.exe",
                tenant_slug="test",
                entity_type="products",
                entity_id=1,
            )

        assert exc_info.value.code == "INVALID_IMAGE_FORMAT"
        assert exc_info.value.status_code == 422

    @pytest.mark.asyncio
    async def test_jpg_ext_with_exe_magic_bytes_raises_invalid_format(self):
        """Extension .jpg avec magic bytes EXE (\\x4d\\x5a) → AppError INVALID_IMAGE_FORMAT.

        Les magic bytes ``MZ`` ne sont reconnus par aucun format image →
        ``_detect_format_from_bytes`` retourne ``None`` → rejet immédiat.
        """
        with pytest.raises(AppError) as exc_info:
            await cloudinary_upload(
                file_bytes=_exe_bytes(),  # magic bytes MZ = PE exécutable
                filename="malware.jpg",   # extension camouflée
                tenant_slug="test",
                entity_type="products",
                entity_id=1,
            )

        assert exc_info.value.code == "INVALID_IMAGE_FORMAT"
        assert exc_info.value.status_code == 422

    @pytest.mark.asyncio
    async def test_cloudinary_sdk_exception_raises_upload_failed(self):
        """Échec Cloudinary (exception SDK) → AppError UPLOAD_FAILED (502).

        Toute exception non-AppError levée par le thread Cloudinary est
        interceptée et convertie en ``UPLOAD_FAILED``.
        """
        with patch("anyio.to_thread.run_sync", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = Exception("Connection timeout")

            with pytest.raises(AppError) as exc_info:
                await cloudinary_upload(
                    file_bytes=_jpg_bytes(),
                    filename="photo.jpg",
                    tenant_slug="test",
                    entity_type="products",
                    entity_id=1,
                )

        assert exc_info.value.code == "UPLOAD_FAILED"
        assert exc_info.value.status_code == 502
        assert "Connection timeout" in exc_info.value.detail


# ===========================================================================
# 2. ImageService — tests d'intégration (db_session + cloudinary mocké)
# ===========================================================================

class TestImageService:
    """Tests d'intégration pour ``app.modules.catalog.image_service``.

    Chaque test opère dans une transaction avec savepoint rollback via ``db_session``.
    Les appels Cloudinary sont mockés via les fixtures ``mock_cloudinary_upload`` /
    ``mock_cloudinary_delete``.
    """

    @pytest.mark.asyncio
    async def test_upload_entity_image_entity_not_found(self, db_session, mock_cloudinary_upload):
        """upload_entity_image avec entité inexistante → AppError ENTITY_NOT_FOUND (404).

        Cloudinary ne doit pas être appelé si l'entité n'existe pas en base.

        Args:
            db_session: Session SQLAlchemy avec savepoint rollback.
            mock_cloudinary_upload: Mock de cloudinary_service.upload_image.
        """
        with pytest.raises(AppError) as exc_info:
            await image_service.upload_entity_image(
                session=db_session,
                tenant_slug="test",
                entity_type="products",
                entity_id=99_999,  # inexistant
                file_bytes=_jpg_bytes(),
                filename="photo.jpg",
            )

        assert exc_info.value.code == "ENTITY_NOT_FOUND"
        assert exc_info.value.status_code == 404
        mock_cloudinary_upload.assert_not_called()

    @pytest.mark.asyncio
    async def test_upload_first_image_auto_primary(self, db_session, db_product, mock_cloudinary_upload):
        """Premier upload sans is_primary → is_primary auto-True (première image de l'entité).

        La logique : si aucune image n'existe encore pour l'entité,
        la nouvelle image devient automatiquement la principale, indépendamment
        du paramètre ``is_primary`` passé.

        Args:
            db_session: Session SQLAlchemy avec savepoint rollback.
            db_product: Product persisté dans la transaction de test.
            mock_cloudinary_upload: Mock de cloudinary_service.upload_image.
        """
        image = await image_service.upload_entity_image(
            session=db_session,
            tenant_slug="test",
            entity_type="products",
            entity_id=db_product.id,
            file_bytes=_jpg_bytes(),
            filename="first.jpg",
            is_primary=False,  # non fourni explicitement → devrait devenir True auto
        )

        assert image.is_primary is True

    @pytest.mark.asyncio
    async def test_upload_second_image_primary_demotes_first(
        self, db_session, db_product, mock_cloudinary_upload
    ):
        """Deuxième upload avec is_primary=True → premier perd le flag is_primary.

        Vérifie la logique de démotion atomique : avant l'upload, toutes les images
        existantes de l'entité ont leur ``is_primary`` mis à False.

        Args:
            db_session: Session SQLAlchemy avec savepoint rollback.
            db_product: Product persisté dans la transaction de test.
            mock_cloudinary_upload: Mock de cloudinary_service.upload_image.
        """
        first = await image_service.upload_entity_image(
            session=db_session,
            tenant_slug="test",
            entity_type="products",
            entity_id=db_product.id,
            file_bytes=_jpg_bytes(),
            filename="first.jpg",
            is_primary=True,
        )
        assert first.is_primary is True

        # Cloudinary mock retourne un public_id différent pour le deuxième appel
        mock_cloudinary_upload.return_value = {
            **_CLOUD_DATA_SERVICE,
            "cloudinary_public_id": "pizza/test/products/1/def456",
            "url": "https://res.cloudinary.com/demo/image/upload/v1/def456",
        }

        second = await image_service.upload_entity_image(
            session=db_session,
            tenant_slug="test",
            entity_type="products",
            entity_id=db_product.id,
            file_bytes=_jpg_bytes(),
            filename="second.jpg",
            is_primary=True,
        )

        await db_session.refresh(first)
        assert second.is_primary is True
        assert first.is_primary is False

    @pytest.mark.asyncio
    async def test_list_entity_images_sorted_by_display_order(
        self, db_session, db_product, mock_cloudinary_upload
    ):
        """list_entity_images → retourne liste triée par display_order croissant.

        Trois images uploadées séquentiellement ; display_order doit être [0, 1, 2].

        Args:
            db_session: Session SQLAlchemy avec savepoint rollback.
            db_product: Product persisté dans la transaction de test.
            mock_cloudinary_upload: Mock de cloudinary_service.upload_image.
        """
        for i in range(3):
            mock_cloudinary_upload.return_value = {
                **_CLOUD_DATA_SERVICE,
                "cloudinary_public_id": f"pizza/test/products/{db_product.id}/img{i}",
                "url": f"https://res.cloudinary.com/demo/image/upload/v1/img{i}",
            }
            await image_service.upload_entity_image(
                session=db_session,
                tenant_slug="test",
                entity_type="products",
                entity_id=db_product.id,
                file_bytes=_jpg_bytes(),
                filename=f"img{i}.jpg",
            )

        images = await image_service.list_entity_images(db_session, "products", db_product.id)

        assert len(images) == 3
        orders = [img.display_order for img in images]
        assert orders == sorted(orders), f"Ordre attendu croissant, obtenu : {orders}"

    @pytest.mark.asyncio
    async def test_delete_entity_image_removes_row_and_calls_cloudinary(
        self, db_session, db_product, mock_cloudinary_upload, mock_cloudinary_delete
    ):
        """delete_entity_image → supprime la ligne en base ET appelle cloudinary.delete_image.

        L'ordre doit être : Cloudinary d'abord, puis suppression DB (idempotence).

        Args:
            db_session: Session SQLAlchemy avec savepoint rollback.
            db_product: Product persisté dans la transaction de test.
            mock_cloudinary_upload: Mock de cloudinary_service.upload_image.
            mock_cloudinary_delete: Mock de cloudinary_service.delete_image.
        """
        image = await image_service.upload_entity_image(
            session=db_session,
            tenant_slug="test",
            entity_type="products",
            entity_id=db_product.id,
            file_bytes=_jpg_bytes(),
            filename="to_delete.jpg",
        )
        image_id = image.id
        public_id = image.cloudinary_public_id

        await image_service.delete_entity_image(db_session, image_id, "test")

        mock_cloudinary_delete.assert_called_once_with(public_id)
        deleted = await db_session.get(MediaImage, image_id)
        assert deleted is None

    @pytest.mark.asyncio
    async def test_delete_primary_image_promotes_next(
        self, db_session, db_product, mock_cloudinary_upload, mock_cloudinary_delete
    ):
        """Suppression de la primary → auto-promotion de l'image suivante (display_order++).

        La deuxième image (display_order=1) doit récupérer ``is_primary=True``
        après suppression de la première (display_order=0, is_primary=True).

        Args:
            db_session: Session SQLAlchemy avec savepoint rollback.
            db_product: Product persisté dans la transaction de test.
            mock_cloudinary_upload: Mock de cloudinary_service.upload_image.
            mock_cloudinary_delete: Mock de cloudinary_service.delete_image.
        """
        mock_cloudinary_upload.return_value = {
            **_CLOUD_DATA_SERVICE,
            "cloudinary_public_id": "pizza/test/products/1/primary",
        }
        primary = await image_service.upload_entity_image(
            session=db_session,
            tenant_slug="test",
            entity_type="products",
            entity_id=db_product.id,
            file_bytes=_jpg_bytes(),
            filename="primary.jpg",
            is_primary=True,
        )

        mock_cloudinary_upload.return_value = {
            **_CLOUD_DATA_SERVICE,
            "cloudinary_public_id": "pizza/test/products/1/secondary",
            "url": "https://res.cloudinary.com/demo/image/upload/v1/secondary",
        }
        secondary = await image_service.upload_entity_image(
            session=db_session,
            tenant_slug="test",
            entity_type="products",
            entity_id=db_product.id,
            file_bytes=_jpg_bytes(),
            filename="secondary.jpg",
            is_primary=False,
        )

        await image_service.delete_entity_image(db_session, primary.id, "test")

        await db_session.refresh(secondary)
        assert secondary.is_primary is True

    @pytest.mark.asyncio
    async def test_set_primary_image_changes_flag_correctly(
        self, db_session, db_product, mock_cloudinary_upload
    ):
        """set_primary_image → seule l'image cible obtient is_primary=True.

        Toutes les autres images de l'entité doivent avoir ``is_primary=False``.

        Args:
            db_session: Session SQLAlchemy avec savepoint rollback.
            db_product: Product persisté dans la transaction de test.
            mock_cloudinary_upload: Mock de cloudinary_service.upload_image.
        """
        mock_cloudinary_upload.return_value = {
            **_CLOUD_DATA_SERVICE,
            "cloudinary_public_id": "pizza/test/products/1/img_a",
        }
        first = await image_service.upload_entity_image(
            session=db_session,
            tenant_slug="test",
            entity_type="products",
            entity_id=db_product.id,
            file_bytes=_jpg_bytes(),
            filename="first.jpg",
            is_primary=True,
        )

        mock_cloudinary_upload.return_value = {
            **_CLOUD_DATA_SERVICE,
            "cloudinary_public_id": "pizza/test/products/1/img_b",
            "url": "https://res.cloudinary.com/demo/image/upload/v1/img_b",
        }
        second = await image_service.upload_entity_image(
            session=db_session,
            tenant_slug="test",
            entity_type="products",
            entity_id=db_product.id,
            file_bytes=_jpg_bytes(),
            filename="second.jpg",
            is_primary=False,
        )
        assert first.is_primary is True
        assert second.is_primary is False

        updated = await image_service.set_primary_image(
            db_session, second.id, "products", db_product.id
        )

        await db_session.refresh(first)
        assert updated.is_primary is True
        assert first.is_primary is False

    @pytest.mark.asyncio
    async def test_reorder_images_updates_display_order(
        self, db_session, db_product, mock_cloudinary_upload
    ):
        """reorder_images → met à jour display_order selon l'ordre de la liste fournie.

        Upload 3 images (order 0, 1, 2), puis réordonne en [2, 0, 1].
        Les display_order résultants doivent être [0, 1, 2] pour cette nouvelle séquence.

        Args:
            db_session: Session SQLAlchemy avec savepoint rollback.
            db_product: Product persisté dans la transaction de test.
            mock_cloudinary_upload: Mock de cloudinary_service.upload_image.
        """
        images = []
        for i in range(3):
            mock_cloudinary_upload.return_value = {
                **_CLOUD_DATA_SERVICE,
                "cloudinary_public_id": f"pizza/test/products/{db_product.id}/img_{i}",
                "url": f"https://res.cloudinary.com/demo/image/upload/v1/img_{i}",
            }
            img = await image_service.upload_entity_image(
                session=db_session,
                tenant_slug="test",
                entity_type="products",
                entity_id=db_product.id,
                file_bytes=_jpg_bytes(),
                filename=f"img{i}.jpg",
            )
            images.append(img)

        # Nouvel ordre : img2 → img0 → img1
        new_order_ids = [images[2].id, images[0].id, images[1].id]
        reordered = await image_service.reorder_images(
            db_session, new_order_ids, "products", db_product.id
        )

        result_ids = [img.id for img in reordered]
        assert result_ids == new_order_ids, (
            f"Ordre attendu {new_order_ids}, obtenu {result_ids}"
        )
        # display_order doit correspondre à la position dans new_order_ids
        for idx, img in enumerate(reordered):
            assert img.display_order == idx


# ===========================================================================
# 3. Router — tests ASGI (httpx, service mocké)
# ===========================================================================

class TestImageRouter:
    """Tests des endpoints REST ``/api/v1/catalog`` (image upload, list, delete, primary, reorder).

    Les couches service ET ``get_tenant_session`` sont mockées pour éviter la DB.
    Les tests vérifient : authentification, autorisation, statuts HTTP, structure des réponses.
    """

    _BASE = "/api/v1/catalog"

    # -----------------------------------------------------------------------
    # POST /{entity_type}/{entity_id}/images — Upload
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_upload_without_auth_returns_401(self, async_client):
        """POST /products/1/images sans Authorization header → 401.

        Args:
            async_client: Client ASGI httpx.
        """
        response = await async_client.post(
            f"{self._BASE}/products/1/images",
            files=[("file", ("photo.jpg", io.BytesIO(_jpg_bytes()), "image/jpeg"))],
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_upload_with_staff_auth_returns_201_and_media_image_out(
        self, async_client, staff_headers
    ):
        """POST avec token staff valide → 201 + payload MediaImageOut.

        Vérifie la présence des champs obligatoires : url, url_thumbnail, url_medium.

        Args:
            async_client: Client ASGI httpx.
            staff_headers: Headers Bearer avec token staff.
        """
        mock_image = _make_media_image(entity_id=1)

        with patch(
            "app.modules.catalog.image_service.upload_entity_image",
            new_callable=AsyncMock,
            return_value=mock_image,
        ):
            with patch(
                "app.modules.catalog.image_router.get_tenant_session",
                side_effect=_noop_tenant_session,
            ):
                response = await async_client.post(
                    f"{self._BASE}/products/1/images",
                    files=[("file", ("photo.jpg", io.BytesIO(_jpg_bytes()), "image/jpeg"))],
                    headers=staff_headers,
                )

        assert response.status_code == 201
        body = response.json()
        assert "url" in body
        assert "url_thumbnail" in body
        assert "url_medium" in body
        assert "id" in body

    @pytest.mark.asyncio
    async def test_upload_file_too_large_returns_413_with_code(
        self, async_client, staff_headers
    ):
        """POST fichier > 8 Mo → 413 avec code FILE_TOO_LARGE dans le corps JSON.

        Args:
            async_client: Client ASGI httpx.
            staff_headers: Headers Bearer avec token staff.
        """
        with patch(
            "app.modules.catalog.image_service.upload_entity_image",
            new_callable=AsyncMock,
            side_effect=AppError("FILE_TOO_LARGE", "Fichier trop volumineux. Maximum 8 Mo.", 413),
        ):
            with patch(
                "app.modules.catalog.image_router.get_tenant_session",
                side_effect=_noop_tenant_session,
            ):
                big_stream = io.BytesIO(b"\xff\xd8\xff" + b"\x00" * (8 * 1024 * 1024 + 1))
                response = await async_client.post(
                    f"{self._BASE}/products/1/images",
                    files=[("file", ("big.jpg", big_stream, "image/jpeg"))],
                    headers=staff_headers,
                )

        assert response.status_code == 413
        assert response.json()["code"] == "FILE_TOO_LARGE"

    @pytest.mark.asyncio
    async def test_upload_invalid_format_pdf_returns_422_with_code(
        self, async_client, staff_headers
    ):
        """POST avec un PDF → 422 avec code INVALID_IMAGE_FORMAT dans le corps JSON.

        Args:
            async_client: Client ASGI httpx.
            staff_headers: Headers Bearer avec token staff.
        """
        with patch(
            "app.modules.catalog.image_service.upload_entity_image",
            new_callable=AsyncMock,
            side_effect=AppError(
                "INVALID_IMAGE_FORMAT",
                "Format non accepté : pdf. Formats autorisés : JPG, PNG, WebP, SVG.",
                422,
            ),
        ):
            with patch(
                "app.modules.catalog.image_router.get_tenant_session",
                side_effect=_noop_tenant_session,
            ):
                response = await async_client.post(
                    f"{self._BASE}/products/1/images",
                    files=[("file", ("doc.pdf", io.BytesIO(b"%PDF-1.4 fake"), "application/pdf"))],
                    headers=staff_headers,
                )

        assert response.status_code in (400, 422)
        assert response.json()["code"] == "INVALID_IMAGE_FORMAT"

    # -----------------------------------------------------------------------
    # GET /{entity_type}/{entity_id}/images — Liste publique
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_list_images_without_auth_returns_200(self, async_client):
        """GET /products/1/images sans auth → 200 liste vide (route publique).

        Args:
            async_client: Client ASGI httpx.
        """
        with patch(
            "app.modules.catalog.image_service.list_entity_images",
            new_callable=AsyncMock,
            return_value=[],
        ):
            with patch(
                "app.modules.catalog.image_router.get_tenant_session",
                side_effect=_noop_tenant_session,
            ):
                response = await async_client.get(
                    f"{self._BASE}/products/1/images",
                    headers={"X-Tenant-Slug": "test"},
                )

        assert response.status_code == 200
        assert response.json() == []

    # -----------------------------------------------------------------------
    # DELETE /images/{image_id}
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_delete_without_auth_returns_401(self, async_client):
        """DELETE /images/1 sans Authorization header → 401.

        Args:
            async_client: Client ASGI httpx.
        """
        response = await async_client.delete(f"{self._BASE}/images/1")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_delete_with_client_role_returns_403(self, async_client, client_token):
        """DELETE /images/1 avec rôle 'client' (non staff/admin) → 403.

        Args:
            async_client: Client ASGI httpx.
            client_token: JWT avec rôle client.
        """
        response = await async_client.delete(
            f"{self._BASE}/images/1",
            headers={"Authorization": f"Bearer {client_token}"},
        )
        assert response.status_code == 403

    # -----------------------------------------------------------------------
    # PATCH /images/{image_id}/primary
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_set_primary_without_auth_returns_401(self, async_client):
        """PATCH /images/1/primary sans auth → 401.

        Args:
            async_client: Client ASGI httpx.
        """
        response = await async_client.patch(
            f"{self._BASE}/images/1/primary",
            params={"entity_type": "products", "entity_id": 1},
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_set_primary_with_client_role_returns_403(self, async_client, client_token):
        """PATCH /images/1/primary avec rôle 'client' → 403.

        Args:
            async_client: Client ASGI httpx.
            client_token: JWT avec rôle client.
        """
        response = await async_client.patch(
            f"{self._BASE}/images/1/primary",
            params={"entity_type": "products", "entity_id": 1},
            headers={"Authorization": f"Bearer {client_token}"},
        )
        assert response.status_code == 403

    # -----------------------------------------------------------------------
    # PATCH /{entity_type}/{entity_id}/images/reorder
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_reorder_without_auth_returns_401(self, async_client):
        """PATCH /products/1/images/reorder sans auth → 401.

        Args:
            async_client: Client ASGI httpx.
        """
        response = await async_client.patch(
            f"{self._BASE}/products/1/images/reorder",
            json={"image_ids": [3, 1, 2]},
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_reorder_with_staff_auth_returns_200_with_new_order(
        self, async_client, staff_headers
    ):
        """PATCH /products/1/images/reorder avec token staff → 200 + liste dans le nouvel ordre.

        Vérifie que les IDs retournés respectent l'ordre fourni dans la requête.

        Args:
            async_client: Client ASGI httpx.
            staff_headers: Headers Bearer avec token staff.
        """
        now = datetime.now(timezone.utc)
        requested_order = [3, 1, 2]

        reordered_images = [
            _make_media_image(id=img_id, display_order=idx, created_at=now)
            for idx, img_id in enumerate(requested_order)
        ]

        with patch(
            "app.modules.catalog.image_service.reorder_images",
            new_callable=AsyncMock,
            return_value=reordered_images,
        ):
            with patch(
                "app.modules.catalog.image_router.get_tenant_session",
                side_effect=_noop_tenant_session,
            ):
                response = await async_client.patch(
                    f"{self._BASE}/products/1/images/reorder",
                    json={"image_ids": requested_order},
                    headers=staff_headers,
                )

        assert response.status_code == 200
        body = response.json()
        assert isinstance(body, list)
        assert [item["id"] for item in body] == requested_order
