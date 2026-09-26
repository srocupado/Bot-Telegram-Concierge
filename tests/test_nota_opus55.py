"""Nota técnica no Claude Opus 5.5 + esforço escolhido pelo dono.

Pedido do dono (26/09/2026): usar o Opus 5.5 com esforço "high" na nota. Dois
obstáculos, ambos documentados na referência da API:
- o Opus 5.5 recusa ferramenta FORÇADA (tool_choice "tool") com 400 — a nota
  era gerada exatamente assim; ia sair "sem análise da IA";
- esforço padrão do Opus 5.5 é "medium"; o bot não enviava o parâmetro.

Os testes rodam o SDK REAL (anthropic 0.69, o fixado no projeto) contra um
servidor HTTP local que imita a API: confere o JSON que sai de verdade. Foi
assim que apareceu que o 0.69 não aceita `output_config` como argumento (daria
TypeError e a nota cairia no texto base em silêncio) — por isso vai em
extra_body.
"""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from bot.services import dou_monitor

_MP = {"numero": "1393", "ano": 2026, "data_publicacao": "2026-09-25",
       "ementa": "Desenrola Brasil 3.0", "texto": "Art. 1º ..."}
_NOTA = {"ementa": "Desenrola", "p1_contexto": "c", "p2_dispositivos": "d",
         "p3_continuacao": "", "p4_sintese": "", "p5_fechamento": ""}


def _eh_pesquisa(corpo: dict) -> bool:
    return any(t.get("name") == "web_search" for t in corpo.get("tools", []))


class _API:
    """Servidor local no lugar de api.anthropic.com. `recusa_forcado`
    imita o Opus 5.5 (400 em tool_choice forçado)."""

    def __init__(self, recusa_forcado: bool, erro_400: str | None = None,
                 dossie: str = "sem cobertura web ainda", stop_dossie: str = "end_turn"):
        self.corpos: list[dict] = []
        self.dossie, self.stop_dossie = dossie, stop_dossie
        api = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                corpo = json.loads(self.rfile.read(int(self.headers["content-length"])))
                api.corpos.append(corpo)
                if _eh_pesquisa(corpo):
                    return self._ok([{"type": "text", "text": api.dossie}],
                                    stop=api.stop_dossie)
                forcado = corpo.get("tool_choice", {}).get("type") == "tool"
                if erro_400:
                    return self._erro(erro_400)
                if forcado and recusa_forcado:
                    return self._erro('tool_choice: type "tool" and "any" are '
                                      "not supported for this model.")
                if forcado:
                    return self._ok([{"type": "tool_use", "id": "tu_1",
                                      "name": "nota_tecnica", "input": _NOTA}],
                                    stop="tool_use")
                return self._ok([{"type": "thinking", "thinking": "", "signature": "s"},
                                 {"type": "text", "text": json.dumps(_NOTA)}])

            def _ok(self, content, stop="end_turn"):
                self._json(200, {"id": "msg_1", "type": "message", "role": "assistant",
                                 "model": "m", "content": content, "stop_reason": stop,
                                 "stop_sequence": None,
                                 "usage": {"input_tokens": 1, "output_tokens": 1}})

            def _erro(self, msg):
                self._json(400, {"type": "error", "error": {
                    "type": "invalid_request_error", "message": msg}})

            def _json(self, status, obj):
                data = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def notas(self):
        return [c for c in self.corpos if not _eh_pesquisa(c)]

    def pesquisas(self):
        return [c for c in self.corpos if _eh_pesquisa(c)]


def _gerar(monkeypatch, api: _API, model: str, effort: str | None):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", api.url)
    monkeypatch.setattr(dou_monitor.settings, "anthropic_api_key", "sk-teste")
    try:
        return asyncio.run(dou_monitor._gen_nota_anthropic(
            _MP, model_override=model, effort=effort))
    finally:
        api.srv.shutdown()


def test_opus55_recusa_ferramenta_forcada_e_a_nota_sai_por_json(monkeypatch) -> None:
    api = _API(recusa_forcado=True)
    nota = _gerar(monkeypatch, api, "claude-opus-5-5", "high")

    assert nota == _NOTA, "a nota tem que sair COM análise, não texto base"
    primeira, segunda = api.notas()
    assert primeira["tool_choice"] == {"type": "tool", "name": "nota_tecnica"}
    assert primeira["output_config"] == {"effort": "high"}
    assert "tool_choice" not in segunda and "tools" not in segunda
    oc = segunda["output_config"]
    assert oc["effort"] == "high", "o esforço escolhido se perdeu no retry"
    schema = oc["format"]["schema"]
    assert oc["format"]["type"] == "json_schema"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(_NOTA)


def test_modelo_que_aceita_ferramenta_segue_igual_ao_de_antes(monkeypatch) -> None:
    """Sonnet 5 (o que gerou as notas até hoje): sem esforço escolhido, o
    corpo é o mesmo de antes — nenhum output_config, ferramenta forçada."""
    api = _API(recusa_forcado=False)
    nota = _gerar(monkeypatch, api, "claude-sonnet-5", None)

    assert nota == _NOTA
    (unica,) = api.notas()
    assert unica["tool_choice"] == {"type": "tool", "name": "nota_tecnica"}
    assert "output_config" not in unica


def test_esforco_vai_no_corpo_tambem_sem_fallback(monkeypatch) -> None:
    api = _API(recusa_forcado=False)
    _gerar(monkeypatch, api, "claude-sonnet-5", "xhigh")
    (unica,) = api.notas()
    assert unica["output_config"] == {"effort": "xhigh"}


def test_outro_400_nao_tenta_json_e_cai_no_texto_base(monkeypatch) -> None:
    """Só o 400 de tool_choice troca de caminho; qualquer outro erro segue a
    regra de antes (None → DOCX com aviso "sem análise da IA")."""
    api = _API(recusa_forcado=False, erro_400="effort: invalid value for this model")
    assert _gerar(monkeypatch, api, "claude-haiku-4-5", "max") is None
    assert len(api.notas()) == 1


# ───────────── /dou_provider esforco ─────────────

class _Msg:
    def __init__(self):
        self.respostas: list[str] = []

    async def answer(self, texto, **kw):
        self.respostas.append(texto)


class _Sessao:
    async def commit(self):
        pass


def _cmd(args: str, user):
    import types
    from bot.handlers import dou_mp
    msg = _Msg()
    asyncio.run(dou_mp.cmd_dou_provider(
        msg, types.SimpleNamespace(args=args), user, _Sessao()))
    return msg.respostas[-1]


def _user():
    import types
    return types.SimpleNamespace(is_authorized=True, dou_mp_provider="anthropic",
                                 dou_mp_model="claude-opus-5-5", dou_mp_effort=None)


def test_comando_esforco_grava_e_mostra() -> None:
    u = _user()
    assert "high" in _cmd("esforco high", u)
    assert u.dou_mp_effort == "high"
    assert "<b>high</b>" in _cmd("", u)
    _cmd("esforço padrao", u)
    assert u.dou_mp_effort is None
    assert "medium" in _cmd("", u), "status tem que dizer o padrão do Opus 5.5"


def test_comando_esforco_invalido_nao_grava() -> None:
    u = _user()
    resp = _cmd("esforco altissimo", u)
    assert u.dou_mp_effort is None
    assert "low | medium | high | xhigh | max" in resp


def test_comando_esforco_avisa_quando_o_motor_e_gemini() -> None:
    u = _user()
    u.dou_mp_provider = "gemini"
    assert "Gemini" in _cmd("esforco high", u)


# ───────────── pesquisa web (dossiê) ─────────────

def test_pesquisa_web_tem_espaco_pra_escrever(monkeypatch) -> None:
    """Medido na API real: com 1024 tokens o Sonnet 5 dizia "sem cobertura"
    e o Opus 5.5 devolvia dossiê vazio; com 4096 os dois trouxeram contexto."""
    api = _API(recusa_forcado=False)
    _gerar(monkeypatch, api, "claude-sonnet-5", None)
    (pesquisa,) = api.pesquisas()
    assert pesquisa["max_tokens"] >= 4096


def test_pesquisa_cortada_no_teto_avisa_no_log(monkeypatch, caplog) -> None:
    import logging
    api = _API(recusa_forcado=False, dossie="", stop_dossie="max_tokens")
    with caplog.at_level(logging.WARNING, logger="bot.services.dou_monitor"):
        _gerar(monkeypatch, api, "claude-opus-5-5", None)
    assert any("cortada no teto" in r.getMessage() for r in caplog.records)
