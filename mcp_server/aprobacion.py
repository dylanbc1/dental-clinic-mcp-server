"""Security layer 3: human-in-the-loop.

The failure this prevents is documented and recent. In July 2025 an AI agent
deleted a production database at SaaStr during a code freeze: it held the
permission, so it acted. Scopes alone would not have stopped it, because the
token was valid. What was missing was a human between intent and effect.

So every write and clinical tool is two-phase:

1. The tool **proposes**. It returns plain language describing exactly what
   would happen, plus a signed confirmation token. Nothing has changed.
2. A human reads it and confirms, which calls `confirmar_operacion` with that
   token. Only then does the mutation run.

The token is what makes phase 2 safe to expose as a tool:

* **Signed** (HMAC-SHA256), so the model cannot forge one or edit the arguments
  of a proposal it was handed. Confirming approves that action with those
  arguments, not "the last thing discussed".
* **Single-use**, so a replayed token cannot cancel the same appointment twice.
* **Short-lived**, so an approval from five minutes ago does not authorise an
  action taken tomorrow.
* **Bound to the caller**, so one subject's token cannot be redeemed by another.

The consumed-token store is in-process, which is honest for one replica and is
called out in `docs/security.md`. Behind several it belongs in Redis, and the
interface here is narrow enough to swap.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from backend.domain.errores import CodigoError
from mcp_server.errores import ErrorHerramienta

#: Bumping this invalidates every outstanding token, which is correct when the
#: payload's meaning changes.
VERSION = 1


def _b64(datos: bytes) -> str:
    return base64.urlsafe_b64encode(datos).decode().rstrip("=")


def _de_b64(texto: str) -> bytes:
    relleno = "=" * (-len(texto) % 4)
    return base64.urlsafe_b64decode(texto + relleno)


@dataclass(frozen=True, slots=True)
class Propuesta:
    """What the model is asking a human to approve."""

    accion: str
    argumentos: dict[str, Any]
    resumen: str
    efectos: list[str]
    sujeto: str
    emitida_en: int
    nonce: str
    advertencias: list[str] = field(default_factory=list)

    def payload(self) -> dict[str, Any]:
        return {
            "v": VERSION,
            "accion": self.accion,
            "argumentos": self.argumentos,
            "sujeto": self.sujeto,
            "iat": self.emitida_en,
            "nonce": self.nonce,
        }


@dataclass(frozen=True, slots=True)
class OperacionAprobada:
    """The verified content of a confirmation token."""

    accion: str
    argumentos: dict[str, Any]
    sujeto: str
    nonce: str


class RegistroDeUsos:
    """Remembers spent nonces so a confirmation cannot be replayed.

    Entries older than the TTL are dropped: a token that can no longer be
    redeemed does not need remembering.
    """

    def __init__(self, ttl_segundos: int) -> None:
        self._ttl = ttl_segundos
        self._usados: dict[str, float] = {}

    def _purgar(self, ahora: float) -> None:
        limite = ahora - self._ttl
        vencidos = [n for n, t in self._usados.items() if t < limite]
        for nonce in vencidos:
            del self._usados[nonce]

    def ya_usado(self, nonce: str, *, ahora: float | None = None) -> bool:
        momento = ahora if ahora is not None else time.time()
        self._purgar(momento)
        return nonce in self._usados

    def marcar(self, nonce: str, *, ahora: float | None = None) -> None:
        momento = ahora if ahora is not None else time.time()
        self._usados[nonce] = momento

    def __len__(self) -> int:
        return len(self._usados)


class GestorDeAprobaciones:
    """Mints and redeems confirmation tokens."""

    def __init__(self, clave: str, *, ttl_segundos: int = 300) -> None:
        if not clave:
            raise ValueError("la clave de firma de aprobaciones no puede estar vacía")
        self._clave = clave.encode()
        self.ttl = ttl_segundos
        self.usos = RegistroDeUsos(ttl_segundos)

    # --- minting -----------------------------------------------------------

    def _firmar(self, cuerpo: bytes) -> str:
        return _b64(hmac.new(self._clave, cuerpo, hashlib.sha256).digest())

    def proponer(
        self,
        accion: str,
        argumentos: dict[str, Any],
        *,
        resumen: str,
        efectos: list[str],
        sujeto: str,
        advertencias: list[str] | None = None,
        ahora: float | None = None,
    ) -> tuple[Propuesta, str]:
        propuesta = Propuesta(
            accion=accion,
            argumentos=argumentos,
            resumen=resumen,
            efectos=efectos,
            sujeto=sujeto,
            emitida_en=int(ahora if ahora is not None else time.time()),
            nonce=secrets.token_urlsafe(12),
            advertencias=advertencias or [],
        )
        cuerpo = json.dumps(
            propuesta.payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        token = f"{_b64(cuerpo)}.{self._firmar(cuerpo)}"
        return propuesta, token

    # --- redeeming ---------------------------------------------------------

    def verificar(
        self, token: str, *, sujeto: str, ahora: float | None = None
    ) -> OperacionAprobada:
        """Validate and spend a confirmation token.

        Signature first. Reporting "expired" for a forged token would confirm to
        an attacker that their forgery parsed.
        """
        momento = ahora if ahora is not None else time.time()

        try:
            cuerpo_b64, firma = token.strip().split(".", 1)
            cuerpo = _de_b64(cuerpo_b64)
        except (ValueError, TypeError) as exc:
            raise self._invalido("El token de confirmación está malformado.") from exc

        if not hmac.compare_digest(firma, self._firmar(cuerpo)):
            raise self._invalido("La firma del token de confirmación no es válida.")

        try:
            datos = json.loads(cuerpo)
        except json.JSONDecodeError as exc:  # pragma: no cover - signature covers this
            raise self._invalido("El contenido del token no es legible.") from exc

        if datos.get("v") != VERSION:
            raise self._invalido("El token de confirmación pertenece a otra versión.")

        if momento - float(datos["iat"]) > self.ttl:
            raise ErrorHerramienta(
                str(CodigoError.APROBACION_EXPIRADA),
                f"La aprobación expiró (vigencia: {self.ttl} segundos).",
                sugerencia=(
                    "Vuelve a llamar la herramienta para generar una propuesta nueva "
                    "y pide confirmación otra vez."
                ),
            )

        if datos["sujeto"] != sujeto:
            # A confirmation approved for one identity is not transferable.
            raise self._invalido("El token de confirmación fue emitido para otro usuario.")

        nonce = str(datos["nonce"])
        if self.usos.ya_usado(nonce, ahora=momento):
            raise ErrorHerramienta(
                str(CodigoError.APROBACION_YA_USADA),
                "Esa confirmación ya se usó.",
                sugerencia=(
                    "La operación ya se ejecutó. Verifica el estado actual antes de "
                    "intentar repetirla; no reenvíes la misma confirmación."
                ),
            )
        self.usos.marcar(nonce, ahora=momento)

        return OperacionAprobada(
            accion=str(datos["accion"]),
            argumentos=dict(datos["argumentos"]),
            sujeto=str(datos["sujeto"]),
            nonce=nonce,
        )

    @staticmethod
    def _invalido(mensaje: str) -> ErrorHerramienta:
        return ErrorHerramienta(
            str(CodigoError.APROBACION_INVALIDA),
            mensaje,
            sugerencia=(
                "Genera una propuesta nueva llamando la herramienta correspondiente; "
                "no construyas ni edites tokens de confirmación a mano."
            ),
        )


def formatear_propuesta(propuesta: Propuesta, token: str, ttl: int) -> dict[str, Any]:
    """The payload a write tool returns instead of acting.

    Written to be read aloud. The person approving is a receptionist, not an
    engineer, and the list of effects is what they are consenting to.
    """
    return {
        "requiere_confirmacion": True,
        "accion": propuesta.accion,
        "resumen": propuesta.resumen,
        "esto_va_a_pasar": propuesta.efectos,
        "advertencias": propuesta.advertencias,
        "argumentos": propuesta.argumentos,
        "token_confirmacion": token,
        "vigencia_segundos": ttl,
        "siguiente_paso": (
            "Muestra este resumen a la persona responsable. Si lo aprueba, llama "
            "confirmar_operacion con el token_confirmacion tal cual. Si no, no llames "
            "nada más: no se ha modificado ningún dato."
        ),
    }
