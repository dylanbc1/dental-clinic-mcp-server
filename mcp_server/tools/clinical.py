"""The clinical tool (scope: ``clinical``), the most restricted surface here.

Booking an appointment is administrative. Recording *why* the patient is coming
is clinical data, and in Colombia that pulls in Resolución 2654/2019, informed
consent, and RNBD registration with the SIC. That boundary, §2.5 of the spec,
is why this tool has its own scope rather than being another write.

Three gates stack, and each is necessary because the others do not cover it:

1. **Scope** ``clinical``. A `write` token cannot reach it. Authority to move an
   appointment is not authority to read or write a diagnosis.
2. **Human approval**, like every mutation.
3. **Recorded consent for this specific patient**, enforced by the backend. The
   caller can hold every scope and a fresh approval and still be refused,
   because consent belongs to the patient, not to the operator.
"""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from mcp_server.auth import Scope
from mcp_server.contexto import Contexto

SCOPE = Scope.CLINICAL


def registrar(servidor: MCPServer[Any], ctx: Contexto) -> None:
    @servidor.tool(
        name="registrar_motivo_consulta",
        title="Registrar el motivo de consulta (dato clínico · requiere confirmación)",
        description=(
            "PROPONE anotar el motivo de consulta o un antecedente en una cita. Esto es "
            "DATO CLÍNICO y está regulado (Resolución 2654/2019): exige el permiso "
            "'clinical', aprobación humana y consentimiento informado registrado del "
            "paciente. Si el paciente no tiene consentimiento, la operación será "
            "rechazada y NO debes insistir: pide que se registre el consentimiento "
            "primero. Transcribe lo que dice el paciente; nunca interpretes, diagnostiques "
            "ni sugieras tratamiento."
        ),
    )
    async def registrar_motivo_consulta(
        cita_id: Annotated[int, Field(gt=0)],
        motivo: Annotated[
            str,
            Field(
                min_length=3,
                max_length=500,
                description=(
                    "Lo que el paciente reporta, en sus términos. Sin diagnóstico ni "
                    "interpretación clínica."
                ),
            ),
        ],
    ) -> dict[str, Any]:
        identidad = ctx.autorizar("registrar_motivo_consulta", SCOPE)
        cita = await ctx.cliente.obtener(f"/citas/{cita_id}")

        ctx.auditor.acceso_clinico(sujeto=identidad.sujeto, cita_id=cita_id, resultado="propuesta")
        ctx.auditor.invocacion(
            "registrar_motivo_consulta",
            sujeto=identidad.sujeto,
            scope=str(SCOPE),
            argumentos={"cita_id": cita_id, "motivo": motivo},
            resultado="propuesta",
        )
        return ctx.proponer(
            "registrar_motivo_consulta",
            {"cita_id": cita_id, "motivo": motivo},
            resumen=(
                f"Registrar el motivo de consulta en la cita {cita_id} de "
                f"{cita['paciente']} ({cita['inicio_local']})."
            ),
            efectos=[
                "Se guardará el motivo de consulta asociado a la cita.",
                "Quedará registrado quién lo anotó y cuándo, en el log de auditoría.",
                "Se rechazará si el paciente no tiene consentimiento informado registrado.",
            ],
            sujeto=identidad.sujeto,
            advertencias=[
                "Esto es información clínica sujeta a la Resolución 2654/2019 y a la "
                "Ley 1581 de protección de datos. Confirma solo si el paciente autorizó "
                "el tratamiento de sus datos clínicos."
            ],
        )
