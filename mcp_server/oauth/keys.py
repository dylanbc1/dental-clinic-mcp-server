"""RSA signing keys for the authorization server.

A key is loaded from `OAUTH_PRIVATE_KEY_PEM` when present and generated in
memory otherwise. The generated case is for development and tests only, and it
is loud about it: every restart invalidates every outstanding token, which is
exactly the behaviour you want from a key nobody persisted on purpose.

There is no key material in this repository, and no code path that would write
one to disk.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

KEY_ENV_VAR = "OAUTH_PRIVATE_KEY_PEM"
KEY_SIZE = 2048


def _b64uint(value: int) -> str:
    crudo = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(crudo).decode().rstrip("=")


@dataclass(frozen=True, slots=True)
class KeyPair:
    private: rsa.RSAPrivateKey
    kid: str
    #: True when the key was generated in-process rather than provided.
    ephemeral: bool

    @property
    def public_key(self) -> rsa.RSAPublicKey:
        return self.private.public_key()

    def private_pem(self) -> str:
        return self.private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()

    def public_jwk(self) -> dict[str, Any]:
        numeros = self.public_key.public_numbers()
        return {
            "kty": "RSA",
            "use": "sig",
            "alg": "RS256",
            "kid": self.kid,
            "n": _b64uint(numeros.n),
            "e": _b64uint(numeros.e),
        }

    def jwks(self) -> dict[str, Any]:
        return {"keys": [self.public_jwk()]}


def generate() -> KeyPair:
    private = rsa.generate_private_key(public_exponent=65537, key_size=KEY_SIZE)
    return KeyPair(private=private, kid="dev-efimera", ephemeral=True)


def load_from_env() -> KeyPair | None:
    pem = os.getenv(KEY_ENV_VAR)
    if not pem:
        return None
    private = serialization.load_pem_private_key(pem.encode(), password=None)
    if not isinstance(private, rsa.RSAPrivateKey):
        raise ValueError(f"{KEY_ENV_VAR} no contiene una llave RSA privada")
    return KeyPair(private=private, kid="entorno", ephemeral=False)


@lru_cache(maxsize=1)
def signing_keys() -> KeyPair:
    return load_from_env() or generate()
