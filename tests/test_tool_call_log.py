"""Log de tool calls (04/08/2026): áudio pedindo previsão do tempo voltou com
a fatura INTEIRA do cartão na frente — alguma tool financeira rodou num turno
de clima e o log do Orange Pi não tinha COMO dizer qual/por quê (nenhum
provider logava as tool calls). resumo_tool_call é a linha que os três
providers agora emitem por chamada; sem ela o bug de roteamento é
indiagnosticável contra a fonte real.
"""
from __future__ import annotations

from bot.services.llm.base import resumo_tool_call


def test_resumo_e_compacto_e_legivel() -> None:
    out = resumo_tool_call("consultar_clima", {"dias": 7})
    assert out == 'consultar_clima({"dias": 7})'


def test_resumo_trunca_args_gigantes() -> None:
    out = resumo_tool_call("buscar_web", {"query": "x" * 1000})
    assert len(out) < 400
    assert "…" in out


def test_resumo_nao_estoura_com_args_nao_serializaveis() -> None:
    # Telemetria não pode piorar a falha que observa: objeto arbitrário nos
    # args (protos do Gemini, p.ex.) cai no default=str, nunca em exceção.
    out = resumo_tool_call("t", {"obj": object()})
    assert out.startswith("t(")


def test_resumo_aceita_args_none() -> None:
    assert resumo_tool_call("ajuda", None) == "ajuda({})"


def test_os_tres_providers_logam_tool_call() -> None:
    """A linha de log existe nos três loops — se um refactor derrubar uma,
    o provider volta a ser caixa-preta."""
    import pathlib
    base = pathlib.Path(__file__).resolve().parent.parent / "bot" / "services" / "llm"
    for impl in ("anthropic_impl.py", "gemini_impl.py", "openai_impl.py"):
        src = (base / impl).read_text(encoding="utf-8")
        assert "resumo_tool_call(" in src, f"{impl} não loga as tool calls"
        assert "tool call: %s" in src, f"{impl} não loga as tool calls"
