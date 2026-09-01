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

VARIABLE_LLAVE = "OAUTH_PRIVATE_KEY_PEM"
TAMANO_LLAVE = 2048


def _b64uint(valor: int) -> str:
    crudo = valor.to_bytes((valor.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(crudo).decode().rstrip("=")


@dataclass(frozen=True, slots=True)
class ParDeLlaves:
    privada: rsa.RSAPrivateKey
    kid: str
    #: True when the key was generated in-process rather than provided.
    efimera: bool

    @property
    def publica(self) -> rsa.RSAPublicKey:
        return self.privada.public_key()

    def pem_privada(self) -> str:
        return self.privada.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()

    def jwk_publica(self) -> dict[str, Any]:
        numeros = self.publica.public_numbers()
        return {
            "kty": "RSA",
            "use": "sig",
            "alg": "RS256",
            "kid": self.kid,
            "n": _b64uint(numeros.n),
            "e": _b64uint(numeros.e),
        }

    def jwks(self) -> dict[str, Any]:
        return {"keys": [self.jwk_publica()]}


def generar() -> ParDeLlaves:
    privada = rsa.generate_private_key(public_exponent=65537, key_size=TAMANO_LLAVE)
    return ParDeLlaves(privada=privada, kid="dev-efimera", efimera=True)


def cargar_desde_entorno() -> ParDeLlaves | None:
    pem = os.getenv(VARIABLE_LLAVE)
    if not pem:
        return None
    privada = serialization.load_pem_private_key(pem.encode(), password=None)
    if not isinstance(privada, rsa.RSAPrivateKey):
        raise ValueError(f"{VARIABLE_LLAVE} no contiene una llave RSA privada")
    return ParDeLlaves(privada=privada, kid="entorno", efimera=False)


@lru_cache(maxsize=1)
def llaves() -> ParDeLlaves:
    return cargar_desde_entorno() or generar()
