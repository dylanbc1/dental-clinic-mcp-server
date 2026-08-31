"""The clinical tool over MRTR, with three gates stacked.

Scope, human approval, and recorded consent. The test that matters most is the
one where the caller holds every scope and a person approved, and it is *still*
refused, because consent belongs to the patient rather than to the operator.
"""

from typing import Any

import pytest
from sqlalchemy.orm import Session

from backend.domain.servicios import agendar_cita, obtener_cita
from tests.conftest import SUJETO, ClienteMCP, ErrorDeHerramienta, Escenario, como

pytestmark = [pytest.mark.integration, pytest.mark.security]

CLINICO = ["read", "write", "clinical"]


@pytest.fixture
def cita_con_consentimiento(sesion_backend: Session, escenario: Escenario) -> int:
    """Ana has consent on file."""
    cita = agendar_cita(
        sesion_backend,
        paciente_id=escenario.ana_id,
        slot_id=escenario.slots_general[0],
        usuario="setup",
    ).cita
    sesion_backend.commit()
    return cita.id


@pytest.fixture
def cita_sin_consentimiento(sesion_backend: Session, escenario: Escenario) -> int:
    """Carla does not."""
    cita = agendar_cita(
        sesion_backend,
        paciente_id=escenario.carla_id,
        slot_id=escenario.slots_general[1],
        usuario="setup",
    ).cita
    sesion_backend.commit()
    return cita.id


class TestConConsentimiento:
    async def test_el_ciclo_completo_registra_el_motivo(
        self, mcp: ClienteMCP, sesion_backend: Session, cita_con_consentimiento: int
    ) -> None:
        args = {"cita_id": cita_con_consentimiento, "motivo": "Dolor en molar inferior"}
        with como(SUJETO, CLINICO):
            resultado = await mcp.aprobar("registrar_motivo_consulta", args)

        assert resultado["motivo"] == "Dolor en molar inferior"
        sesion_backend.expire_all()
        assert obtener_cita(sesion_backend, cita_con_consentimiento).motivo_registrado_por == SUJETO

    async def test_la_pregunta_advierte_de_la_regulacion(
        self, mcp: ClienteMCP, cita_con_consentimiento: int
    ) -> None:
        args = {"cita_id": cita_con_consentimiento, "motivo": "Dolor"}
        with como(SUJETO, CLINICO):
            mensaje = mcp.mensaje_de(await mcp.preguntar("registrar_motivo_consulta", args))
        assert "2654" in mensaje
        assert "1581" in mensaje

    async def test_preguntar_no_escribe_nada_todavia(
        self, mcp: ClienteMCP, sesion_backend: Session, cita_con_consentimiento: int
    ) -> None:
        args = {"cita_id": cita_con_consentimiento, "motivo": "Dolor agudo"}
        with como(SUJETO, CLINICO):
            await mcp.preguntar("registrar_motivo_consulta", args)
        sesion_backend.expire_all()
        assert obtener_cita(sesion_backend, cita_con_consentimiento).motivo is None


class TestSinConsentimiento:
    async def test_ni_el_scope_ni_la_aprobacion_alcanzan(
        self, mcp: ClienteMCP, cita_sin_consentimiento: int
    ) -> None:
        """Every gate open except the patient's own authorisation, and that is
        the one that must still stop it."""
        args = {"cita_id": cita_sin_consentimiento, "motivo": "Dolor"}
        with como(SUJETO, CLINICO), pytest.raises(ErrorDeHerramienta) as exc:
            await mcp.aprobar("registrar_motivo_consulta", args)
        assert "CONSENTIMIENTO_REQUERIDO" in exc.value.texto
        assert "2654" in exc.value.texto
        assert "Acción requerida" in exc.value.texto

    async def test_el_rechazo_no_deja_el_motivo_escrito(
        self, mcp: ClienteMCP, sesion_backend: Session, cita_sin_consentimiento: int
    ) -> None:
        args = {"cita_id": cita_sin_consentimiento, "motivo": "Dolor severo"}
        with como(SUJETO, CLINICO), pytest.raises(ErrorDeHerramienta):
            await mcp.aprobar("registrar_motivo_consulta", args)
        sesion_backend.expire_all()
        assert obtener_cita(sesion_backend, cita_sin_consentimiento).motivo is None


class TestAuditoriaClinica:
    async def test_el_acceso_clinico_tiene_su_propio_evento(
        self, mcp: ClienteMCP, ctx: Any, cita_con_consentimiento: int
    ) -> None:
        """Res. 2654 asks who touched clinical data. Burying that in the generic
        invocation stream makes it unanswerable at audit time."""
        args = {"cita_id": cita_con_consentimiento, "motivo": "Control"}
        with como("odontologa@clinica.test", CLINICO):
            await mcp.aprobar("registrar_motivo_consulta", args)

        clinicos = [e for e in ctx.auditor.eventos if e["evento"] == "clinico.acceso"]
        assert clinicos[-1]["resultado"] == "registrado"
        assert all(e["sujeto"] == "odontologa@clinica.test" for e in clinicos)
        assert all(e["cita_id"] == cita_con_consentimiento for e in clinicos)

    async def test_un_rechazo_tambien_queda_auditado(
        self, mcp: ClienteMCP, ctx: Any, cita_sin_consentimiento: int
    ) -> None:
        args = {"cita_id": cita_sin_consentimiento, "motivo": "Dolor"}
        with como(SUJETO, CLINICO), pytest.raises(ErrorDeHerramienta):
            await mcp.aprobar("registrar_motivo_consulta", args)
        clinicos = [e for e in ctx.auditor.eventos if e["evento"] == "clinico.acceso"]
        assert clinicos[-1]["resultado"] == "rechazado:CONSENTIMIENTO_REQUERIDO"

    async def test_el_motivo_no_se_copia_al_log(
        self, mcp: ClienteMCP, ctx: Any, cita_con_consentimiento: int
    ) -> None:
        """The reason for consultation is the clinical datum itself. Auditing
        the access must not duplicate it somewhere less protected."""
        secreto = "sangrado gingival persistente hace tres semanas"
        with como(SUJETO, CLINICO):
            await mcp.aprobar(
                "registrar_motivo_consulta",
                {"cita_id": cita_con_consentimiento, "motivo": secreto},
            )
        assert secreto not in str(ctx.auditor.eventos)
        assert any(
            e.get("argumentos", {}).get("motivo") == "«redactado»"
            for e in ctx.auditor.eventos
            if e["evento"] == "tool.invocacion"
        )

    async def test_el_motivo_clinico_no_viaja_en_el_mensaje_al_humano(
        self, mcp: ClienteMCP, cita_con_consentimiento: int
    ) -> None:
        """The person approving needs to know *that* a reason is being recorded,
        and for whom. The reason itself is the patient's, and echoing it back
        through the client would put clinical data in one more place."""
        secreto = "absceso periapical según el paciente"
        with como(SUJETO, CLINICO):
            mensaje = mcp.mensaje_de(
                await mcp.preguntar(
                    "registrar_motivo_consulta",
                    {"cita_id": cita_con_consentimiento, "motivo": secreto},
                )
            )
        assert secreto not in mensaje
        assert "motivo de consulta" in mensaje
