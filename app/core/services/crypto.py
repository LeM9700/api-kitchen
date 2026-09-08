"""Chiffrement symetrique (Fernet) pour les secrets stockes en base -- tokens
OAuth de connexion POS. Cle unique lue depuis settings.pos_token_encryption_key.

[SECURITE] Contrairement a app.core.services.cache (qui ne leve jamais), ces
fonctions levent une exception en cas d'echec -- fail closed : un secret qui
ne peut pas etre chiffre ne doit jamais etre persiste en clair, et un secret
qui ne peut pas etre dechiffre ne doit jamais etre traite comme une chaine
vide silencieuse.
"""
from cryptography.fernet import Fernet

from app.core.config import settings


class CryptoNotConfigured(RuntimeError):
    """Levee quand pos_token_encryption_key est vide (feature desactivee)."""


def _fernet() -> Fernet:
    if not settings.pos_token_encryption_key:
        raise CryptoNotConfigured(
            "POS_TOKEN_ENCRYPTION_KEY n'est pas configure -- impossible de "
            "chiffrer/dechiffrer un secret."
        )
    return Fernet(settings.pos_token_encryption_key.encode())


def encrypt_secret(plaintext: str) -> str:
    """Chiffre une chaine en clair et retourne le texte chiffre (str base64).

    Args:
        plaintext: Secret en clair (ex: access_token OAuth).

    Returns:
        Texte chiffre, pret a etre persiste tel quel en base.

    Raises:
        CryptoNotConfigured: si aucune cle n'est configuree.
    """
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    """Dechiffre un secret precedemment chiffre par encrypt_secret.

    Args:
        ciphertext: Texte chiffre tel que stocke en base.

    Returns:
        Secret en clair.

    Raises:
        CryptoNotConfigured: si aucune cle n'est configuree.
        cryptography.fernet.InvalidToken: si le texte est corrompu ou signe
            avec une autre cle.
    """
    return _fernet().decrypt(ciphertext.encode()).decode()


def _super_admin_mfa_fernet() -> Fernet:
    # [SECURITE] Cle DEDIEE, distincte de pos_token_encryption_key -- le secret
    # TOTP d'un super-admin plateforme n'a pas le meme blast radius qu'un token
    # OAuth POS ; partager la cle coupletait deux categories de secrets sans
    # rapport (une rotation de l'une invaliderait silencieusement l'autre).
    if not settings.super_admin_mfa_encryption_key:
        raise CryptoNotConfigured(
            "SUPER_ADMIN_MFA_ENCRYPTION_KEY n'est pas configure -- impossible de "
            "chiffrer/dechiffrer le secret MFA super-admin."
        )
    return Fernet(settings.super_admin_mfa_encryption_key.encode())


def encrypt_super_admin_mfa_secret(plaintext: str) -> str:
    """Chiffre le secret TOTP d'un super-admin plateforme avant persistance.

    Args:
        plaintext: Secret TOTP base32 en clair (pyotp.random_base32()).

    Returns:
        Texte chiffre (str base64), pret a etre persiste dans
        ``super_admins.mfa_secret_encrypted``.

    Raises:
        CryptoNotConfigured: si SUPER_ADMIN_MFA_ENCRYPTION_KEY est vide -- fail
        closed : un secret MFA super-admin ne doit jamais etre persiste en clair.
    """
    return _super_admin_mfa_fernet().encrypt(plaintext.encode()).decode()


def decrypt_super_admin_mfa_secret(ciphertext: str) -> str:
    """Dechiffre le secret TOTP d'un super-admin plateforme.

    Args:
        ciphertext: Valeur stockee dans ``super_admins.mfa_secret_encrypted``.

    Returns:
        Secret TOTP en clair, pret pour ``pyotp.TOTP(secret)``.

    Raises:
        CryptoNotConfigured: si SUPER_ADMIN_MFA_ENCRYPTION_KEY est vide.
        cryptography.fernet.InvalidToken: si le texte est corrompu ou signe
            avec une autre cle.
    """
    return _super_admin_mfa_fernet().decrypt(ciphertext.encode()).decode()


def _tenant_mfa_fernet() -> Fernet:
    # [SECURITE] Cle DEDIEE, distincte de super_admin_mfa_encryption_key ET de
    # pos_token_encryption_key -- voir le commentaire de
    # settings.tenant_mfa_encryption_key pour le detail du blast radius.
    if not settings.tenant_mfa_encryption_key:
        raise CryptoNotConfigured(
            "TENANT_MFA_ENCRYPTION_KEY n'est pas configure -- impossible de "
            "chiffrer/dechiffrer le secret MFA d'un compte tenant."
        )
    return Fernet(settings.tenant_mfa_encryption_key.encode())


def encrypt_tenant_mfa_secret(plaintext: str) -> str:
    """Chiffre le secret TOTP d'un compte admin/staff tenant avant persistance.

    Args:
        plaintext: Secret TOTP base32 en clair (pyotp.random_base32()).

    Returns:
        Texte chiffre (str base64), pret a etre persiste dans
        ``users.mfa_secret``.

    Raises:
        CryptoNotConfigured: si TENANT_MFA_ENCRYPTION_KEY est vide -- fail
        closed : un secret MFA ne doit jamais etre persiste en clair.
    """
    return _tenant_mfa_fernet().encrypt(plaintext.encode()).decode()


def decrypt_tenant_mfa_secret(ciphertext: str) -> str:
    """Dechiffre le secret TOTP d'un compte admin/staff tenant.

    Args:
        ciphertext: Valeur stockee dans ``users.mfa_secret``.

    Returns:
        Secret TOTP en clair, pret pour ``pyotp.TOTP(secret)``.

    Raises:
        CryptoNotConfigured: si TENANT_MFA_ENCRYPTION_KEY est vide.
        cryptography.fernet.InvalidToken: si le texte est corrompu ou signe
            avec une autre cle.
    """
    return _tenant_mfa_fernet().decrypt(ciphertext.encode()).decode()
