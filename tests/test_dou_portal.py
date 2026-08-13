"""Portal público (in.gov.br) no monitor de MP.

Motivação (06/08/2026): "Inlabs tá virando um vaga-lume" (dono). Homologado
contra o site real (sondas do container e do Orange Pi, MP 1.381 achada de
ponta a ponta). INVERTIDO em 11/08/2026 (caso MP 1.382, achada só pelo
portal na edição extra retroativa de 01/08): o portal virou VERIFICADOR —
evidência positiva em dia FECHADO dá baixa (MPs anunciadas, edição sem MP,
ou ausência conclusiva com dia-controle vivo); dia aberto segue só detecção.
Scraping sem contrato: forma inesperada estoura PortalError ALTO, nunca
lista vazia silenciosa.
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
    assert dc["edicao"] == "Normal"

    sem_texto = dou_portal.PortalMP("1.381", 2026, "t", "e", "u")
    assert dou_portal.mp_dict_para_nota(sem_texto, D) is None, (
        "sem texto aprovado não há dict — a nota espera o Inlabs"
    )


def test_edicao_extra_vem_do_pubname() -> None:
    """Caso MP 1.382 (dono, 11/08/2026): publicada na extra de sábado
    (pubName DO1_EXTRA_C), a nota saiu rotulada 'Edição Normal' — o portal
    não capturava a edição e o default silencioso mentia pro LLM. Agora o
    pubName decide, e o dict da nota carrega a edição."""
    item = _item_mp(numero="1.382")
    item["pubName"] = "DO1_EXTRA_C"

    async def _main():
        with respx.mock:
            _rotas({"MEDIDA": [item]})
            return await dou_portal.checar_dia_portal(D)

    dia = asyncio.run(_main())
    assert dia.mps[0].edicao == "Extra"
    dc = dou_portal.mp_dict_para_nota(
        dou_portal.PortalMP("1.382", 2026, "t", "e", "u",
                            texto="TEXTO...", edicao="Extra"), D)
    assert dc["edicao"] == "Extra"


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

    async def _portal(_d, **_kw):
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
    monkeypatch.setattr(dou_monitor, "_ultima_ok", {})   # sem bleed entre testes

    user = SimpleNamespace(id=99, dou_mp_subscribed=True,
                           dou_ultimo_dia_ok=hoje - timedelta(days=1))
    facts = asyncio.run(proactive.collect_mp(_FakeSession(), user, [hoje]))
    return hoje, facts, marks


def test_portal_primeiro_mp_anunciada_sem_tocar_o_inlabs(monkeypatch) -> None:
    """PORTAL PRIMEIRO (dono, 11/08/2026): o portal conclusivo responde a
    janela SOZINHO — nenhuma linha de falha (o Inlabs quebrado nem é
    percebido), MP anunciada como sempre (com botão de nota, geração sob
    demanda — sem fila automática)."""
    mp = dou_portal.PortalMP("1.382", 2026,
                             "MEDIDA PROVISÓRIA Nº 1.382, DE 5 DE AGOSTO DE 2026",
                             "Dispõe sobre teste.", "https://x",
                             texto="TEXTO INTEGRAL aprovado na sanidade")
    hoje, facts, marks = _rodar_collect(monkeypatch, dou_portal.PortalDia([mp], True))

    mps = [f for f in facts if f.kind == "mp"]
    assert len(mps) == 1
    assert mps[0].key == "1.382/2026"
    assert "Dispõe sobre teste" in mps[0].text
    assert mps[0].date_iso == hoje.isoformat()          # botão de nota ativo
    assert [f for f in facts if f.kind == "mp_fail"] == [], (
        "portal conclusivo → Inlabs fora nem aparece"
    )
    # Nota continua SOB DEMANDA (botão) — sem fila automática.
    assert not any(k == "nota_pendente" for k, _ in marks)
    # Dia ABERTO segue pendente (provisorio) até fechar.
    assert ("mp_pendente", hoje.isoformat()) in marks


def test_portal_primeiro_sem_mp_responde_sem_alarme(monkeypatch) -> None:
    hoje, facts, marks = _rodar_collect(monkeypatch, dou_portal.PortalDia([], True))

    assert [f for f in facts if f.kind == "mp"] == []
    assert [f for f in facts if f.kind == "mp_fail"] == [], (
        "edição confirmada sem MP é resposta, não falha"
    )
    assert ("mp_pendente", hoje.isoformat()) in marks, "dia aberto: sem baixa"
    # Furo 4: checagem do portal alimenta a memória do 'já checado HH:MM'.
    assert dou_monitor.ultima_checagem_ok(hoje) is not None


def test_indice_sem_a_data_vira_linha_informativa_das_duas_fontes(monkeypatch) -> None:
    """Incidente de 08/08/2026 (sábado 7h05): portal consultado, índice ainda
    sem a edição — e o briefing só mostrou o grito 'NÃO assuma', como se o
    fallback nem existisse. Inconclusivo agora É dito: duas fontes
    consultadas, leitura provável, nada afirmado, dia segue pendente."""
    hoje, facts, marks = _rodar_collect(monkeypatch, dou_portal.PortalDia([], False))

    fails = [f for f in facts if f.kind == "mp_fail"]
    assert len(fails) == 1 and fails[0].key.startswith("portal:")
    assert "índice ainda não tem a edição" in fails[0].text
    assert "fim de semana" in fails[0].text
    assert "NÃO assuma" not in fails[0].text
    assert ("mp_pendente", hoje.isoformat()) in marks, "segue pendente"


def _rodar_retro(monkeypatch, portal_do_retro, *, dias_atras=3):
    """Inlabs falhando; dia retro FECHADO (pin: hoje-3) na fila; portal
    responde `portal_do_retro` pro retro e inconclusivo pra hoje."""
    hoje = datetime.now(proactive.BRT).date()
    retro = hoje - timedelta(days=dias_atras)
    marks: list[tuple[str, str]] = []

    async def _fetch(_d):
        raise dou_monitor.DouError("Inlabs em manutenção")

    async def _portal(d, **_kw):
        return (portal_do_retro if d == retro
                else dou_portal.PortalDia([], False))

    async def _pendentes(_s, _uid, _hoje, _desist=None):
        return [retro]

    async def _false(*a, **kw):
        return False

    async def _mark(_s, _uid, kind, key):
        marks.append((kind, key))

    async def _none(*a, **kw):
        return None

    monkeypatch.setattr(dou_monitor, "fetch_mps", _fetch)
    monkeypatch.setattr(dou_portal, "checar_dia_portal", _portal)
    monkeypatch.setattr(proactive, "_mp_dias_pendentes", _pendentes)
    monkeypatch.setattr(proactive, "already_notified", _false)
    monkeypatch.setattr(proactive, "mark_notified", _mark)
    monkeypatch.setattr(proactive, "unmark_notified", _none)
    monkeypatch.setattr(proactive.settings, "dou_portal_fallback", True)

    user = SimpleNamespace(id=99, dou_mp_subscribed=True,
                           dou_ultimo_dia_ok=hoje - timedelta(days=1))
    facts = asyncio.run(proactive.collect_mp(_FakeSession(), user, [hoje]))
    return retro, facts, marks


def test_dia_retroativo_fechado_com_mp_e_baixado_pelo_portal(monkeypatch) -> None:
    """Dono, 09/08/2026: dia preso na fila esperava só o Inlabs. PORTAL
    PRIMEIRO: a retroativa consulta o portal antes de qualquer Inlabs — MP
    do dia fechado é anunciada e o dia recebe BAIXA (✅ retroativa
    concluída), sem alarme nenhum."""
    mp = dou_portal.PortalMP("1.383", 2026, "MEDIDA PROVISÓRIA Nº 1.383",
                             "Ementa retro.", "https://x", texto="TEXTO ok")
    retro, facts, marks = _rodar_retro(
        monkeypatch, dou_portal.PortalDia([mp], True))

    mps = [f for f in facts if f.kind == "mp"]
    assert len(mps) == 1 and mps[0].key == "1.383/2026"
    assert mps[0].date_iso == retro.isoformat()
    baixas = [f for f in facts if f.kind == "mp_retro"]
    assert len(baixas) == 1 and "1 MP(s) nova(s)" in baixas[0].text
    assert baixas[0].key == f"retro:{retro.isoformat()}"
    assert not [f for f in facts if f.kind == "mp_fail"
                and "fila retroativa" in f.text], "baixado não vira alarme"
    # Nota sob demanda (botão), como em qualquer anúncio de MP.
    assert not any(k == "nota_pendente" for k, _ in marks)


def test_dia_retroativo_sem_edicao_conclusivo_e_baixado(monkeypatch) -> None:
    """08-09/08/2026 ao vivo: fim de semana sem edição, preso na fila por dias
    com o falso 'recusou a sessão'. Portal com dia-controle vivo → ausência é
    evidência positiva → baixa retroativa sem Inlabs nenhum."""
    retro, facts, marks = _rodar_retro(
        monkeypatch, dou_portal.PortalDia([], False, sem_edicao=True))

    baixas = [f for f in facts if f.kind == "mp_retro"]
    assert len(baixas) == 1
    assert "nenhuma MP nova" in baixas[0].text
    assert baixas[0].key == f"retro:{retro.isoformat()}"
    assert not [f for f in facts if f.kind == "mp_fail"
                and "fila retroativa" in f.text]


def test_dia_retroativo_inconclusivo_segue_pendente(monkeypatch) -> None:
    """Portal sem índice pro retro (e controle vazio): NADA de baixa — linha
    informativa e o dia continua na fila. Na dúvida, é pendência."""
    retro, facts, _ = _rodar_retro(
        monkeypatch, dou_portal.PortalDia([], False))

    assert not [f for f in facts if f.kind == "mp_retro"
                and "fila retroativa" in f.text]
    infos = [f for f in facts if f.kind == "mp_fail"
             and "fila retroativa" in f.text]
    assert len(infos) == 1 and "índice ainda não tem a edição" in infos[0].text


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

    async def _portal(_d, **_kw):
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


def _rodar_nota_portal_cru(monkeypatch, *, portal_mps, vistos=(),
                           falha_em=(), entradas=()):
    """_tentar_nota_via_portal com sessão que devolve `vistos` (DouSeenMP,
    lista VIVA — mark_seen alimenta), `entradas` (nota_pendente existentes)
    e geração que falha pras MPs em `falha_em`."""
    eventos = {"notas": [], "unmarks": []}
    vivos = list(vistos)

    class _Sessao2:
        async def scalars(self, stmt):
            if "dou_seen" in str(stmt):
                return [SimpleNamespace(numero=n, ano=2026) for n in vivos]
            return [SimpleNamespace(key=k) for k in entradas]

        async def commit(self):
            return None

    async def _portal(_d, **_kw):
        return dou_portal.PortalDia(list(portal_mps), True)

    async def _nota(bot, user, mp, caption_extra=None):
        if mp["numero"] in falha_em:
            raise RuntimeError("LLM 500")
        eventos["notas"].append(mp["numero"])

    async def _unmark(_s, _uid, kind, key):
        eventos["unmarks"].append(key)

    async def _send(_b, _uid, texto, **kw):
        return True

    async def _seen(_s, _uid, dc):
        vivos.append(dc["numero"])

    from bot.services import dou_monitor as dm
    monkeypatch.setattr(dou_portal, "checar_dia_portal", _portal)
    monkeypatch.setattr(dm, "gerar_e_enviar_nota", _nota)
    monkeypatch.setattr(dm, "mark_seen", _seen)
    monkeypatch.setattr(proactive, "unmark_notified", _unmark)
    monkeypatch.setattr(proactive, "_send", _send)
    monkeypatch.setattr(proactive.settings, "dou_portal_fallback", True)
    numeros = sorted({mp.numero for mp in portal_mps})
    key = f"{D.isoformat()}:{','.join(numeros)}"
    ok = asyncio.run(proactive._tentar_nota_via_portal(
        None, _Sessao2(), SimpleNamespace(id=9), D, numeros, key))
    return ok, eventos, key


def test_retentativa_pula_notas_ja_entregues(monkeypatch) -> None:
    """Bug de 13/08/2026: 1.385 e 1.384 entregues, 1.383 falhou → o dia
    inteiro re-enfileirado e a re-tentativa REGERAVA as entregues. Agora
    gera SÓ a que falta e dá baixa."""
    ok, ev, key = _rodar_nota_portal_cru(
        monkeypatch,
        portal_mps=[_mp_completa("1.385"), _mp_completa("1.384"),
                    _mp_completa("1.383")],
        vistos=("1.385", "1.384"),
    )
    assert ok is True
    assert ev["notas"] == ["1.383"], "só a que faltava — sem duplicar"
    assert ev["unmarks"] == [key]


def test_falha_em_uma_nao_derruba_as_irmas(monkeypatch) -> None:
    """A falha na geração de UMA MP não pode abortar as demais (entregar
    2 de 3 é melhor que 0 de 3); a entrada fica na fila pra refazer só a
    que faltou."""
    ok, ev, _ = _rodar_nota_portal_cru(
        monkeypatch,
        portal_mps=[_mp_completa("1.385"), _mp_completa("1.384"),
                    _mp_completa("1.383")],
        falha_em=("1.384",),
    )
    assert ok is False
    assert ev["notas"] == ["1.385", "1.383"], "irmãs entregues mesmo assim"
    assert ev["unmarks"] == [], "entrada segue na fila pra 1.384"


def test_tudo_ja_entregue_da_baixa_sem_regenerar(monkeypatch) -> None:
    ok, ev, key = _rodar_nota_portal_cru(
        monkeypatch,
        portal_mps=[_mp_completa("1.385")],
        vistos=("1.385",),
    )
    assert ok is True and ev["notas"] == [] and ev["unmarks"] == [key]


def test_baixa_cobre_chave_com_ordem_diferente(monkeypatch) -> None:
    """Bug do /mp_fila de 13/08/2026: o botão gravou a entrada como
    '1.385,1.384,1.383' (ordem do anúncio) e a baixa rodou com a chave
    ordenada — string diferente, entrada órfã pra sempre com tudo entregue.
    A baixa agora varre as entradas da data por CONJUNTO de números."""
    orfa = f"{D.isoformat()}:1.385,1.384,1.383"
    ok, ev, key = _rodar_nota_portal_cru(
        monkeypatch,
        portal_mps=[_mp_completa("1.385"), _mp_completa("1.384"),
                    _mp_completa("1.383")],
        vistos=("1.385", "1.384"),
        entradas=(orfa,),
    )
    assert ok is True
    assert ev["notas"] == ["1.383"]
    assert key in ev["unmarks"], "a própria chave recebe baixa"
    assert orfa in ev["unmarks"], "a irmã fora de ordem morre junto"


def test_help_documenta_o_portal_verificador() -> None:
    from bot.handlers.start import HELP_TEXT, find_help_sections
    assert "Portal público como verificador" in HELP_TEXT
    for pergunta in ("e se o inlabs estiver fora do ar?",
                     "o bot usa o portal do dou?"):
        secoes = find_help_sections(pergunta)
        assert any("Portal público como verificador" in s for s in secoes), pergunta


# ─────────────── unidade: dia-controle e ausência conclusiva ───────────────

def test_dia_controle_e_o_ultimo_dia_util() -> None:
    assert dou_portal.dia_controle(date(2026, 8, 9)) == date(2026, 8, 7)   # dom→sex
    assert dou_portal.dia_controle(date(2026, 8, 8)) == date(2026, 8, 7)   # sáb→sex
    assert dou_portal.dia_controle(date(2026, 8, 11)) == date(2026, 8, 10)  # ter→seg


def test_controle_vivo_transforma_ausencia_em_sem_edicao() -> None:
    """Índice vazio pra D com a MESMA sonda devolvendo matérias no dia de
    controle = índice vivo cobrindo o período → 'não houve edição' vira
    evidência POSITIVA (a prova estrutural do raiz-sem-pasta, no portal)."""
    ctrl = dou_portal.dia_controle(D)

    def _resp(request):
        import httpx
        q = request.url.params.get("q", "")
        de = request.url.params.get("publishFrom", "")
        if de == ctrl.strftime("%d-%m-%Y") and "portaria" in q:
            return httpx.Response(200, text=_pagina([{"title": "PORTARIA 9"}]))
        return httpx.Response(200, text=_pagina([]))

    async def _main():
        with respx.mock:
            respx.route(host="www.in.gov.br").mock(side_effect=_resp)
            return await dou_portal.checar_dia_portal(D, controle=ctrl)

    dia = asyncio.run(_main())
    assert dia.mps == [] and dia.edicao_confirmada is False
    assert dia.sem_edicao is True


def test_controle_vazio_segue_inconclusivo() -> None:
    """Controle também vazio (feriado no controle / índice fora) → nada de
    afirmar ausência: sem_edicao continua False (lado seguro)."""
    async def _main():
        with respx.mock:
            _rotas({})
            return await dou_portal.checar_dia_portal(
                D, controle=dou_portal.dia_controle(D))

    dia = asyncio.run(_main())
    assert dia.sem_edicao is False and dia.edicao_confirmada is False


# ─────────── /mp_dou_agora com Inlabs fora → portal resolve na hora ───────────

def _rodar_manual(monkeypatch, portal_result, *, dias_atras=3):
    from bot.handlers import dou_mp
    ev = {"msgs": [], "baixas": [], "botoes": []}

    async def _portal(_d, **_kw):
        if isinstance(portal_result, Exception):
            raise portal_result
        return portal_result

    async def _baixa(_s, _u, d, entregues, falhas, motivo, **kw):
        ev["baixas"].append((d, entregues, motivo, kw.get("preservar_numeradas")))
        return True

    monkeypatch.setattr(dou_portal, "checar_dia_portal", _portal)
    monkeypatch.setattr(proactive, "baixa_checagem_manual", _baixa)

    class _Bot:
        async def send_message(self, _uid, text, reply_markup=None, **kw):
            ev["msgs"].append(text)
            if reply_markup is not None:
                for linha in reply_markup.inline_keyboard:
                    ev["botoes"] += [b.callback_data for b in linha]

    alvo = datetime.now(proactive.BRT).date() - timedelta(days=dias_atras)
    user = SimpleNamespace(id=9, is_authorized=True)
    ok = asyncio.run(dou_mp._checar_via_portal(_Bot(), _FakeSession(), user, alvo))
    return ok, ev, alvo


def test_manual_sem_edicao_da_baixa_na_hora(monkeypatch) -> None:
    """O caso 08-09/08 ao vivo: /mp_dou_agora não pode mais morrer em
    'recusou a sessão' quando o portal prova que o dia não teve edição."""
    ok, ev, alvo = _rodar_manual(
        monkeypatch, dou_portal.PortalDia([], False, sem_edicao=True))
    assert ok is True
    assert any("não houve edição" in m for m in ev["msgs"])
    assert ev["baixas"] == [(alvo, 0, "sem_edicao", None)]


def test_manual_edicao_sem_mp_da_baixa(monkeypatch) -> None:
    ok, ev, alvo = _rodar_manual(monkeypatch, dou_portal.PortalDia([], True))
    assert ok is True
    assert any("NENHUMA Medida Provisória" in m for m in ev["msgs"])
    assert ev["baixas"] == [(alvo, 0, "sem_mp", None)]


def test_manual_mp_manda_card_com_botao_sem_gerar(monkeypatch) -> None:
    """Dono, 13/08/2026: '/mp_dou_agora não deu botão por MP, gerou tudo'.
    Agora cada MP vem no card com o SEU botão de nota (sob demanda); o dia
    fechado recebe baixa preservando notas prometidas na fila."""
    ok, ev, alvo = _rodar_manual(
        monkeypatch, dou_portal.PortalDia(
            [_mp_completa("1.382"), _mp_completa("1.383")], True))
    assert ok is True
    cards = [m for m in ev["msgs"] if "Prazos" in m]
    assert len(cards) == 2 and any("1.382" in c for c in cards)
    assert ev["botoes"] == [
        f"doump:y:{alvo.isoformat()}:1.382",
        f"doump:y:{alvo.isoformat()}:1.383",
    ], "um botão POR MP, com o número dela"
    assert not any("Gerando" in m for m in ev["msgs"]), "nota é sob demanda"
    assert ev["baixas"] == [(alvo, 2, "portal_conclusivo", True)]


def test_manual_inconclusivo_devolve_pro_caminho_da_fila(monkeypatch) -> None:
    ok, ev, _ = _rodar_manual(monkeypatch, dou_portal.PortalDia([], False))
    assert ok is False and ev["msgs"] == [] and ev["baixas"] == []


# ───────── furos 1-3 da varredura de 11/08/2026 (portal primeiro) ─────────

def _rodar_tool(monkeypatch, portal_result, *, inlabs=None, inlabs_exc=None):
    """Roda a tool consultar_mp_dou (pergunta 'saiu MP hoje?' em chat/voz/
    foto/agendado). Inlabs mockado pra ESTOURAR se for consultado sem
    necessidade."""
    from bot.services import dou_monitor as dm
    from bot.services import tools
    registros = []

    async def _portal(_d, **_kw):
        if isinstance(portal_result, Exception):
            raise portal_result
        return portal_result

    async def _fetch(_d):
        if inlabs_exc:
            raise inlabs_exc
        assert inlabs is not None, "Inlabs consultado com o portal conclusivo"
        return inlabs

    monkeypatch.setattr(dou_portal, "checar_dia_portal", _portal)
    monkeypatch.setattr(dm, "fetch_mps", _fetch)
    monkeypatch.setattr(dm, "registrar_checagem_ok",
                        lambda d, n: registros.append((d, n)))
    monkeypatch.setattr(proactive.settings, "dou_portal_fallback", True)
    ctx = SimpleNamespace(tz="America/Sao_Paulo", fallback_text=None,
                          direct_html=None, short_circuit=False,
                          dou_mp_found=None)
    out = asyncio.run(tools._h_consultar_mp_dou({"data_iso": D.isoformat()}, ctx))
    return out, ctx, registros


def test_pergunta_saiu_mp_vai_no_portal_primeiro(monkeypatch) -> None:
    """Furo 1: 'saiu MP hoje?' ia DIRETO ao Inlabs. Agora o portal responde
    sozinho (o mock do Inlabs estoura se tocado) e alimenta a memória de
    checagem."""
    mp = _mp_completa("1.390")
    out, ctx, registros = _rodar_tool(
        monkeypatch, dou_portal.PortalDia([mp], True))
    assert ctx.short_circuit is True
    assert "1.390" in ctx.direct_html
    assert ctx.dou_mp_found == {"date_iso": D.isoformat(), "count": 1}
    assert registros == [(D, 1)]


def test_pergunta_sem_mp_conclusivo_pelo_portal(monkeypatch) -> None:
    out, ctx, registros = _rodar_tool(monkeypatch, dou_portal.PortalDia([], True))
    assert "Nenhuma MP publicada" in ctx.direct_html
    assert registros == [(D, 0)]


def test_pergunta_portal_inconclusivo_cai_no_inlabs(monkeypatch) -> None:
    out, ctx, _ = _rodar_tool(
        monkeypatch, dou_portal.PortalDia([], False),
        inlabs=[{"numero": "1.391", "ano": 2026, "ementa": "Via Inlabs."}])
    assert "1.391" in ctx.direct_html and "Via Inlabs" in ctx.direct_html


def test_pergunta_duas_fontes_mudas_avisa_as_duas(monkeypatch) -> None:
    from bot.services.dou_monitor import DouError
    out, ctx, _ = _rodar_tool(
        monkeypatch, dou_portal.PortalDia([], False),
        inlabs_exc=DouError("recusou a sessão"))
    assert "portal público inconclusivo" in ctx.direct_html
    assert "recusou a sessão" in ctx.direct_html


class _SessaoJob:
    async def get(self, _m, _id):
        return SimpleNamespace(id=_id, is_authorized=True,
                               dou_mp_subscribed=True)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def test_fila_numerada_tenta_portal_antes_do_inlabs(monkeypatch) -> None:
    """Furo 2: a fila de notas tentava o Inlabs primeiro (portal só nos
    excepts). Agora o portal resolve e o Inlabs nem é tocado."""
    from bot.db import session as db_session
    from bot.services import dou_monitor as dm
    ordem = []

    async def _nota_portal(bot, session, user, d, numeros, key):
        ordem.append(("portal", tuple(numeros)))
        return True

    async def _deliver(*a, **kw):
        raise AssertionError("Inlabs consultado com o portal resolvendo")

    async def _false(*a, **kw):
        return False

    async def _none(*a, **kw):
        return None

    monkeypatch.setattr(db_session, "SessionLocal", lambda: _SessaoJob())
    monkeypatch.setattr(proactive, "_tentar_nota_via_portal", _nota_portal)
    monkeypatch.setattr(dm, "deliver_to_user", _deliver)
    monkeypatch.setattr(proactive, "already_notified", _false)
    monkeypatch.setattr(proactive, "mark_notified", _none)
    monkeypatch.setattr(proactive, "unmark_notified", _none)
    monkeypatch.setattr(proactive.settings, "dou_portal_fallback", True)
    asyncio.run(proactive._entregar_nota_pendente(
        None, 42, D, ["1.382"], f"{D.isoformat()}:1.382"))
    assert ordem == [("portal", ("1.382",))]


def test_botao_com_numeros_tenta_portal_antes_do_inlabs(monkeypatch) -> None:
    """Furo 3: o botão 'gerar nota' (números embutidos) pulava o portal
    inteiro. Agora o texto da nota tenta a fonte primária antes."""
    from bot.db import session as db_session
    from bot.handlers import dou_mp
    ordem = []

    async def _nota_portal(bot, session, user, d, numeros, key):
        ordem.append(("portal", tuple(numeros), key))
        return True

    async def _deliver(*a, **kw):
        raise AssertionError("Inlabs antes do portal no botão")

    monkeypatch.setattr(db_session, "SessionLocal", lambda: _SessaoJob())
    monkeypatch.setattr(proactive, "_tentar_nota_via_portal", _nota_portal)
    monkeypatch.setattr(dou_mp, "deliver_to_user", _deliver)
    monkeypatch.setattr(proactive.settings, "dou_portal_fallback", True)
    asyncio.run(dou_mp._rodar_nota(None, 42, D, ["1.383"]))
    assert ordem == [("portal", ("1.383",), f"{D.isoformat()}:1.383")]
