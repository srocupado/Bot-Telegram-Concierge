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
        "pubDate": "31/07/2026",
        "urlTitle": url_title,
    }


_MATERIA = (
    '<html>'
    '<p class="identifica"><strong>MEDIDA PROVISÓRIA Nº 1.381, DE 30 DE JULHO DE 2026</strong></p>'
    '<p class="ementa">Abre crédito <b>extraordinário</b>.</p>'
    '<p class="dou-paragraph" align="x">O PRESIDENTE DA REPÚBLICA, no uso da atribuição que lhe confere o art. 62 da Constituição, adota a seguinte Medida Provisória, com força de lei:</p>'
    '<p class="dou-paragraph">Art. 1º Fica aberto crédito extraordinário no valor de R$ 3 bi.</p>'
    '<p class="dou-paragraph">Art. 2º Esta Medida Provisória entra em vigor na data de sua publicação.</p>'
    '<p class="assinaPr">LUIZ INÁCIO LULA DA SILVA</p>'
    '<p class="assina">Fernando Haddad</p>'
    '</html>'
)

# Corpo raso (1 parágrafo): reprovado na régua de sanidade → texto None.
_MATERIA_RASA = ('<html><p class="identifica">MP X</p>'
                 '<p class="ementa">Ementa.</p>'
                 '<p class="dou-paragraph">Só um parágrafo.</p></html>')


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
    assert (mp.numero, mp.ano) == ("1.381", 2026), (
        "ano INT como nos dicts do Inlabs — str quebrava o dedup de DouSeenMP"
    )
    assert mp.ementa == "Abre crédito extraordinário."
    assert "MEDIDA PROVISÓRIA Nº 1.381" in mp.titulo, "título sai LIMPO de tags"
    assert mp.data_publicacao == "2026-07-31"
    # Texto INTEGRAL montado da matéria: identifica + parágrafos + assinatura.
    assert mp.texto is not None
    assert "MEDIDA PROVISÓRIA Nº 1.381" in mp.texto
    assert "Art. 1º" in mp.texto and "Art. 2º" in mp.texto
    assert "LULA" in mp.texto
    assert dia.edicao_confirmada is True


def test_corpo_raso_reprova_na_sanidade_mas_nao_bloqueia_deteccao() -> None:
    async def _main():
        with respx.mock:
            _rotas({"MEDIDA": [_item_mp()]}, materia_html=_MATERIA_RASA)
            return await dou_portal.checar_dia_portal(D)

    dia = asyncio.run(_main())
    assert len(dia.mps) == 1, "detecção sai mesmo sem texto aprovado"
    assert dia.mps[0].texto is None, "texto suspeito NÃO alimenta a nota"
    assert dia.mps[0].ementa == "Ementa."


def test_mp_dict_para_nota_tem_o_formato_do_inlabs() -> None:
    mp = dou_portal.PortalMP("1.381", 2026, "MEDIDA PROVISÓRIA Nº 1.381",
                             "Abre crédito.", "https://x",
                             texto="TEXTO INTEGRAL...", data_publicacao="2026-07-31")
    dc = dou_portal.mp_dict_para_nota(mp, D)
    assert dc["numero"] == "1.381" and dc["ano"] == 2026
    assert dc["texto_integral"] == "TEXTO INTEGRAL..."
    assert dc["data_publicacao"] == "2026-07-31"
    assert "mpv1.381.htm" in dc["url_planalto"]

    sem_texto = dou_portal.PortalMP("1.381", 2026, "t", "e", "u")
    assert dou_portal.mp_dict_para_nota(sem_texto, D) is None, (
        "sem texto aprovado não há dict — a nota espera o Inlabs"
    )


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
    mp = dou_portal.PortalMP("1.382", 2026,
                             "MEDIDA PROVISÓRIA Nº 1.382, DE 5 DE AGOSTO DE 2026",
                             "Dispõe sobre teste.", "https://x",
                             texto="TEXTO INTEGRAL aprovado na sanidade")
    hoje, facts, marks = _rodar_collect(monkeypatch, dou_portal.PortalDia([mp], True))

    mps = [f for f in facts if f.kind == "mp"]
    assert len(mps) == 1
    assert mps[0].key == "1.382/2026"
    assert "PORTAL" in mps[0].text and "Dispõe sobre teste" in mps[0].text
    assert "texto do portal" in mps[0].text, (
        "com texto aprovado o aviso promete a nota EM SEGUIDA, não fila"
    )
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


# ──────────────── nota gerada com o texto do portal (fila) ────────────────

def _harness_nota_portal(monkeypatch, portal_result, numeros=("1.382",)):
    """Roda _tentar_nota_via_portal com tudo capturado."""
    from bot.services import dou_monitor as dm
    eventos = {"notas": [], "unmarks": [], "sends": [], "seen": []}

    async def _portal(_d):
        if isinstance(portal_result, Exception):
            raise portal_result
        return portal_result

    async def _nota(bot, user, mp, caption_extra=None):
        eventos["notas"].append((mp["numero"], caption_extra))

    async def _unmark(_s, _uid, kind, key):
        eventos["unmarks"].append((kind, key))

    async def _send(_bot, _uid, texto, **kw):
        eventos["sends"].append(texto)
        return True

    async def _seen(_s, _uid, mp):
        eventos["seen"].append((mp["numero"], mp["ano"]))

    monkeypatch.setattr(dou_portal, "checar_dia_portal", _portal)
    monkeypatch.setattr(dm, "gerar_e_enviar_nota", _nota)
    monkeypatch.setattr(dm, "mark_seen", _seen)
    monkeypatch.setattr(proactive, "unmark_notified", _unmark)
    monkeypatch.setattr(proactive, "_send", _send)
    monkeypatch.setattr(proactive.settings, "dou_portal_fallback", True)

    user = SimpleNamespace(id=99, dou_mp_provider=None, dou_mp_model=None)
    ok = asyncio.run(proactive._tentar_nota_via_portal(
        bot=None, session=_FakeSession(), user=user, d=D,
        numeros=list(numeros) if numeros is not None else None,
        key=f"{D.isoformat()}:{','.join(numeros)}" if numeros else f"{D.isoformat()}:all",
    ))
    return ok, eventos


def _mp_completa(numero="1.382"):
    return dou_portal.PortalMP(
        numero, 2026, f"MEDIDA PROVISÓRIA Nº {numero}", "Ementa.", "https://x",
        texto="TEXTO INTEGRAL aprovado", data_publicacao="2026-07-31",
    )


def test_nota_da_fila_sai_com_texto_do_portal(monkeypatch) -> None:
    ok, ev = _harness_nota_portal(
        monkeypatch, dou_portal.PortalDia([_mp_completa()], True))
    assert ok is True
    assert len(ev["notas"]) == 1
    numero, caption = ev["notas"][0]
    assert numero == "1.382"
    assert "portal oficial" in caption, "origem do texto é DITA na entrega"
    assert ev["seen"] == [("1.382", 2026)], "entregue = visto (conferência da Câmara)"
    assert ev["unmarks"] == [("nota_pendente", f"{D.isoformat()}:1.382")]
    assert any("PORTAL" in s for s in ev["sends"])


def test_entrada_all_nunca_e_resolvida_pelo_portal(monkeypatch) -> None:
    """'all' é CHECAGEM do dia — resolvê-la sem o Inlabs daria baixa sem a
    confirmação final (extra em PDF puro só existe lá)."""
    chamado = {"portal": False}

    async def _portal(_d):
        chamado["portal"] = True
        return dou_portal.PortalDia([_mp_completa()], True)

    monkeypatch.setattr(dou_portal, "checar_dia_portal", _portal)
    monkeypatch.setattr(proactive.settings, "dou_portal_fallback", True)
    ok = asyncio.run(proactive._tentar_nota_via_portal(
        bot=None, session=_FakeSession(),
        user=SimpleNamespace(id=99), d=D, numeros=None, key=f"{D.isoformat()}:all"))
    assert ok is False and chamado["portal"] is False


def test_texto_reprovado_mantem_a_fila(monkeypatch) -> None:
    sem_texto = dou_portal.PortalMP("1.382", 2026, "t", "e", "u")
    ok, ev = _harness_nota_portal(
        monkeypatch, dou_portal.PortalDia([sem_texto], True))
    assert ok is False
    assert ev["notas"] == [] and ev["unmarks"] == [], "fila intacta na dúvida"


def test_mp_da_fila_ausente_do_portal_mantem_a_fila(monkeypatch) -> None:
    ok, ev = _harness_nota_portal(
        monkeypatch, dou_portal.PortalDia([_mp_completa("1.399")], True))
    assert ok is False and ev["unmarks"] == []


def test_portal_indisponivel_mantem_a_fila(monkeypatch) -> None:
    ok, ev = _harness_nota_portal(
        monkeypatch, dou_portal.PortalError("WAF"))
    assert ok is False and ev["unmarks"] == []


def test_help_documenta_a_fonte_reserva() -> None:
    from bot.handlers.start import HELP_TEXT, find_help_sections
    assert "Fonte reserva" in HELP_TEXT
    secoes = find_help_sections("e se o inlabs estiver fora do ar?")
    assert any("Fonte reserva" in s for s in secoes)
