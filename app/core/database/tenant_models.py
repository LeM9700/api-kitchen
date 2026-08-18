"""Source unique des modules de modeles ORM tenant-scoped.

Importer ce module peuple ``Base.metadata`` avec toutes les tables qui
vivent dans le schema de chaque tenant (``tenant_{slug}``). C'est la
seule liste a mettre a jour quand un module ajoute des tables tenant :
Alembic (``alembic/env.py``) et le provisioning de nouveaux tenants
(``app/modules/auth/service.py``) importent tous deux ce module plutot
que de dupliquer la liste chacun de leur cote.
"""

from app.modules.admin.tenants import models as admin_tenants_models  # noqa: F401
from app.modules.auth import models as auth_models  # noqa: F401
from app.modules.catalog.allergen import allergen_models as catalog_allergen_models  # noqa: F401
from app.modules.catalog.image import image_model as catalog_image_model  # noqa: F401
from app.modules.catalog import models as catalog_models  # noqa: F401
from app.modules.delivery import models as delivery_models  # noqa: F401
from app.modules.hr import models as hr_models  # noqa: F401
from app.modules.kds import models as kds_models  # noqa: F401
from app.modules.loyalty.account import models as loyalty_account_models  # noqa: F401
from app.modules.loyalty.config import models as loyalty_config_models  # noqa: F401
from app.modules.notifications import models as notifications_models  # noqa: F401
from app.modules.orders import models as orders_models  # noqa: F401
from app.modules.payments import models as payments_models  # noqa: F401
from app.modules.promotions import models as promotions_models  # noqa: F401
from app.modules.stock import models as stock_models  # noqa: F401
