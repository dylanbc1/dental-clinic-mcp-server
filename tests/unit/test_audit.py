"""The tool-invocation audit log."""

from __future__ import annotations

import json
from typing import Any

import pytest

from mcp_server.audit import (
    CAMPOS_REDACTADOS,
    REDACTADO,
    Auditor,
    configurar_logging,
    redactar,
)

pytestmark = pytest.mark.security


class LoggerFalso:
    def __init__(self) -> None:
        self.llamadas: list[tuple[str, dict[str, Any]]] = []

    def info(self, evento: str, **datos: Any) -> None:
        self.llamadas.append((evento, datos))


class TestRedaccion:
    @pytest.mark.parametrize("campo", sorted(CAMPOS_REDACTADOS))
    def test_todo_campo_sensible_se_redacta(self, campo: str) -> None:
        assert redactar({campo: "valor real"})[campo] == REDACTADO

    def test_los_demas_campos_se_conservan(self) -> None:
        assert redactar({"cita_id": 7, "estado": "atendida"}) == {
            "cita_id": 7,
            "estado": "atendida",
        }

    def test_un_none_no_se_redacta_innecesariamente(self) -> None:
        """Redacting an absent value hides that it was absent."""
        assert redactar({"motivo": None}) == {"motivo": None}

    def test_el_motivo_de_consulta_esta_cubierto(self) -> None:
        # It is clinical data under Res. 2654/2019.
        assert "motivo" in CAMPOS_REDACTADOS

    def test_el_token_de_confirmacion_esta_cubierto(self) -> None:
        # A logged token is a replayable approval.
        assert "token_confirmacion" in CAMPOS_REDACTADOS


class TestAuditor:
    def test_registra_una_invocacion_exitosa(self) -> None:
        logger = LoggerFalso()
        auditor = Auditor(logger)
        auditor.invocacion(
            "buscar_paciente",
            sujeto="ana@clinica.test",
            scope="read",
            argumentos={"documento": "123"},
            resultado="ok",
        )
        evento, datos = logger.llamadas[-1]
        assert evento == "tool.invocacion"
        assert datos["sujeto"] == "ana@clinica.test"
        assert datos["argumentos"]["documento"] == REDACTADO

    def test_registra_el_codigo_de_error_cuando_lo_hay(self) -> None:
        auditor = Auditor(LoggerFalso())
        auditor.invocacion(
            "x",
            sujeto="a",
            scope="read",
            argumentos={},
            resultado="error",
            codigo_error="CITA_NO_ENCONTRADA",
        )
        assert auditor.eventos[-1]["codigo_error"] == "CITA_NO_ENCONTRADA"

    def test_marca_la_ejecucion_aprobada(self) -> None:
        auditor = Auditor(LoggerFalso())
        auditor.invocacion(
            "x", sujeto="a", scope="write", argumentos={}, resultado="ok", aprobada=True
        )
        assert auditor.eventos[-1]["con_aprobacion_humana"] is True

    def test_propuesta_y_confirmacion_son_eventos_distintos(self) -> None:
        auditor = Auditor(LoggerFalso())
        auditor.propuesta_emitida("cancelar_cita", sujeto="a", nonce="n1")
        auditor.propuesta_confirmada("cancelar_cita", sujeto="a", nonce="n1")
        assert [e["evento"] for e in auditor.eventos] == [
            "aprobacion.propuesta",
            "aprobacion.confirmada",
        ]

    def test_el_acceso_clinico_es_su_propio_tipo_de_evento(self) -> None:
        auditor = Auditor(LoggerFalso())
        auditor.acceso_clinico(sujeto="a", cita_id=7, resultado="registrado")
        assert auditor.eventos[-1] == {
            "evento": "clinico.acceso",
            "sujeto": "a",
            "cita_id": 7,
            "resultado": "registrado",
        }

    def test_la_memoria_esta_acotada(self) -> None:
        """An audit log is a log, not a data store: the in-memory mirror must
        not grow without bound in a long-running process."""
        auditor = Auditor(LoggerFalso())
        auditor.limite_memoria = 10
        for i in range(50):
            auditor.acceso_clinico(sujeto="a", cita_id=i, resultado="x")
        assert len(auditor.eventos) == 10
        assert auditor.eventos[-1]["cita_id"] == 49

    def test_los_eventos_son_serializables_a_json(self) -> None:
        """They are shipped to a log pipeline; an unserialisable value would be
        discovered in production rather than here."""
        auditor = Auditor(LoggerFalso())
        auditor.invocacion("x", sujeto="a", scope="read", argumentos={"cita_id": 1}, resultado="ok")
        json.dumps(auditor.eventos)


class TestConfiguracionDeLogging:
    def test_configurar_no_revienta_con_un_nivel_valido(self) -> None:
        configurar_logging("DEBUG")
        configurar_logging("INFO")

    def test_un_nivel_desconocido_cae_a_info(self) -> None:
        configurar_logging("NO-EXISTE")
