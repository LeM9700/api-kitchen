from fastapi import APIRouter

from app.modules.loyalty.account.router import router as account_router
from app.modules.loyalty.config.router import router as config_router

router = APIRouter()

router.include_router(account_router)
router.include_router(config_router)
