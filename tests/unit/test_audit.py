"""The tool-invocation audit log."""

from __future__ import annotations

import json
from typing import Any

import pytest

from mcp_server.audit import (
    REDACTED,
    REDACTED_FIELDS,
    Auditor,
    configure_logging,
    redact,
)

pytestmark = pytest.mark.security


class LoggerFalso:
    def __init__(self) -> None:
        self.llamadas: list[tuple[str, dict[str, Any]]] = []

    def info(self, evento: str, **payload: Any) -> None:
        self.llamadas.append((evento, payload))


class TestRedaccion:
    @pytest.mark.parametrize("campo", sorted(REDACTED_FIELDS))
    def test_todo_campo_sensible_se_redacta(self, campo: str) -> None:
        assert redact({campo: "valor real"})[campo] == REDACTED

    def test_los_demas_campos_se_conservan(self) -> None:
        assert redact({"cita_id": 7, "estado": "attended"}) == {
            "cita_id": 7,
            "estado": "attended",
        }

    def test_un_none_no_se_redacta_innecesariamente(self) -> None:
        """Redacting an absent value hides that it was absent."""
        assert redact({"motivo": None}) == {"motivo": None}

    def test_el_motivo_de_consulta_esta_cubierto(self) -> None:
        # It is clinical data under Res. 2654/2019.
        assert "motivo" in REDACTED_FIELDS

    def test_el_token_de_confirmacion_esta_cubierto(self) -> None:
        # A logged token is a replayable approval.
        assert "token_confirmacion" in REDACTED_FIELDS


class TestAuditor:
    def test_registra_una_invocacion_exitosa(self) -> None:
        logger = LoggerFalso()
        auditor = Auditor(logger)
        auditor.tool_call(
            "search_patients",
            subject="ana@clinica.test",
            scope="read",
            arguments={"documento": "123"},
            result="ok",
        )
        evento, payload = logger.llamadas[-1]
        assert evento == "tool.invocation"
        assert payload["subject"] == "ana@clinica.test"
        assert payload["arguments"]["documento"] == REDACTED

    def test_registra_el_codigo_de_error_cuando_lo_hay(self) -> None:
        auditor = Auditor(LoggerFalso())
        auditor.tool_call(
            "x",
            subject="a",
            scope="read",
            arguments={},
            result="error",
            error_code="CITA_NO_ENCONTRADA",
        )
        assert auditor.events[-1]["error_code"] == "CITA_NO_ENCONTRADA"

    def test_marca_la_ejecucion_aprobada(self) -> None:
        auditor = Auditor(LoggerFalso())
        auditor.tool_call("x", subject="a", scope="write", arguments={}, result="ok", approved=True)
        assert auditor.events[-1]["with_human_approval"] is True

    def test_propuesta_y_confirmacion_son_eventos_distintos(self) -> None:
        auditor = Auditor(LoggerFalso())
        auditor.question_asked("cancel_appointment", subject="a", nonce="n1")
        auditor.question_answered("cancel_appointment", subject="a", nonce="n1")
        assert [e["event"] for e in auditor.events] == [
            "approval.proposed",
            "approval.confirmed",
        ]

    def test_el_acceso_clinico_es_su_propio_tipo_de_evento(self) -> None:
        auditor = Auditor(LoggerFalso())
        auditor.clinical_access(subject="a", cita_id=7, result="registrado")
        assert auditor.events[-1] == {
            "event": "clinical.access",
            "subject": "a",
            "cita_id": 7,
            "result": "registrado",
        }

    def test_la_memoria_esta_acotada(self) -> None:
        """An audit log is a log, not a data store: the in-memory mirror must
        not grow without bound in a long-running process."""
        auditor = Auditor(LoggerFalso())
        auditor.memory_limit = 10
        for i in range(50):
            auditor.clinical_access(subject="a", cita_id=i, result="x")
        assert len(auditor.events) == 10
        assert auditor.events[-1]["cita_id"] == 49

    def test_los_eventos_son_serializables_a_json(self) -> None:
        """They are shipped to a log pipeline; an unserialisable value would be
        discovered in production rather than here."""
        auditor = Auditor(LoggerFalso())
        auditor.tool_call("x", subject="a", scope="read", arguments={"cita_id": 1}, result="ok")
        json.dumps(auditor.events)


class TestConfiguracionDeLogging:
    def test_configurar_no_revienta_con_un_nivel_valido(self) -> None:
        configure_logging("DEBUG")
        configure_logging("INFO")

    def test_un_nivel_desconocido_cae_a_info(self) -> None:
        configure_logging("NO-EXISTE")
