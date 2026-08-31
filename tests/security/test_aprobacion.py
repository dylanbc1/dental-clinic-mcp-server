"""Security layer 3: the approval token, attacked directly.

These run without a database: the token is a self-contained cryptographic
object, and its guarantees should hold without any system state. Each test is
one way an agent, or someone who has read the source, might try to act without
a human having approved it.
"""

from __future__ import annotations

import base64
import json
import time

import pytest

from mcp_server.aprobacion import (
    VERSION,
    GestorDeAprobaciones,
    RegistroDeUsos,
    formatear_propuesta,
)
from mcp_server.errores import ErrorHerramienta

pytestmark = pytest.mark.security

CLAVE = "clave-de-pruebas"
OTRA_CLAVE = "otra-clave-distinta"
SUJETO = "recepcion@clinica.test"


@pytest.fixture
def gestor() -> GestorDeAprobaciones:
    return GestorDeAprobaciones(CLAVE, ttl_segundos=300)


def emitir(gestor: GestorDeAprobaciones, **extra: object) -> str:
    _, token = gestor.proponer(
        "cancelar_cita",
        {"cita_id": 7, "motivo": "el paciente viajó"},
        resumen="Cancelar la cita 7.",
        efectos=["Se libera el cupo."],
        sujeto=SUJETO,
        **extra,  # type: ignore[arg-type]
    )
    return token


class TestCicloFeliz:
    def test_un_token_recien_emitido_se_puede_canjear(self, gestor: GestorDeAprobaciones) -> None:
        aprobada = gestor.verificar(emitir(gestor), sujeto=SUJETO)
        assert aprobada.accion == "cancelar_cita"
        assert aprobada.argumentos == {"cita_id": 7, "motivo": "el paciente viajó"}
        assert aprobada.sujeto == SUJETO

    def test_cada_propuesta_tiene_un_nonce_distinto(self, gestor: GestorDeAprobaciones) -> None:
        assert emitir(gestor) != emitir(gestor)

    def test_la_clave_no_viaja_en_el_token(self, gestor: GestorDeAprobaciones) -> None:
        assert CLAVE not in emitir(gestor)


class TestFirma:
    def test_un_token_con_la_firma_cambiada_se_rechaza(self, gestor: GestorDeAprobaciones) -> None:
        cuerpo, _ = emitir(gestor).split(".", 1)
        with pytest.raises(ErrorHerramienta) as exc:
            gestor.verificar(f"{cuerpo}.firmaInventada", sujeto=SUJETO)
        assert exc.value.codigo == "APROBACION_INVALIDA"

    def test_no_se_pueden_editar_los_argumentos_aprobados(
        self, gestor: GestorDeAprobaciones
    ) -> None:
        """The attack this defends against: approve cancelling appointment 7,
        then redeem a token that says appointment 99."""
        cuerpo_b64, firma = emitir(gestor).split(".", 1)
        relleno = "=" * (-len(cuerpo_b64) % 4)
        datos = json.loads(base64.urlsafe_b64decode(cuerpo_b64 + relleno))
        datos["argumentos"]["cita_id"] = 99
        alterado = (
            base64.urlsafe_b64encode(
                json.dumps(datos, sort_keys=True, separators=(",", ":")).encode()
            )
            .decode()
            .rstrip("=")
        )

        with pytest.raises(ErrorHerramienta) as exc:
            gestor.verificar(f"{alterado}.{firma}", sujeto=SUJETO)
        assert exc.value.codigo == "APROBACION_INVALIDA"

    def test_no_se_puede_cambiar_la_accion_aprobada(self, gestor: GestorDeAprobaciones) -> None:
        cuerpo_b64, firma = emitir(gestor).split(".", 1)
        relleno = "=" * (-len(cuerpo_b64) % 4)
        datos = json.loads(base64.urlsafe_b64decode(cuerpo_b64 + relleno))
        datos["accion"] = "registrar_motivo_consulta"
        alterado = (
            base64.urlsafe_b64encode(
                json.dumps(datos, sort_keys=True, separators=(",", ":")).encode()
            )
            .decode()
            .rstrip("=")
        )
        with pytest.raises(ErrorHerramienta):
            gestor.verificar(f"{alterado}.{firma}", sujeto=SUJETO)

    def test_un_token_de_otro_servidor_no_sirve(self) -> None:
        ajeno = GestorDeAprobaciones(OTRA_CLAVE)
        token = emitir(ajeno)
        with pytest.raises(ErrorHerramienta):
            GestorDeAprobaciones(CLAVE).verificar(token, sujeto=SUJETO)

    @pytest.mark.parametrize("basura", ["", "sin-punto", "a.b", "....", "no-es-base64.tampoco"])
    def test_un_token_malformado_no_revienta_el_servidor(
        self, gestor: GestorDeAprobaciones, basura: str
    ) -> None:
        with pytest.raises(ErrorHerramienta) as exc:
            gestor.verificar(basura, sujeto=SUJETO)
        assert exc.value.codigo == "APROBACION_INVALIDA"

    def test_una_clave_vacia_no_se_acepta_al_construir(self) -> None:
        with pytest.raises(ValueError, match="clave"):
            GestorDeAprobaciones("")


class TestUnSoloUso:
    def test_el_segundo_canje_se_rechaza(self, gestor: GestorDeAprobaciones) -> None:
        """Without this, an agent cancels the same appointment twice by
        resending one confirmation."""
        token = emitir(gestor)
        gestor.verificar(token, sujeto=SUJETO)
        with pytest.raises(ErrorHerramienta) as exc:
            gestor.verificar(token, sujeto=SUJETO)
        assert exc.value.codigo == "APROBACION_YA_USADA"

    def test_el_mensaje_dice_que_no_reintente(self, gestor: GestorDeAprobaciones) -> None:
        token = emitir(gestor)
        gestor.verificar(token, sujeto=SUJETO)
        with pytest.raises(ErrorHerramienta) as exc:
            gestor.verificar(token, sujeto=SUJETO)
        assert "no reenvíes" in (exc.value.sugerencia or "")

    def test_dos_tokens_distintos_no_se_estorban(self, gestor: GestorDeAprobaciones) -> None:
        primero, segundo = emitir(gestor), emitir(gestor)
        gestor.verificar(primero, sujeto=SUJETO)
        gestor.verificar(segundo, sujeto=SUJETO)


class TestExpiracion:
    def test_un_token_vencido_se_rechaza(self) -> None:
        gestor = GestorDeAprobaciones(CLAVE, ttl_segundos=60)
        ahora = time.time()
        _, token = gestor.proponer(
            "confirmar_cita",
            {"cita_id": 1},
            resumen="x",
            efectos=[],
            sujeto=SUJETO,
            ahora=ahora,
        )
        with pytest.raises(ErrorHerramienta) as exc:
            gestor.verificar(token, sujeto=SUJETO, ahora=ahora + 61)
        assert exc.value.codigo == "APROBACION_EXPIRADA"

    def test_justo_dentro_del_plazo_todavia_sirve(self) -> None:
        gestor = GestorDeAprobaciones(CLAVE, ttl_segundos=60)
        ahora = time.time()
        _, token = gestor.proponer(
            "confirmar_cita",
            {"cita_id": 1},
            resumen="x",
            efectos=[],
            sujeto=SUJETO,
            ahora=ahora,
        )
        gestor.verificar(token, sujeto=SUJETO, ahora=ahora + 59)

    def test_la_expiracion_se_comprueba_despues_de_la_firma(
        self, gestor: GestorDeAprobaciones
    ) -> None:
        """Reporting 'expired' for a forgery would confirm to an attacker that
        their forged payload parsed."""
        cuerpo, _ = emitir(gestor).split(".", 1)
        with pytest.raises(ErrorHerramienta) as exc:
            gestor.verificar(f"{cuerpo}.firmaFalsa", sujeto=SUJETO, ahora=time.time() + 10_000)
        assert exc.value.codigo == "APROBACION_INVALIDA"


class TestAtadoAlSujeto:
    def test_otro_usuario_no_puede_canjear_mi_aprobacion(
        self, gestor: GestorDeAprobaciones
    ) -> None:
        with pytest.raises(ErrorHerramienta) as exc:
            gestor.verificar(emitir(gestor), sujeto="otro@clinica.test")
        assert exc.value.codigo == "APROBACION_INVALIDA"

    def test_no_se_gasta_el_nonce_al_fallar_por_sujeto(self, gestor: GestorDeAprobaciones) -> None:
        """A failed redemption by the wrong user must not burn the token: that
        would be a denial-of-service on the legitimate approver."""
        token = emitir(gestor)
        with pytest.raises(ErrorHerramienta):
            gestor.verificar(token, sujeto="intruso@clinica.test")
        assert gestor.verificar(token, sujeto=SUJETO).accion == "cancelar_cita"


class TestVersionado:
    def test_un_payload_de_otra_version_se_rechaza(self, gestor: GestorDeAprobaciones) -> None:
        cuerpo = json.dumps(
            {
                "v": VERSION + 1,
                "accion": "cancelar_cita",
                "argumentos": {},
                "sujeto": SUJETO,
                "iat": int(time.time()),
                "nonce": "x",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        b64 = base64.urlsafe_b64encode(cuerpo).decode().rstrip("=")
        firma = gestor._firmar(cuerpo)
        with pytest.raises(ErrorHerramienta) as exc:
            gestor.verificar(f"{b64}.{firma}", sujeto=SUJETO)
        assert exc.value.codigo == "APROBACION_INVALIDA"


class TestRegistroDeUsos:
    def test_recuerda_lo_usado(self) -> None:
        registro = RegistroDeUsos(ttl_segundos=60)
        assert not registro.ya_usado("abc", ahora=0)
        registro.marcar("abc", ahora=0)
        assert registro.ya_usado("abc", ahora=1)

    def test_olvida_lo_que_ya_no_podria_canjearse(self) -> None:
        """A token that can no longer be redeemed does not need remembering,
        otherwise the store grows without bound."""
        registro = RegistroDeUsos(ttl_segundos=60)
        registro.marcar("abc", ahora=0)
        assert not registro.ya_usado("abc", ahora=1000)
        assert len(registro) == 0


class TestFormatoDeLaPropuesta:
    def test_la_propuesta_es_legible_para_una_persona(self, gestor: GestorDeAprobaciones) -> None:
        propuesta, token = gestor.proponer(
            "cancelar_cita",
            {"cita_id": 7, "motivo": "viaje"},
            resumen="Cancelar la cita 7 de Ana Gómez.",
            efectos=["El cupo quedará libre."],
            sujeto=SUJETO,
            advertencias=["El paciente tiene saldo en mora."],
        )
        payload = formatear_propuesta(propuesta, token, gestor.ttl)

        assert payload["requiere_confirmacion"] is True
        assert payload["resumen"].startswith("Cancelar")
        assert payload["esto_va_a_pasar"] == ["El cupo quedará libre."]
        assert payload["advertencias"] == ["El paciente tiene saldo en mora."]
        assert payload["vigencia_segundos"] == gestor.ttl
        assert "confirmar_operacion" in payload["siguiente_paso"]

    def test_deja_claro_que_todavia_no_paso_nada(self, gestor: GestorDeAprobaciones) -> None:
        propuesta, token = gestor.proponer(
            "agendar_cita", {}, resumen="x", efectos=[], sujeto=SUJETO
        )
        assert (
            "no se ha modificado"
            in formatear_propuesta(propuesta, token, gestor.ttl)["siguiente_paso"]
        )
