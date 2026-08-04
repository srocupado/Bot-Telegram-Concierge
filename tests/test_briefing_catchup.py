"""Catch-up do briefing perdido (restart/queda entre 7h e meio-dia).

Incidente real (03/08/2026): o bot subiu às 08:53 e o briefing das 7h —
com a conferência diária da Câmara e a re-checagem de ontem, que só rodam
nele — foi perdido o dia INTEIRO (a janela era hora exata + 20 min).

Agora o briefing pendente é recuperado no início de cada hora até meio-dia
local, com o run_key POR DIA garantindo execução única. Hora regular
configurada tem precedência sobre o catch-up (senão uma PROACTIVE_HOURS
com janela entre 8h e 11h teria essa janela engolida).
"""
from __future__ import annotations

from datetime import date, datetime

from bot.services.proactive import run_key_da_janela
from bot.services.scheduler import _BRIEFING_CATCHUP_ATE_H, janela_proativa

HOURS = {7, 13, 19}
BRIEFING = 7


def _t(h: int, m: int) -> datetime:
    return datetime(2026, 8, 3, h, m)


def test_briefing_na_hora_certa() -> None:
    assert janela_proativa(_t(7, 5), HOURS, BRIEFING) == "briefing"


def test_minuto_alem_do_catchup_nao_dispara() -> None:
    assert janela_proativa(_t(7, 25), HOURS, BRIEFING) is None


def test_janela_regular() -> None:
    assert janela_proativa(_t(13, 10), HOURS, BRIEFING) == "regular"
    assert janela_proativa(_t(19, 0), HOURS, BRIEFING) == "regular"


def test_catchup_recupera_briefing_perdido() -> None:
    """O caso do restart às 08:53: às 9h em ponto o briefing roda."""
    assert janela_proativa(_t(9, 3), HOURS, BRIEFING) == "briefing"
    assert janela_proativa(_t(11, 20), HOURS, BRIEFING) == "briefing"


def test_catchup_para_ao_meio_dia() -> None:
    assert janela_proativa(_t(_BRIEFING_CATCHUP_ATE_H, 0), HOURS, BRIEFING) is None


def test_fora_de_qualquer_janela() -> None:
    assert janela_proativa(_t(15, 0), HOURS, BRIEFING) is None
    assert janela_proativa(_t(20, 5), HOURS, BRIEFING) is None


def test_hora_regular_nao_e_engolida_pelo_catchup() -> None:
    """PROACTIVE_HOURS com janela DENTRO da faixa de catch-up (ex.: 10h):
    a janela regular tem que rodar — o briefing atrasado pega a hora
    seguinte. Sem a precedência, a regular das 10h sumia todo dia em que o
    briefing já tivesse saído às 7h."""
    assert janela_proativa(_t(10, 5), {7, 10, 13}, BRIEFING) == "regular"


# ───────────────────────── run_key por janela ─────────────────────────

def test_run_key_do_briefing_e_por_dia() -> None:
    """Com o catch-up tentando a cada hora, chave com hora deixaria o MESMO
    briefing rodar de novo às 8h, 9h… A chave por dia derruba as repetições."""
    d = date(2026, 8, 3)
    assert run_key_da_janela("briefing", d, 7) == "briefing:2026-08-03"
    assert (run_key_da_janela("briefing", d, 7)
            == run_key_da_janela("briefing", d, 11))


def test_run_key_regular_continua_por_hora() -> None:
    d = date(2026, 8, 3)
    assert (run_key_da_janela("regular", d, 13)
            != run_key_da_janela("regular", d, 19))
    assert run_key_da_janela("regular", d, 13) == "regular:2026-08-03:13"
