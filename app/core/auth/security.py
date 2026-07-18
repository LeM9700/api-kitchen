import hashlib
import hmac as _hmac
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from passlib.context import CryptContext

from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# [SECURITE] Hash bcrypt pre-calcule a l'import (cout unique ~300ms au demarrage).
# Utilise dans authenticate() pour egaliser le temps de reponse quand un email est
# introuvable -- evite le timing oracle qui revele l'existence d'un compte.
DUMMY_HASH: str = pwd_context.hash("__dummy_sentinel_for_timing_safety__")


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def get_password_hash(plain: str) -> str:
    return pwd_context.hash(plain)


def create_access_token(data: dict) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_access_expire_minutes)
    jti = str(uuid.uuid4())
    return jwt.encode(
        {**data, "exp": expire, "type": "access", "jti": jti},
        settings.jwt_secret,
        algorithm="HS256",
    )


def create_refresh_token(data: dict) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=settings.jwt_refresh_expire_days)
    return jwt.encode({**data, "exp": expire, "type": "refresh"}, settings.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])


def compute_token_lookup(token: str) -> str:
    """Calcule le HMAC-SHA256 d'un token refresh pour un lookup O(1) en base.

    Le digest est stocke dans la colonne indexee refresh_tokens.token_lookup.
    A la verification, on calcule ce meme digest depuis le token recu et on fait
    un SELECT WHERE token_lookup = ? au lieu de scanner tous les tokens bcrypt.

    [SECURITE] HMAC-SHA256 resiste aux attaques par timing et ne revele pas
    le token original. La cle HMAC est settings.jwt_hmac_secret (secret dedie,
    separe de jwt_secret) — fallback sur jwt_secret si non configure.

    [PERF] Le lookup passe de O(n*bcrypt) a O(1) index + 1 bcrypt.

    Args:
        token: Token refresh JWT en clair.

    Returns:
        Digest hexadecimal HMAC-SHA256 (64 caracteres).
    """
    hmac_secret = settings.jwt_hmac_secret or settings.jwt_secret
    return _hmac.new(
        hmac_secret.encode("utf-8"),
        token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
