"""Enums partagees du futur reseau de livreurs independants.

Ces enums posent les conventions communes consommees par les plans 02-05
(checkout du reseau, demandes de livraison diffusees aux livreurs,
affectation, suivi). Aucun modele de donnees de CE plan ne les utilise
encore : le reseau reste desactive par defaut
(`settings.delivery_network_enabled = False`).

Volontairement des `str, Enum` Python -- **pas** de
`sqlalchemy.Enum(..., native_enum=True)`. Un ENUM Postgres natif impose une
migration `ALTER TYPE` pour chaque nouvelle valeur ; une simple colonne
`String` validee par ces enums cote application n'en a pas besoin, ce qui
convient mieux au rythme d'iteration attendu sur les plans 02-05.
"""

from enum import Enum


class DeliveryHandoffMode(str, Enum):
    """Mode de remise de la commande au client final."""

    CUSTOMER_PICKUP = "customer_pickup"
    IN_HOUSE_COURIER = "in_house_courier"
    NETWORK_COURIER = "network_courier"


class DeliveryCheckoutStatus(str, Enum):
    """Statut du checkout d'une commande confiee au reseau de livreurs."""

    PENDING = "pending"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class DeliveryRequestStatus(str, Enum):
    """Statut d'une demande de livraison diffusee aux livreurs independants."""

    PENDING = "pending"
    BROADCASTING = "broadcasting"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class DeliveryVehicleType(str, Enum):
    """Type de vehicule declare par un livreur independant."""

    BIKE = "bike"
    SCOOTER = "scooter"
    MOTORCYCLE = "motorcycle"
    CAR = "car"
    ON_FOOT = "on_foot"


class DeliveryAuditActor(str, Enum):
    """Acteur a l'origine d'une transition auditee du reseau de livreurs."""

    SYSTEM = "system"
    ADMIN = "admin"
    COURIER = "courier"
    CUSTOMER = "customer"
    RESTAURANT_STAFF = "restaurant_staff"
