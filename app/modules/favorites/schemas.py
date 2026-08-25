from datetime import datetime

from pydantic import BaseModel, ConfigDict


class FavoriteCreate(BaseModel):
    """Payload pour marquer un produit favori.

    Attributes:
        product_id: Identifiant du produit à ajouter aux favoris.
    """

    product_id: int


class FavoriteResponse(BaseModel):
    """Réponse de lecture d'un favori enregistré.

    Attributes:
        id: Identifiant de l'enregistrement.
        product_id: Identifiant du produit favori.
        created_at: Timestamp UTC de création.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    created_at: datetime
