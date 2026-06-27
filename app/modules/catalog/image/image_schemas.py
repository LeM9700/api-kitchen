"""Schémas Pydantic pour l'API de gestion des images de catalogue."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class MediaImageOut(BaseModel):
    """Représentation sérialisée d'une image en base de données.

    Attributes:
        id: Clé primaire.
        entity_type: Type d'entité associée (``product``, ``category``, ``extra``, ``variant``).
        entity_id: Identifiant de l'entité associée.
        url: URL originale haute résolution.
        url_thumbnail: URL crop 300×300.
        url_medium: URL redimensionnée 800px de large.
        format: Format du fichier (``jpg``, ``png``, ``webp``, ``svg``).
        size_bytes: Poids en octets.
        width: Largeur originale en pixels.
        height: Hauteur originale en pixels.
        is_primary: Indique si c'est l'image principale de l'entité.
        display_order: Ordre d'affichage dans la galerie (croissant).
        alt_text: Texte alternatif pour l'accessibilité.
        created_at: Horodatage de création (timezone-aware).
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    entity_type: str
    entity_id: int
    url: str
    url_thumbnail: str
    url_medium: str
    format: str
    size_bytes: int
    width: int
    height: int
    is_primary: bool
    display_order: int
    alt_text: str | None
    created_at: datetime


class ImageReorderBody(BaseModel):
    """Corps de la requête de réordonnancement de la galerie.

    Attributes:
        image_ids: Liste des IDs dans l'ordre souhaité, du premier au dernier affiché.
    """

    image_ids: list[int]
