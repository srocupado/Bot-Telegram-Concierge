"""Janelas proativas fora da hora redonda (PROACTIVE_MINUTE, 04/08/2026).

Hipótese do dono: o Inlabs degrada nos minutos :00 — pico de bots/rotinas
do mundo inteiro disparando junto. Deslocar o disparo pra 7h05/13h05/19h05
custa zero e tira o bot da multidão. O catch-up anda junto com o alvo
([alvo, alvo+20min]), e os textos passam a dizer '13h05' pra não parecer
atraso.
"""
from __future__ import annotations

from datetime import datetime

from bot.config import settings
from bot.services import proactive
from bot.services.scheduler import _PROACTIVE_CATCHUP_MIN, janela_proativa

HOURS = {7, 13, 19}
BRIEFING = 7


def _t(h: int, m: int) -> datetime:
    return datetime(2026, 8, 4, h, m)


def test_default_do_projeto_e_5() -> None:
    assert settings.proactive_minute == 5, (
        "o deslocamento pros minutos :05 é decisão do dono — mudar o default "
        "volta o bot pro pico da hora redonda do Inlabs"
    )


def test_janela_desloca_com_minuto_alvo() -> None:
    # Antes do alvo: nada dispara (é o intervalo 13h00–13h04 que motivou tudo).
    assert janela_proativa(_t(13, 0), HOURS, BRIEFING, 5) is None
    assert janela_proativa(_t(13, 4), HOURS, BRIEFING, 5) is None
    # No alvo e dentro do catch-up deslocado: dispara.
    assert janela_proativa(_t(13, 5), HOURS, BRIEFING, 5) == "regular"
    assert janela_proativa(_t(13, 5 + _PROACTIVE_CATCHUP_MIN), HOURS, BRIEFING, 5) == "regular"
    # Depois do catch-up: fecha.
    assert janela_proativa(_t(13, 6 + _PROACTIVE_CATCHUP_MIN), HOURS, BRIEFING, 5) is None
    # Briefing e catch-up do briefing andam junto.
    assert janela_proativa(_t(7, 5), HOURS, BRIEFING, 5) == "briefing"
    assert janela_proativa(_t(9, 2), HOURS, BRIEFING, 5) is None
    assert janela_proativa(_t(9, 7), HOURS, BRIEFING, 5) == "briefing"


def test_minuto_alvo_zero_preserva_comportamento_antigo() -> None:
    assert janela_proativa(_t(13, 0), HOURS, BRIEFING) == "regular"
    assert janela_proativa(_t(13, _PROACTIVE_CATCHUP_MIN), HOURS, BRIEFING) == "regular"
    assert janela_proativa(_t(13, _PROACTIVE_CATCHUP_MIN + 1), HOURS, BRIEFING) is None


def test_fmt_hora_janela_mostra_o_minuto(monkeypatch) -> None:
    monkeypatch.setattr(settings, "proactive_minute", 5)
    assert proactive._fmt_hora_janela(13) == "13h05"
    monkeypatch.setattr(settings, "proactive_minute", 0)
    assert proactive._fmt_hora_janela(13) == "13h"


def test_janelas_restantes_conta_a_hora_corrente_antes_do_disparo(monkeypatch) -> None:
    """Entre 13h00 e 13h04 a janela das 13h05 ainda vai acontecer — o /mp_fila
    não pode pular direto pras 19h."""
    monkeypatch.setattr(settings, "proactive_hours", "7,13,19")
    monkeypatch.setattr(settings, "proactive_minute", 5)
    assert proactive._janelas_restantes(13, 2) == [13, 19]
    assert proactive._janelas_restantes(13, 5) == [19]   # disparo já passou
    assert proactive._janelas_restantes(13, 30) == [19]
    # Sem minuto (chamada legada): conservador, hora corrente não conta.
    assert proactive._janelas_restantes(13) == [19]
    assert proactive._janelas_restantes(19, 40) == []
