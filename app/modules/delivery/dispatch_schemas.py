from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class DriverCreate(BaseModel):
    email: EmailStr
    full_name: str = Field(..., min_length=1, max_length=255)
    phone: str | None = Field(None, max_length=32)
    vehicle: str | None = Field(None, max_length=64)
    establishment_id: int = Field(..., ge=1)

    @field_validator("full_name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Le nom est requis")
        return value


class DriverUpdate(BaseModel):
    is_active: bool | None = None
    phone: str | None = Field(None, max_length=32)
    vehicle: str | None = Field(None, max_length=64)


class DriverOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    full_name: str | None = None
    email: str | None = None
    phone: str | None = None
    vehicle: str | None = None
    establishment_id: int
    is_active: bool
    # Pointe (en service, pause comprise) : seul un livreur pointe peut recevoir une livraison.
    clocked_in: bool = False
    on_break: bool = False
    active_deliveries: int = 0
    delivered_today: int = 0


class DriverCreatedOut(DriverOut):
    # Affiche une seule fois : le livreur doit le changer a sa premiere connexion.
    temporary_password: str


class DispatchOrderOut(BaseModel):
    order_id: int
    status: str
    establishment_id: int | None = None
    customer_name: str | None = None
    customer_phone: str | None = None
    delivery_address: str | None = None
    delivery_lat: float | None = None
    delivery_lng: float | None = None
    delivery_instructions: str | None = None
    total: float
    payment_status: str
    items_count: int = 0
    created_at: datetime | None = None
    estimated_delivery_at: datetime | None = None


class DispatchDeliveryOut(BaseModel):
    id: int
    status: str
    driver_id: int
    driver_name: str | None = None
    run_id: int | None = None
    assigned_at: datetime | None = None
    departed_at: datetime | None = None
    arrived_at: datetime | None = None
    order: DispatchOrderOut


class DispatchBoardOut(BaseModel):
    dispatch_enabled: bool
    unassigned: list[DispatchOrderOut]
    deliveries: list[DispatchDeliveryOut]
    drivers: list[DriverOut]


class AssignRequest(BaseModel):
    order_ids: list[int] = Field(..., min_length=1, max_length=20)
    driver_id: int = Field(..., ge=1)

    @field_validator("order_ids")
    @classmethod
    def _unique(cls, value: list[int]) -> list[int]:
        if len(set(value)) != len(value):
            raise ValueError("Commandes en double")
        if any(v < 1 for v in value):
            raise ValueError("Identifiant de commande invalide")
        return value


class UnassignRequest(BaseModel):
    order_id: int = Field(..., ge=1)


class DeliveryActionOut(BaseModel):
    id: int
    order_id: int
    status: str
    driver_id: int


class DriverDeliveryOut(BaseModel):
    """Ce que le livreur voit d'une livraison : de quoi livrer, rien de plus."""

    id: int
    order_id: int
    status: str
    # 'upcoming' (assignee, pas encore prete), 'ready' (peut partir), 'en_route', 'arrived'.
    phase: str
    run_id: int | None = None
    customer_name: str | None = None
    customer_phone: str | None = None
    delivery_address: str | None = None
    delivery_lat: float | None = None
    delivery_lng: float | None = None
    delivery_instructions: str | None = None
    items_count: int = 0
    total: float
    # Montant a encaisser a la remise (empreinte bancaire = especes), 0 si deja paye.
    amount_due: float = 0
    payment_status: str
    estimated_delivery_at: datetime | None = None
    assigned_at: datetime | None = None
    departed_at: datetime | None = None
    arrived_at: datetime | None = None
    # Preuve de remise exigee : le livreur doit saisir le code du client.
    proof_required: bool = False
    code_locked: bool = False
    # Regles d'echec « client absent / injoignable » (delai depuis l'arrivee, appels).
    failure_min_wait_minutes: int = 5
    failure_min_call_attempts: int = 1


class DriverMeOut(BaseModel):
    id: int
    user_id: int
    full_name: str | None = None
    phone: str | None = None
    vehicle: str | None = None
    establishment_id: int
    clocked_in: bool = False
    on_break: bool = False


class DepartRequest(BaseModel):
    delivery_ids: list[int] = Field(..., min_length=1, max_length=20)

    @field_validator("delivery_ids")
    @classmethod
    def _unique(cls, value: list[int]) -> list[int]:
        if len(set(value)) != len(value):
            raise ValueError("Livraisons en double")
        return value


class DeliverRequest(BaseModel):
    # Obligatoire pour une commande a regler a la remise ; ignore si deja payee en ligne.
    cash_received: float | None = Field(None, gt=0)
    # Code de remise a 4 chiffres du client (obligatoire quand la preuve est exigee). Le format
    # est controle par le service pour renvoyer un message clair.
    code: str | None = Field(None, max_length=16)


class DriverRecapItemOut(BaseModel):
    delivery_id: int
    order_id: int
    delivery_address: str | None = None
    total: float
    finished_at: datetime | None = None


class DriverRecapOut(BaseModel):
    day: date
    delivered_count: int
    runs_count: int
    cash_collected: float
    deliveries: list[DriverRecapItemOut]


class DriverFailureRequest(BaseModel):
    reason: str = Field(..., max_length=32)
    note: str | None = Field(None, max_length=200)
    # Nombre d'appels passes au client (exige pour « absent » et « injoignable »).
    call_attempts: int = Field(0, ge=0, le=20)


class FailureOut(BaseModel):
    id: int
    order_id: int
    delivery_id: int
    driver_id: int
    driver_name: str | None = None
    reason: str
    reason_label: str
    # Qui est en cause : le client ou le restaurant (aucun frais ne peut etre retenu au client
    # si la faute est celle du restaurant).
    fault: str
    note: str | None = None
    call_attempts: int = 0
    waited_seconds: int | None = None
    status: str
    resolution: str | None = None
    retained_amount: float | None = None
    created_at: datetime | None = None
    resolved_at: datetime | None = None
    customer_name: str | None = None
    customer_phone: str | None = None
    delivery_address: str | None = None
    total: float
    payment_status: str


class ResolveFailureRequest(BaseModel):
    action: str = Field(..., pattern="^(refund|retain|redeliver)$")
    # Centimes, uniquement pour `retain`.
    amount: int | None = Field(None, gt=0)
    note: str | None = Field(None, max_length=200)


class DeliverWithoutCodeRequest(BaseModel):
    reason: str = Field(..., min_length=3, max_length=200)
