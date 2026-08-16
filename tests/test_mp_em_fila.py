"""/mp_em_fila: visibilidade da fila do monitor de MP (notas aguardando
geração + dias pendentes de re-checagem). Read-only — consultar a fila não
pode alterá-la.
"""
from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

from bot.services import proactive
from bot.services.proactive import _MP_RETRO_EXPIRA_DIAS
from bot.handlers.dou_mp import _fmt_alvo, _fmt_fila_mp


class _Sess:
    def __init__(self, rows):
        self._rows = rows

    async def scalars(self, _stmt):
        return list(self._rows)


def _n(kind, key):
    return SimpleNamespace(kind=kind, key=key)


# ───────────────────────── leitor da fila (serviço) ─────────────────────────

def test_listar_fila_notas_so_com_numero_all_vira_dia() -> None:
    hoje = date(2026, 8, 2)
    rows = [
        _n("nota_pendente", "2026-08-01:1382"),   # número conhecido → NOTA
        _n("nota_pendente", "2026-07-30:all"),    # 'all' → DIA a verificar
        _n("mp_pendente", "2026-07-29"),          # → DIA (dentro da janela)
        _n("mp_pendente", "2026-07-01"),          # > 14 dias → fora do output
        _n("dou_manut", "now"),
    ]
    fila = asyncio.run(proactive.listar_fila_mp(_Sess(rows), 1, hoje))

    assert fila["manutencao"] is True
    # só a nota com número entra em 'notas'; a 'all' NÃO
    assert fila["notas"] == [(date(2026, 8, 1), "1382")]
    datas = dict(fila["dias"])
    assert date(2026, 7, 30) in datas, "'all' devia virar dia a verificar"
    assert date(2026, 7, 29) in datas, "dia dentro da janela sumiu da fila"
    assert date(2026, 7, 1) not in datas, "dia expirado não devia aparecer"
    assert datas[date(2026, 7, 29)] == _MP_RETRO_EXPIRA_DIAS - 4  # 02/08 - 29/07 = 4d


def test_listar_fila_dedup_all_e_mp_pendente_do_mesmo_dia() -> None:
    """O caso do 02/08: nota_pendente 'all' + mp_pendente do MESMO dia não
    podem listar o dia DUAS vezes."""
    hoje = date(2026, 8, 2)
    rows = [
        _n("nota_pendente", "2026-08-02:all"),
        _n("mp_pendente", "2026-08-02"),
    ]
    fila = asyncio.run(proactive.listar_fila_mp(_Sess(rows), 1, hoje))
    assert fila["notas"] == []
    assert [d for d, _ in fila["dias"]] == [date(2026, 8, 2)], "dia duplicado"


def test_listar_fila_e_read_only(monkeypatch) -> None:
    """Consultar a fila NÃO pode expirar/remover nada — senão ver a fila
    mudaria a fila. A expiração é responsabilidade do proativo."""
    tocou: list = []

    async def _spy(*a, **kw):
        tocou.append(a)

    monkeypatch.setattr(proactive, "unmark_notified", _spy)
    monkeypatch.setattr(proactive, "mark_notified", _spy)
    hoje = date(2026, 8, 2)
    rows = [_n("mp_pendente", "2026-06-01")]      # bem expirado
    asyncio.run(proactive.listar_fila_mp(_Sess(rows), 1, hoje))
    assert tocou == [], "listar a fila alterou o estado (efeito colateral)"


def test_listar_fila_ignora_chave_corrompida() -> None:
    hoje = date(2026, 8, 2)
    rows = [_n("mp_pendente", "lixo"), _n("nota_pendente", "sem-data")]
    fila = asyncio.run(proactive.listar_fila_mp(_Sess(rows), 1, hoje))
    assert fila["dias"] == []      # data inválida (mp_pendente) descartada
    # nota 'all' SEM data não vira "dia" (sem data pra verificar) nem "nota"
    # (sem número) — fica de fora do /mp_fila; o proativo a expira/limpa.
    assert fila["notas"] == []


# ─────────────────────────── formatação (verbatim) ──────────────────────────

def test_fmt_alvo() -> None:
    assert _fmt_alvo("all") == "todas as MPs"
    assert _fmt_alvo("") == "todas as MPs"
    assert _fmt_alvo("1382") == "MP 1382"
    assert _fmt_alvo("1382,1383") == "MPs 1382, 1383"


def test_fmt_fila_vazia() -> None:
    out = _fmt_fila_mp({"notas": [], "dias": [], "manutencao": False})
    assert "Fila do DOU vazia" in out
    assert "manuten" not in out.lower()


def test_fmt_fila_vazia_com_manutencao() -> None:
    out = _fmt_fila_mp({"notas": [], "dias": [], "manutencao": True})
    assert "vazia" in out and "manutenção" in out


def test_fmt_fila_com_notas_e_dias() -> None:
    """Dia FECHADO preso por falha: é o único que carrega o contador de
    desistência — e sempre com o aviso de que desistir avisa."""
    fila = {
        "notas": [(date(2026, 8, 1), "1382")],
        "dias": [(date(2026, 7, 30), 11), (date(2026, 7, 29), 10)],
        "manutencao": True,
    }
    out = _fmt_fila_mp(fila)
    assert "MP 1382 de 01/08/2026" in out
    assert "30/07/2026 — re-checando a cada janela; desisto (com aviso) em 11 dia(s)" in out
    assert "29/07/2026 — re-checando a cada janela; desisto (com aviso) em 10 dia(s)" in out
    assert "Inlabs em manutenção" in out
    assert "/mp_dou_agora" in out


def test_manutencao_sem_nota_na_fila_nao_promete_espera(monkeypatch) -> None:
    """Dono, 16/08/2026: com só o DIA na fila, o texto dizia 'a fila drena
    quando ele voltar' — resíduo do desenho antigo. Desde a inversão o
    portal é a fonte primária e a checagem não espera o Inlabs."""
    fila = {"notas": [], "dias": [(date(2026, 8, 16), 14)], "manutencao": True}
    out = _fmt_fila_mp(fila)
    assert "drena quando" not in out and "voltar" not in out
    assert "portal oficial" in out and "sem efeito aqui" in out


def test_manutencao_com_nota_na_fila_explica_o_que_espera() -> None:
    """Com NOTA na fila o Inlabs ainda importa — mas só pro TEXTO que o
    portal não entregou íntegro; a checagem dos dias segue pelo portal."""
    fila = {"notas": [(date(2026, 8, 14), "1390")], "dias": [],
            "manutencao": True}
    out = _fmt_fila_mp(fila)
    assert "checagem dos dias segue pelo portal" in out
    assert "texto de nota" in out
    assert "drena quando" not in out


def test_fmt_fila_sem_manutencao_nao_fala_em_inlabs() -> None:
    """Com o Inlabs ONLINE, o texto não pode afirmar 'quando o Inlabs voltar'
    (bug do dono: /mp_fila mentia com o Inlabs de pé)."""
    fila = {"notas": [], "dias": [(date(2026, 8, 2), 14)], "manutencao": False}
    out = _fmt_fila_mp(fila)
    assert "Inlabs" not in out, "afirmou Inlabs sem causa apurada"
    assert "voltar" not in out
    assert "02/08/2026 — re-checando a cada janela; desisto (com aviso) em 14 dia(s)" in out


def test_fmt_fila_dia_aberto_diz_o_desfecho_de_hoje() -> None:
    """Dia ABERTO com janelas restantes: o desfecho esperado é HOJE (a extra
    das 19h pode resolver) ou o briefing — NUNCA 'por mais 14 dias', que
    soava como prazo esperado (pergunta do dono, 03/08/2026)."""
    d = date(2026, 8, 3)
    fila = {"notas": [], "dias": [(d, 14)], "abertos": [d],
            "janelas_hoje": [13, 19], "manutencao": False}
    out = _fmt_fila_mp(fila)
    assert ("03/08/2026 — re-checo hoje às 13h05 e às 19h05; o desfecho sai "
            "até o briefing de amanhã") in out
    assert "14 dia(s)" not in out


def test_fmt_fila_dia_aberto_sem_janelas_fecha_no_briefing() -> None:
    """Depois das 19h (sem janelas restantes): o que falta é o fechamento
    do dia (6h) — o briefing resolve."""
    d = date(2026, 8, 3)
    fila = {"notas": [], "dias": [(d, 14)], "abertos": [d],
            "janelas_hoje": [], "manutencao": False}
    out = _fmt_fila_mp(fila)
    assert "03/08/2026 — fecho no briefing de amanhã (o dia encerra de madrugada)" in out
    assert "14 dia(s)" not in out
