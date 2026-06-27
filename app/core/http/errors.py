from fastapi import Request
from fastapi.responses import JSONResponse

class AppError(Exception):
    def __init__(self, code: str, detail: str, status_code: int = 400, field: str | None = None):
        self.code = code
        self.detail = detail
        self.status_code = status_code
        self.field = field

async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.code, "detail": exc.detail, "field": exc.field},
    )
