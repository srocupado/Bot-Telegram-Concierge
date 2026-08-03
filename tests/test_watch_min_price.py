"""Vigias de viagem: queda de preço não pode ser perdida por envio falho.

Bug da auditoria de 03/08/2026: `min_price_seen` avançava ANTES de saber se
o alerta foi entregue. Voo caía pra R$ 1.500 (novo mínimo) → Telegram fora →
`sent=False` → mínimo gravado mesmo assim → no dia seguinte o preço era
"no_change" e o alerta NUNCA saía. Perda silenciosa — o `last_alert_at` já
tinha o cuidado de só avançar no sucesso; o mínimo não.
"""
from __future__ import annotations

from types import SimpleNamespace

from bot.services.travels.watches import _registrar_leitura, _should_alert


def _watch(**kw):
    base = dict(max_price=None, min_price_seen=None, last_price=None,
                last_alert_at=None, snooze_until=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_envio_ok_avanca_o_minimo() -> None:
    w = _watch(min_price_seen=2000.0)
    fire, reason = _should_alert(w, 1500.0)
    assert fire and reason == "new_min"
    _registrar_leitura(w, 1500.0, alerta_entregue=True)
    assert w.min_price_seen == 1500.0
    assert w.last_price == 1500.0


def test_envio_falho_nao_avanca_e_realerta_no_dia_seguinte() -> None:
    """O cenário do relatório: queda + Telegram fora → o mínimo NÃO avança e
    a checagem seguinte volta a disparar o alerta."""
    w = _watch(min_price_seen=2000.0)
    fire, _ = _should_alert(w, 1500.0)
    assert fire
    _registrar_leitura(w, 1500.0, alerta_entregue=False)
    assert w.min_price_seen == 2000.0, (
        "mínimo avançou com envio falho — queda de preço perdida pra sempre"
    )
    # Dia seguinte, mesmo preço: tem que disparar de novo.
    fire2, reason2 = _should_alert(w, 1500.0)
    assert fire2 and reason2 == "new_min"


def test_sem_alerta_a_entregar_minimo_acompanha() -> None:
    """Leitura comum (preço não caiu abaixo do mínimo): estado atualiza
    normalmente — entregue=True é o default do caminho sem fire."""
    w = _watch(min_price_seen=1000.0)
    fire, _ = _should_alert(w, 1200.0)
    assert not fire
    _registrar_leitura(w, 1200.0, alerta_entregue=True)
    assert w.min_price_seen == 1000.0
    assert w.last_price == 1200.0
