from fastapi import APIRouter

from app.modules.admin.dashboard.router import router as dashboard_router
from app.modules.admin.security.router import router as security_router
from app.modules.admin.superadmin.router import router as superadmin_router
from app.modules.admin.tenants.lifecycle_router import router as tenants_lifecycle_router
from app.modules.admin.tenants.router import router as tenants_router
from app.modules.admin.users.router import router as users_router

router = APIRouter()

router.include_router(users_router, prefix="/users", tags=["admin-users"])
router.include_router(superadmin_router, tags=["super-admin"])
router.include_router(tenants_lifecycle_router, tags=["admin"])
router.include_router(dashboard_router, tags=["admin"])
router.include_router(security_router, prefix="/security", tags=["security"])
router.include_router(tenants_router, prefix="/tenant", tags=["admin-tenant"])
