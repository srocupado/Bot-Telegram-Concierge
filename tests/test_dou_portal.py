"""Fallback do portal público (in.gov.br) pro monitor de MP.

Motivação (06/08/2026): "Inlabs tá virando um vaga-lume" (dono). Homologado
contra o site real (sondas do container e do Orange Pi, MP 1.381 achada de
ponta a ponta). Papel: DETECÇÃO quando o Inlabs falha — nunca dá baixa; MP
achada é avisada já (nota na fila) e "sem MP" vem com evidência (edição
confirmada no índice). Scraping sem contrato: qualquer forma inesperada
estoura PortalError ALTO, nunca lista vazia silenciosa.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
import respx

from bot.services import dou_monitor, dou_portal, proactive

D = date(2026, 7, 31)


def _pagina(itens: list[dict]) -> str:
    return (
        '<html><script id="_x_params" type="application/json">'
        + json.dumps({"jsonArray": itens}, ensure_ascii=False)
        + "</script></html>"
    )


def _item_mp(numero="1.381", ano="2026", url_title="mp-x-1"):
    return {
        "title": (f"<span class='highlight'>MEDIDA</span> PROVISÓRIA "
                  f"Nº {numero}, DE 30 DE JULHO DE {ano}"),
        "artType": "Medida Provisória",
        "pubName": "DO1",
        "urlTitle": url_title,
    }


_MATERIA = ('<html><p class="identifica">MP</p>'
            '<p class="ementa">Abre crédito <b>extraordinário</b>.</p></html>')


def _rotas(busca_por_q: dict, materia_html: str = _MATERIA):
    """Roteia a busca por conteúdo do parâmetro q; matéria sempre igual."""
    def _resp(request):
        import httpx
        q = request.url.params.get("q", "")
        for chave, itens in busca_por_q.items():
            if chave in q:
                return httpx.Response(200, text=_pagina(itens))
        return httpx.Response(200, text=_pagina([]))

    respx.route(host="www.in.gov.br", path="/consulta/-/buscar/dou").mock(
        side_effect=_resp)
    respx.route(host="www.in.gov.br", path__startswith="/web/dou/-/").respond(
        200, text=materia_html)


def test_acha_mp_com_ementa_e_filtra_por_arttype() -> None:
    async def _main():
        with respx.mock:
            _rotas({"MEDIDA": [
                _item_mp(),
                {"title": "DESPACHOS DO PRESIDENTE", "artType": "Mensagem",
                 "urlTitle": "despacho-1"},   # cita MP no texto: NÃO é MP
            ]})
            return await dou_portal.checar_dia_portal(D)

    dia = asyncio.run(_main())
    assert len(dia.mps) == 1
    mp = dia.mps[0]
    assert (mp.numero, mp.ano) == ("1.381", "2026")
    assert mp.ementa == "Abre crédito extraordinário."
    assert "MEDIDA PROVISÓRIA Nº 1.381" in mp.titulo, "título sai LIMPO de tags"
    assert dia.edicao_confirmada is True


def test_sem_mp_confirma_edicao_pela_sonda() -> None:
    async def _main():
        with respx.mock:
            _rotas({"portaria": [{"title": "PORTARIA Nº 9", "artType": "Portaria",
                                  "urlTitle": "p9"}]})
            return await dou_portal.checar_dia_portal(D)

    dia = asyncio.run(_main())
    assert dia.mps == []
    assert dia.edicao_confirmada is True, "portaria no dia prova edição no índice"


def test_indice_vazio_e_inconclusivo_nao_sem_edicao() -> None:
    async def _main():
        with respx.mock:
            _rotas({})
            return await dou_portal.checar_dia_portal(D)

    dia = asyncio.run(_main())
    assert dia.mps == [] and dia.edicao_confirmada is False


def test_titulo_de_mp_nao_parseavel_estoura_alto() -> None:
    item = _item_mp()
    item["title"] = "MEDIDA PROVISÓRIA (título fora do padrão)"

    async def _main():
        with respx.mock:
            _rotas({"MEDIDA": [item]})
            await dou_portal.checar_dia_portal(D)

    with pytest.raises(dou_portal.PortalError):
        asyncio.run(_main())


def test_pagina_sem_bloco_de_resultados_estoura_alto() -> None:
    async def _main():
        with respx.mock:
            respx.route(host="www.in.gov.br").respond(200, text="<html>WAF</html>")
            await dou_portal.checar_dia_portal(D)

    with pytest.raises(dou_portal.PortalError):
        asyncio.run(_main())


def test_pagina_cheia_estoura_em_vez_de_afirmar() -> None:
    """Página no limite de paginação = pode haver MP cortada — na dúvida, erro."""
    async def _main():
        with respx.mock:
            _rotas({"MEDIDA": [_item_mp(numero=f"1.{i}", url_title=f"u{i}")
                               for i in range(dou_portal._DELTA)]})
            await dou_portal.checar_dia_portal(D)

    with pytest.raises(dou_portal.PortalError):
        asyncio.run(_main())


def test_disfarce_de_navegador_vai_no_request() -> None:
    """Medido no site real: UA de curl → conexão cortada pelo WAF."""
    capturado = {}

    async def _main():
        with respx.mock:
            def _resp(request):
                import httpx
                capturado["ua"] = request.headers.get("user-agent", "")
                return httpx.Response(200, text=_pagina([]))
            respx.route(host="www.in.gov.br").mock(side_effect=_resp)
            return await dou_portal.checar_dia_portal(D)

    asyncio.run(_main())
    assert "Mozilla" in capturado["ua"]


# ───────────────────── integração com o collect_mp ─────────────────────

class _FakeSession:
    async def scalars(self, _stmt):
        return []

    async def commit(self):
        return None


def _rodar_collect(monkeypatch, portal_result):
    """fetch do Inlabs FALHANDO; portal respondendo `portal_result` (ou
    levantando, se for exceção). Devolve (facts, marks)."""
    hoje = datetime.now(proactive.BRT).date()
    marks: list[tuple[str, str]] = []

    async def _fetch(_d):
        raise dou_monitor.DouError("Inlabs em manutenção")

    async def _portal(_d):
        if isinstance(portal_result, Exception):
            raise portal_result
        return portal_result

    async def _false(*a, **kw):
        return False

    async def _mark(_s, _uid, kind, key):
        marks.append((kind, key))

    async def _none(*a, **kw):
        return None

    monkeypatch.setattr(dou_monitor, "fetch_mps", _fetch)
    monkeypatch.setattr(dou_portal, "checar_dia_portal", _portal)
    monkeypatch.setattr(proactive, "already_notified", _false)
    monkeypatch.setattr(proactive, "mark_notified", _mark)
    monkeypatch.setattr(proactive, "unmark_notified", _none)
    monkeypatch.setattr(proactive.settings, "dou_portal_fallback", True)

    user = SimpleNamespace(id=99, dou_mp_subscribed=True,
                           dou_ultimo_dia_ok=hoje - timedelta(days=1))
    facts = asyncio.run(proactive.collect_mp(_FakeSession(), user, [hoje]))
    return hoje, facts, marks


def test_inlabs_fora_mp_do_portal_e_avisada_com_nota_na_fila(monkeypatch) -> None:
    mp = dou_portal.PortalMP("1.382", "2026",
                             "MEDIDA PROVISÓRIA Nº 1.382, DE 5 DE AGOSTO DE 2026",
                             "Dispõe sobre teste.", "https://x")
    hoje, facts, marks = _rodar_collect(monkeypatch, dou_portal.PortalDia([mp], True))

    mps = [f for f in facts if f.kind == "mp"]
    assert len(mps) == 1
    assert mps[0].key == "1.382/2026"
    assert "PORTAL" in mps[0].text and "Dispõe sobre teste" in mps[0].text
    # Aviso informativo no lugar do alarme "não consegui checar".
    fails = [f for f in facts if f.kind == "mp_fail"]
    assert len(fails) == 1 and fails[0].key.startswith("portal:")
    assert "NÃO assuma" not in fails[0].text
    # Outbox: nota na fila; e o dia SEGUE pendente (portal nunca dá baixa).
    assert ("nota_pendente", f"{hoje.isoformat()}:1.382") in marks
    assert ("mp_pendente", hoje.isoformat()) in marks


def test_inlabs_fora_portal_sem_mp_vira_afirmacao_com_evidencia(monkeypatch) -> None:
    hoje, facts, marks = _rodar_collect(monkeypatch, dou_portal.PortalDia([], True))

    assert [f for f in facts if f.kind == "mp"] == []
    fails = [f for f in facts if f.kind == "mp_fail"]
    assert len(fails) == 1 and fails[0].key.startswith("portal:")
    assert "SEM MP" in fails[0].text
    assert ("mp_pendente", hoje.isoformat()) in marks, "sem baixa pelo portal"


def test_portal_tambem_fora_mantem_alarme_forte(monkeypatch) -> None:
    _, facts, _ = _rodar_collect(
        monkeypatch, dou_portal.PortalError("WAF bloqueou"))

    fails = [f for f in facts if f.kind == "mp_fail"]
    assert len(fails) == 1 and fails[0].key.startswith("fail:")
    assert "NÃO assuma" in fails[0].text


def test_help_documenta_a_fonte_reserva() -> None:
    from bot.handlers.start import HELP_TEXT, find_help_sections
    assert "Fonte reserva" in HELP_TEXT
    secoes = find_help_sections("e se o inlabs estiver fora do ar?")
    assert any("Fonte reserva" in s for s in secoes)
