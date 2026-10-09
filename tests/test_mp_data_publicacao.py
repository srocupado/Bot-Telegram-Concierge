"""MP procurada no dia errado (bug real de 09/10/2026, MP 1.395).

O briefing anunciou a MP 1.395 (achada pela rede do Planalto); o dono clicou
em gerar a nota; o bot disse "Gerando…" e em seguida "Nenhuma MP nova no
Diário Oficial de 08/10/2026". Causa, conferida nas fontes reais:

- a página do Planalto tem no título "DE 8 DE OUTUBRO DE 2026" — data da
  ASSINATURA — e no rodapé "Este texto não substitui o publicado no DOU de
  9.10.2026" — data da PUBLICAÇÃO; o bot usava a do título;
- o portal do DOU tem a MP 1.395 em 09/10 e nenhuma MP em 08/10;
- o botão procurou em 08/10, não achou e respondeu com o texto genérico de
  dia sem MP — falso negativo sobre uma MP que o próprio bot tinha visto.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import httpx
import respx

from bot.services import dou_planalto as pl
from bot.services import dou_portal, proactive


# ───────────── 1) data de publicação vinda do rodapé do Planalto ─────────────

def _pagina(rodape: str = "") -> bytes:
    html = (
        "<html><body><p>MEDIDA PROVISÓRIA Nº 1.395, DE <br>8\n DE OUTUBRO DE 2026</p>"
        "<p>Abre crédito extraordinário.</p>"
        "<p>O PRESIDENTE DA REPÚBLICA, no uso da atribuição que lhe confere o "
        "art. 62 da Constituição, adota a seguinte Medida Provisória:</p>"
        + "".join(f"<p>Art. {i}º texto.</p>" for i in range(1, 700))
        + rodape + "</body></html>"
    )
    return html.encode("iso-8859-1")


@respx.mock
def test_data_vem_do_rodape_publicado_no_dou() -> None:
    """O rodapé fica DEPOIS das 500 linhas que o _parse guarda — por isso a
    página é longa aqui: o rodapé tem de ser lido da página inteira."""
    respx.get(pl.url_mp(1395, 2026)).mock(return_value=httpx.Response(200, content=_pagina(
        "<p>Este texto não substitui o publicado no DOU de 9.10.2026</p>")))
    mp = asyncio.run(pl.buscar_mp(1395, 2026))
    assert mp.data_publicacao == "2026-10-09"


@respx.mock
def test_sem_rodape_usa_a_data_do_titulo() -> None:
    respx.get(pl.url_mp(1395, 2026)).mock(return_value=httpx.Response(200, content=_pagina()))
    assert asyncio.run(pl.buscar_mp(1395, 2026)).data_publicacao == "2026-10-08"


def test_rodape_com_variacoes_de_escrita() -> None:
    for txt in ("publicado no D.O.U. de 9.10.2026", "Publicado no DOU de 09.10.2026",
                "publicada no D.O.U de 9.10.2026"):
        assert pl._data_publicacao_dou(txt.encode()) == date(2026, 10, 9), txt


# ───────────── 2) nota procurada nos dias seguintes ─────────────

class _Sessao:
    async def scalars(self, _stmt):
        return []

    async def commit(self):
        return None


def _mp(numero, d: date):
    return dou_portal.PortalMP(numero, 2026, f"MEDIDA PROVISÓRIA Nº {numero}",
                               "Ementa.", "https://x", texto="TEXTO INTEGRAL",
                               data_publicacao=d.isoformat())


def _rodar_portal(monkeypatch, por_dia: dict, alvo: date, numeros):
    from bot.services import dou_monitor as dm
    ev = {"notas": [], "consultas": [], "sends": []}

    async def _portal(d, **_kw):
        ev["consultas"].append(d)
        return dou_portal.PortalDia(por_dia.get(d, []), True)

    async def _nota(bot, user, mp, caption_extra=None):
        ev["notas"].append((mp["numero"], mp["data_publicacao"]))

    async def _nada(*a, **kw):
        return None

    async def _vazio(*a, **kw):
        return set()

    async def _send(_b, _u, t, **kw):
        ev["sends"].append(t)
        return True
    monkeypatch.setattr(dou_portal, "checar_dia_portal", _portal)
    monkeypatch.setattr(dm, "gerar_e_enviar_nota", _nota)
    monkeypatch.setattr(dm, "mark_seen", _nada)
    monkeypatch.setattr(proactive, "unmark_notified", _nada)
    monkeypatch.setattr(proactive, "_send", _send)
    monkeypatch.setattr(proactive, "notas_entregues", _vazio)
    monkeypatch.setattr(proactive, "marcar_nota_entregue", _nada)
    monkeypatch.setattr(proactive.settings, "dou_portal_fallback", True)
    ok = asyncio.run(proactive._tentar_nota_via_portal(
        None, _Sessao(), SimpleNamespace(id=1), alvo, list(numeros),
        f"{alvo.isoformat()}:{','.join(numeros)}", usuario_esperando=True))
    return ok, ev


HOJE = datetime.now(proactive.BRT).date()


def test_mp_no_dia_seguinte_e_achada_e_a_nota_sai(monkeypatch) -> None:
    """O caso real: pedido pra 08/10, MP no DOU de 09/10."""
    pedido = HOJE - timedelta(days=4)
    real = pedido + timedelta(days=1)
    ok, ev = _rodar_portal(monkeypatch, {real: [_mp("1395", real)]}, pedido, ["1395"])
    assert ok is True
    assert ev["notas"] == [("1395", real.isoformat())]
    assert ev["consultas"][:2] == [pedido, real]
    assert any(f"DOU de {real:%d/%m}" in t for t in ev["sends"]), "diz o dia REAL"


def test_mp_no_proprio_dia_nao_procura_mais_nada(monkeypatch) -> None:
    d = HOJE - timedelta(days=4)
    ok, ev = _rodar_portal(monkeypatch, {d: [_mp("1395", d)]}, d, ["1395"])
    assert ok is True and ev["consultas"] == [d]


def test_mp_que_nao_aparece_em_dia_nenhum_mantem_a_fila(monkeypatch) -> None:
    d = HOJE - timedelta(days=6)
    ok, ev = _rodar_portal(monkeypatch, {}, d, ["1395"])
    assert ok is False and ev["notas"] == []
    assert ev["consultas"] == [d + timedelta(days=i) for i in range(0, 4)]


def test_busca_nunca_passa_de_hoje() -> None:
    assert proactive._dias_seguintes(HOJE, 3) == []
    assert proactive._dias_seguintes(HOJE - timedelta(days=1), 3) == [HOJE]


# ───────────── 3) nunca "Nenhuma MP nova" pra MP específica ─────────────

class _Bot:
    def __init__(self):
        self.textos: list[str] = []

    async def send_message(self, _uid, texto, **kw):
        self.textos.append(texto)


def _avisar(monkeypatch, entregues=frozenset()):
    from bot.handlers import dou_mp
    marcas = []

    async def _ja(*a, **kw):
        return False

    async def _marca(_s, _u, kind, key):
        marcas.append((kind, key))

    async def _entregues(_s, _u):
        return set(entregues)
    monkeypatch.setattr(proactive, "already_notified", _ja)
    monkeypatch.setattr(proactive, "mark_notified", _marca)
    monkeypatch.setattr(proactive, "notas_entregues", _entregues)
    bot = _Bot()
    asyncio.run(dou_mp._avisar_mp_nao_achada(
        bot, None, SimpleNamespace(id=1), HOJE - timedelta(days=4), ["1395"],
        regerar=False))
    return bot.textos, marcas


def test_mp_nao_achada_diz_isso_e_deixa_na_fila(monkeypatch) -> None:
    textos, marcas = _avisar(monkeypatch)
    assert len(textos) == 1
    assert "1.395" in textos[0] and "NÃO quer dizer que não existe" in textos[0]
    assert "Nenhuma MP nova" not in textos[0]
    pedido = HOJE - timedelta(days=4)
    assert marcas == [("nota_pendente", f"{pedido.isoformat()}:1395")]


def test_ja_entregue_por_outro_dia_nao_avisa_nada(monkeypatch) -> None:
    textos, marcas = _avisar(monkeypatch, entregues={"1395"})
    assert textos == [] and marcas == []


def test_botao_com_numero_que_nao_acha_nao_usa_o_texto_de_dia_sem_mp(monkeypatch) -> None:
    """O caminho inteiro do botão: portal não acha, Inlabs devolve 0."""
    from bot.handlers import dou_mp
    from bot.db import session as dbs
    chamadas = []

    class _SL:
        async def __aenter__(self):
            return SimpleNamespace(get=_get)

        async def __aexit__(self, *a):
            return False

    async def _get(_model, _uid):
        return SimpleNamespace(id=1, is_authorized=True)

    async def _portal(*a, **kw):
        return False

    async def _deliver(*a, **kw):
        return 0, [], "sem_mp"

    async def _avisar(*a, **kw):
        chamadas.append(("avisar", a[4]))
    monkeypatch.setattr(dbs, "SessionLocal", lambda: _SL())
    monkeypatch.setattr(proactive, "_tentar_nota_via_portal", _portal)
    monkeypatch.setattr(dou_mp, "deliver_to_user", _deliver)
    monkeypatch.setattr(dou_mp, "_avisar_mp_nao_achada", _avisar)
    monkeypatch.setattr(dou_mp.settings, "dou_portal_fallback", True)
    bot = _Bot()
    asyncio.run(dou_mp._rodar_nota(bot, 1, HOJE - timedelta(days=4), ["1395"]))
    assert chamadas == [("avisar", ["1395"])]
    assert not any("Nenhuma MP nova" in t for t in bot.textos)
