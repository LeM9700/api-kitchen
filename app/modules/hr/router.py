"""Router FastAPI du module RH."""

from fastapi import APIRouter, Depends

from app.core.database import get_tenant_session
from app.core.http.deps import require_role
from app.modules.hr import service as hr_service
from app.modules.hr.schemas import (
    EmployeeProfileCreate,
    EmployeeProfileOut,
    EmployeeProfileSelfOut,
    EmployeeProfileUpdate,
)

router = APIRouter()


@router.post("/employees", response_model=EmployeeProfileOut, status_code=201)
async def create_employee(
    body: EmployeeProfileCreate,
    current_user: dict = Depends(require_role("admin")),
) -> EmployeeProfileOut:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        profile = await hr_service.create_employee_profile(session, body)
        return EmployeeProfileOut.model_validate(profile)


@router.get("/employees", response_model=list[EmployeeProfileOut])
async def list_employees(
    current_user: dict = Depends(require_role("admin")),
) -> list[EmployeeProfileOut]:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        profiles = await hr_service.list_employee_profiles(session)
        return [EmployeeProfileOut.model_validate(profile) for profile in profiles]


@router.get("/employees/me", response_model=EmployeeProfileSelfOut)
async def get_my_employee_profile(
    current_user: dict = Depends(require_role("staff", "admin")),
) -> EmployeeProfileSelfOut:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        profile = await hr_service.get_employee_profile_by_user_id(
            session,
            user_id=int(current_user["id"]),
        )
        return EmployeeProfileSelfOut.model_validate(profile)


@router.patch("/employees/{employee_id}", response_model=EmployeeProfileOut)
async def update_employee(
    employee_id: int,
    body: EmployeeProfileUpdate,
    current_user: dict = Depends(require_role("admin")),
) -> EmployeeProfileOut:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        profile = await hr_service.update_employee_profile(session, employee_id, body)
        return EmployeeProfileOut.model_validate(profile)
