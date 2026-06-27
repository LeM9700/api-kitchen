from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Middleware Starlette qui injecte les headers de sécurité HTTP sur chaque réponse.

    Headers appliqués :
    - ``X-Content-Type-Options`` : empêche le MIME-sniffing.
    - ``X-Frame-Options`` : bloque le clickjacking via iframes.
    - ``X-XSS-Protection`` : active le filtre XSS des navigateurs legacy.
    - ``Strict-Transport-Security`` : force HTTPS pour 1 an (includeSubDomains).
    - ``Referrer-Policy`` : limite les infos envoyées au referrer cross-origin.
    - ``Permissions-Policy`` : désactive les APIs sensibles (géoloc, micro, caméra).
    - ``Content-Security-Policy`` : restrictif (default-src 'none') car l'API ne sert que du JSON.

    Headers supprimés :
    - ``server`` et ``x-powered-by`` : évitent de révéler la stack technique.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        # CSP permissif pour une API JSON pure — aucun HTML/script servi.
        response.headers["Content-Security-Policy"] = "default-src 'none'"
        # Suppression des headers qui révèlent la stack technique.
        if "server" in response.headers:
            del response.headers["server"]
        if "x-powered-by" in response.headers:
            del response.headers["x-powered-by"]
        return response
