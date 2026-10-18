"""Indicação de lugar vem do Google, nunca de memória (bug real de 18/10/2026).

Pedido: "sugestões de pizzaria delivery para meu jantar em casa". O modelo
respondeu DE MEMÓRIA, sem ferramenta: "Pizzaria Don Nero" (é de Caruaru/PE)
e "Pizzaria Maiale" (não existe em Brasília) — só descobertos quando o dono
pediu o site de cada uma.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import respx
from pydantic import SecretStr

from bot.services import places, tools
from bot.services.llm.base import TOOLS_SOMENTE_LEITURA, ToolContext


def _p(nome, nota=None, n=0, status="OPERATIONAL", **extra):
    d = {"displayName": {"text": nome}, "businessStatus": status,
         "formattedAddress": f"{nome}, Brasília - DF"}
    if nota is not None:
        d.update(rating=nota, userRatingCount=n)
    d.update(extra)
    return d


def _com_chave(monkeypatch):
    monkeypatch.setattr(places.settings, "google_maps_api_key", SecretStr("k"))


def _sugerir(monkeypatch, lugares, **kw):
    _com_chave(monkeypatch)
    with respx.mock:
        rota = respx.post(places.PLACES_SEARCH_ENDPOINT).mock(
            return_value=httpx.Response(200, json={"places": lugares}))
        out = asyncio.run(places.sugerir_lugares("pizzaria Brasília", **kw))
        req = rota.calls.last.request
    return out, json.loads(req.content), req.headers["X-Goog-FieldMask"]


def test_nota_alta_com_poucas_avaliacoes_nao_passa_a_frente(monkeypatch) -> None:
    out, _, _ = _sugerir(monkeypatch, [
        _p("Novinha", 4.9, 8), _p("Consagrada", 4.7, 3000), _p("Media", 4.2, 900)])
    assert [p["displayName"]["text"] for p in out] == ["Consagrada", "Novinha", "Media"]


def test_fechado_fica_de_fora_e_limite_de_cinco(monkeypatch) -> None:
    lugares = [_p(f"L{i}", 4.5, 100) for i in range(8)]
    lugares.insert(0, _p("Fechada", 5.0, 9999, status="CLOSED_PERMANENTLY"))
    lugares.insert(0, _p("Reforma", 5.0, 9999, status="CLOSED_TEMPORARILY"))
    out, _, _ = _sugerir(monkeypatch, lugares)
    nomes = [p["displayName"]["text"] for p in out]
    assert len(nomes) == places.SUGESTAO_MAX
    assert "Fechada" not in nomes and "Reforma" not in nomes


def test_vies_de_localizacao_e_sem_campo_caro_por_padrao(monkeypatch) -> None:
    _, body, mascara = _sugerir(monkeypatch, [], coords="-15.79,-47.88")
    assert body["locationBias"]["circle"]["center"] == {"latitude": -15.79,
                                                         "longitude": -47.88}
    assert "delivery" not in mascara, "delivery muda o SKU pro mais caro"


def test_entrega_so_quando_pedida(monkeypatch) -> None:
    out, _, mascara = _sugerir(monkeypatch, [_p("A", 4.5, 100, delivery=True)],
                               entrega=True)
    assert "places.delivery" in mascara
    assert "🛵 faz entrega" in places.formatar_sugestoes_html("q", out)


def test_html_escapa_e_traz_os_dados() -> None:
    html = places.formatar_sugestoes_html("pizza & cia", [_p(
        "Bar <do> Zé", 4.6, 1200, nationalPhoneNumber="(61) 3333-4444",
        websiteUri="https://ze.com.br", googleMapsUri="https://maps.google.com/?cid=1",
        currentOpeningHours={"openNow": True})])
    assert "Bar &lt;do&gt; Zé" in html and "pizza &amp; cia" in html
    assert "<code>(61) 3333-4444</code>" in html
    assert "🟢 aberto agora" in html and "⭐ 4.6 (1200 avaliações)" in html


def test_lista_vazia_diz_que_nao_achou() -> None:
    assert "Não achei no Google" in places.formatar_sugestoes_html("x", [])


# ───────────── tool: verbatim, e falha nunca vira lista de memória ─────────────

def _ctx():
    return ToolContext(user=SimpleNamespace(id=1), session=None,
                       tz="America/Sao_Paulo", user_text="")


def test_tool_manda_verbatim(monkeypatch) -> None:
    async def _ok(q, **kw):
        return [_p("Real", 4.5, 100)]
    monkeypatch.setattr(places, "sugerir_lugares", _ok)
    ctx = _ctx()
    out = asyncio.run(tools._h_indicar_lugares({"query": "pizzaria Brasília"}, ctx))
    assert out.startswith("ok") and ctx.short_circuit is True
    assert "Real" in ctx.direct_html


def test_tool_com_falha_nao_monta_lista(monkeypatch) -> None:
    async def _boom(q, **kw):
        raise places.PlacesError("Places request failed: ReadTimeout")
    monkeypatch.setattr(places, "sugerir_lugares", _boom)
    ctx = _ctx()
    out = asyncio.run(tools._h_indicar_lugares({"query": "pizzaria"}, ctx))
    assert out.startswith("erro") and "NÃO sugira nomes de memória" in out
    assert not ctx.direct_html and not ctx.short_circuit


def test_tool_registrada_e_no_prompt() -> None:
    from bot.handlers import chat
    assert any(t.name == "indicar_lugares" for t in tools.TOOLS)
    assert "indicar_lugares" in TOOLS_SOMENTE_LEITURA
    assert "indicar_lugares" in chat._SYSTEM_PROMPT_TEMPLATE and "PROIBIDO citar" in chat._SYSTEM_PROMPT_TEMPLATE
