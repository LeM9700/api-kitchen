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
