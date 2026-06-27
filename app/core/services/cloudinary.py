"""Service Cloudinary : upload, suppression et génération d'URLs variantes.

[🔒 SÉCURITÉ] Validation double des formats : extension du filename ET magic bytes
pour prévenir le spoofing d'extension (ex: image.jpg contenant du SVG malveillant).
[🔒 SÉCURITÉ] Sanitisation des SVG avant upload : suppression des balises <script>,
<use>, des event handlers (on*) et des attributs href/src à valeur javascript:/data:text/html.
"""

import os
import xml.etree.ElementTree as ET
from functools import partial

import anyio
import cloudinary
import cloudinary.api
import cloudinary.uploader
import defusedxml.ElementTree as DefusedET

from app.core.http.errors import AppError

ALLOWED_FORMATS: frozenset[str] = frozenset({"jpg", "jpeg", "png", "webp", "svg"})
MAX_SIZE_BYTES: int = 8 * 1024 * 1024  # 8 Mo


# ---------------------------------------------------------------------------
# Magic bytes signatures
# ---------------------------------------------------------------------------

def _detect_format_from_bytes(data: bytes) -> str | None:
    """Détecte le vrai format d'image depuis les magic bytes du fichier.

    Args:
        data: Premiers octets du fichier (minimum 12 octets recommandés).

    Returns:
        Chaîne de format (``jpg``, ``png``, ``webp``, ``svg``) ou ``None`` si non reconnu.
    """
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] == b"\x89PNG":
        return "png"
    # WebP : "RIFF" aux bytes 0-3 et "WEBP" aux bytes 8-11
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
        return "webp"
    # SVG : peut commencer par <?xml ou <svg (après éventuel BOM UTF-8)
    snippet = data[:512].lstrip(b"\xef\xbb\xbf").lstrip()
    if snippet.startswith(b"<svg") or snippet.startswith(b"<?xml"):
        return "svg"
    return None


# ---------------------------------------------------------------------------
# SVG Sanitisation
# ---------------------------------------------------------------------------

# Préfixes dangereux pour les attributs href / src
_DANGEROUS_PREFIXES: tuple[str, ...] = ("javascript:", "data:text/html")

# Tags à supprimer entièrement (avec leurs enfants éventuels)
_BLOCKED_TAGS: frozenset[str] = frozenset({"script", "use", "foreignObject", "iframe", "object", "embed"})


def sanitize_svg(content: bytes) -> bytes:
    """Sanitise un fichier SVG pour éliminer les vecteurs XSS.

    [🔒 SÉCURITÉ] Utilise ``defusedxml`` pour parser le XML en toute sécurité
    (protection contre billion-laughs, XXE, quadratic blowup).

    Actions réalisées :
    - Suppression des éléments ``<script>`` et ``<use>``.
    - Suppression de tous les attributs ``on*`` (event handlers).
    - Suppression des attributs ``href``, ``xlink:href`` et ``src`` si leur valeur
      commence par ``javascript:`` ou ``data:text/html``.

    Note: ElementTree re-sérialise avec des préfixes de namespace (``ns0:``).
    Le SVG reste sémantiquement équivalent et valide pour Cloudinary.

    Args:
        content: Contenu brut du fichier SVG en bytes.

    Returns:
        SVG sérialisé en UTF-8 après nettoyage.

    Raises:
        AppError: ``INVALID_SVG`` si le contenu est du XML malformé ou invalide.
    """
    try:
        root = DefusedET.fromstring(content.decode("utf-8", errors="replace"))
    except Exception as exc:
        raise AppError(
            "INVALID_SVG",
            "Le fichier SVG est malformé ou invalide.",
            400,
        ) from exc

    # --- Suppression des éléments bloqués ---
    # On construit la parent_map AVANT toute modification pour éviter les bugs
    # d'itération : modifier l'arbre pendant iter() décale les indices internes
    # et fait sauter des éléments frères (ex: <rect> après <script> supprimé).
    parent_map: dict[ET.Element, ET.Element] = {
        child: parent for parent in root.iter() for child in parent
    }
    to_remove: list[tuple[ET.Element, ET.Element]] = []
    for elem in root.iter():
        if not isinstance(elem.tag, str):
            continue  # processing instructions, commentaires
        local = elem.tag.split("}")[-1]
        if local in _BLOCKED_TAGS:
            parent = parent_map.get(elem)
            if parent is not None:
                to_remove.append((parent, elem))
    for parent, elem in to_remove:
        try:
            parent.remove(elem)
        except ValueError:
            pass  # déjà retiré (enfant d'un élément supprimé précédemment)

    # --- Nettoyage des attributs dangereux ---
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        attrs_to_delete: list[str] = []
        for attr, value in element.attrib.items():
            local = attr.split("}")[-1]  # strip namespace prefix ({ns}attr → attr)
            # Supprimer les event handlers (onload, onclick, onerror, onmouseover…)
            if local.startswith("on"):
                attrs_to_delete.append(attr)
                continue
            # Supprimer href / xlink:href / src si valeur dangereuse
            if local in {"href", "src"} and isinstance(value, str):
                if any(value.strip().lower().startswith(p) for p in _DANGEROUS_PREFIXES):
                    attrs_to_delete.append(attr)
        for attr in attrs_to_delete:
            del element.attrib[attr]

    return ET.tostring(root, encoding="unicode").encode("utf-8")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def init_cloudinary(settings) -> None:
    """Configure le SDK Cloudinary depuis les settings applicatifs.

    Doit être appelé une seule fois au démarrage (lifespan FastAPI).

    Args:
        settings: Instance ``Settings`` avec les attributs ``cloudinary_cloud_name``,
            ``cloudinary_api_key``, ``cloudinary_api_secret``.
    """
    cloudinary.config(
        cloud_name=settings.cloudinary_cloud_name,
        api_key=settings.cloudinary_api_key,
        api_secret=settings.cloudinary_api_secret,
        secure=True,
    )


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

def _build_transformation_url(public_id: str, transformation: str, fmt: str) -> str:
    """Construit une URL Cloudinary avec transformation inline.

    Args:
        public_id: Identifiant Cloudinary du fichier (inclut le folder).
        transformation: Chaîne de transformation Cloudinary (ex: ``c_fill,w_300,h_300``).
        fmt: Format de sortie (ex: ``auto``, ``svg``).

    Returns:
        URL HTTPS Cloudinary avec la transformation appliquée.
    """
    cloud_name = cloudinary.config().cloud_name
    # public_id peut déjà contenir le folder complet, Cloudinary le gère.
    return f"https://res.cloudinary.com/{cloud_name}/image/upload/{transformation},f_{fmt}/{public_id}"


def _sync_upload(file_bytes: bytes, folder: str) -> dict:
    """Wrapper synchrone autour de ``cloudinary.uploader.upload``.

    Isolé pour être exécuté dans un thread via ``anyio.to_thread.run_sync``.

    Args:
        file_bytes: Contenu brut du fichier à uploader.
        folder: Dossier Cloudinary cible (ex: ``pizza/slug/products/42``).

    Returns:
        Réponse brute de l'API Cloudinary.

    Raises:
        Exception: Toute erreur remontée par le SDK Cloudinary.
    """
    return cloudinary.uploader.upload(
        file_bytes,
        folder=folder,
        resource_type="auto",
        use_filename=False,
        unique_filename=True,
    )


async def upload_image(
    file_bytes: bytes,
    filename: str,
    tenant_slug: str,
    entity_type: str,
    entity_id: int,
    alt_text: str | None = None,
) -> dict:
    """Upload une image sur Cloudinary avec validation et génération des URLs variantes.

    [🔒 SÉCURITÉ] Double validation : extension + magic bytes.
    [⚡ PERF] L'upload synchrone est wrappé dans ``anyio.to_thread.run_sync``
    pour ne pas bloquer la boucle d'événements asyncio.

    Args:
        file_bytes: Contenu brut du fichier.
        filename: Nom du fichier original (utilisé pour valider l'extension).
        tenant_slug: Slug du tenant pour l'organisation des dossiers Cloudinary.
        entity_type: Type d'entité (``products``, ``categories``, ``extras``, ``variants``).
        entity_id: ID de l'entité cible.
        alt_text: Texte alternatif optionnel.

    Returns:
        Dictionnaire avec les clés : ``url``, ``url_thumbnail``, ``url_medium``,
        ``cloudinary_public_id``, ``format``, ``size_bytes``, ``width``, ``height``.

    Raises:
        AppError: ``FILE_TOO_LARGE`` si le fichier dépasse 8 Mo.
        AppError: ``INVALID_IMAGE_FORMAT`` si l'extension ou les magic bytes sont invalides.
        AppError: ``EXTENSION_MISMATCH`` si l'extension ne correspond pas au format détecté (status 400).
        AppError: ``UPLOAD_FAILED`` si l'upload Cloudinary échoue.
    """
    # --- Validation taille ---
    if len(file_bytes) > MAX_SIZE_BYTES:
        raise AppError(
            "FILE_TOO_LARGE",
            "Fichier trop volumineux. Maximum 8 Mo.",
            status_code=413,
        )

    # --- Validation extension ---
    ext = os.path.splitext(filename)[1].lstrip(".").lower()
    if ext not in ALLOWED_FORMATS:
        raise AppError(
            "INVALID_IMAGE_FORMAT",
            f"Format non accepté : {ext or '(aucun)'}. Formats autorisés : JPG, PNG, WebP, SVG.",
            status_code=422,
        )

    # --- Validation magic bytes (anti-spoofing) ---
    detected = _detect_format_from_bytes(file_bytes)
    if detected is None:
        raise AppError(
            "INVALID_IMAGE_FORMAT",
            "Le contenu du fichier ne correspond à aucun format image supporté.",
            status_code=422,
        )
    # Normalisation : jpg/jpeg → jpg
    normalized_ext = "jpg" if ext == "jpeg" else ext
    if detected != normalized_ext:
        raise AppError(
            "EXTENSION_MISMATCH",
            f"L'extension du fichier (.{ext}) ne correspond pas au format réel détecté ({detected.upper()}). Renommez votre fichier en .{detected} et réessayez.",
            status_code=400,
        )

    # --- Sanitisation SVG (XSS prevention) ---
    if detected == "svg":
        file_bytes = sanitize_svg(file_bytes)

    folder = f"pizza/{tenant_slug}/{entity_type}/{entity_id}"

    # --- Upload dans un thread (SDK synchrone) ---
    try:
        result = await anyio.to_thread.run_sync(
            partial(_sync_upload, file_bytes, folder)
        )
    except Exception as exc:
        raise AppError(
            "UPLOAD_FAILED",
            f"L'upload Cloudinary a échoué : {exc}",
            status_code=502,
        ) from exc

    public_id: str = result["public_id"]
    fmt: str = result.get("format", detected)
    width: int = result.get("width", 0)
    height: int = result.get("height", 0)
    size_bytes: int = result.get("bytes", len(file_bytes))
    original_url: str = result.get("secure_url", "")

    # --- Génération des URLs variantes ---
    if fmt == "svg":
        # SVG vectoriel : pas de crop, conserver le format
        url_thumbnail = _build_transformation_url(public_id, "w_300,h_300", "svg")
        url_medium = _build_transformation_url(public_id, "w_800", "svg")
    else:
        url_thumbnail = _build_transformation_url(public_id, "c_fill,w_300,h_300,q_auto", "auto")
        url_medium = _build_transformation_url(public_id, "w_800,q_auto", "auto")

    return {
        "url": original_url,
        "url_thumbnail": url_thumbnail,
        "url_medium": url_medium,
        "cloudinary_public_id": public_id,
        "format": fmt,
        "size_bytes": size_bytes,
        "width": width,
        "height": height,
    }


# ---------------------------------------------------------------------------
# Suppression
# ---------------------------------------------------------------------------

def _sync_delete(public_id: str) -> dict:
    """Wrapper synchrone autour de ``cloudinary.uploader.destroy``.

    Args:
        public_id: Identifiant Cloudinary du fichier à supprimer.

    Returns:
        Réponse brute de l'API Cloudinary.
    """
    return cloudinary.uploader.destroy(public_id, resource_type="image")


async def delete_image(public_id: str) -> None:
    """Supprime une image de Cloudinary.

    [⚡ PERF] Exécuté dans un thread pour ne pas bloquer la boucle async.

    Args:
        public_id: Identifiant Cloudinary du fichier (``cloudinary_public_id`` en base).

    Raises:
        AppError: ``DELETE_FAILED`` si la suppression échoue côté Cloudinary.
    """
    try:
        await anyio.to_thread.run_sync(partial(_sync_delete, public_id))
    except Exception as exc:
        raise AppError(
            "DELETE_FAILED",
            f"La suppression Cloudinary a échoué : {exc}",
            status_code=502,
        ) from exc
