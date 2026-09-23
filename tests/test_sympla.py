"""Retirada automática de ingresso na Sympla.

Pedido do dono (23/09/2026): toda quarta a Sympla libera 2 ingressos grátis
pro concerto da Orquestra Sinfônica. O evento só é PUBLICADO às 17h59 (URL
nova a cada semana), e a retirada abre às 18h00.

O que dá pra testar de verdade offline: a lógica de ESCOLHA do evento certo
entre os resultados da busca, a leitura do __NEXT_DATA__ (testada com o
FORMATO REAL extraído da página ao vivo durante o levantamento — inclusive
de eventos já encerrados que existem de fato), a matemática de janela
semanal, e a validação de credenciais. O que NÃO dá: a automação de
navegador em si — não existe evento aberto fora do minuto exato da
liberação pra gravar contra ele; ver o docstring de bot/services/sympla.py.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from bot.services import sympla as sy

BRT = ZoneInfo("America/Sao_Paulo")


# ───────────────────── escolher_evento ─────────────────────

def _c(titulo, dias_a_partir_de_hoje=0, hoje=None, encerrado=False):
    hoje = hoje or date(2026, 9, 23)
    d = hoje + timedelta(days=dias_a_partir_de_hoje) if dias_a_partir_de_hoje is not None else None
    return sy.EventoCandidato(titulo=titulo, url=f"https://x/{titulo}",
                              data_inicio=d, encerrado=encerrado)


def test_acha_por_titulo_ignorando_acento_e_caixa() -> None:
    hoje = date(2026, 9, 23)
    cands = [_c("ORQUESTRA SINFÔNICA do Teatro Nacional Cláudio Santoro | Concerto X",
                dias_a_partir_de_hoje=1, hoje=hoje)]
    escolhido = sy.escolher_evento(cands, sy.SEARCH_URL and
                                   "Orquestra Sinfônica do Teatro Nacional Claudio Santoro",
                                   hoje)
    assert escolhido is not None
    assert "Concerto X" in escolhido.titulo


def test_ignora_evento_encerrado() -> None:
    hoje = date(2026, 9, 23)
    cands = [_c("Orquestra Sinfônica Teatro Nacional", dias_a_partir_de_hoje=0,
                hoje=hoje, encerrado=True)]
    assert sy.escolher_evento(cands, "Orquestra Sinfônica Teatro Nacional", hoje) is None


def test_ignora_titulo_que_nao_bate_com_a_query() -> None:
    """Não confunde com OUTRO evento no mesmo teatro (ex.: peça de teatro)."""
    hoje = date(2026, 9, 23)
    cands = [_c("Peça de Teatro no Teatro Nacional", dias_a_partir_de_hoje=1, hoje=hoje)]
    assert sy.escolher_evento(cands, "Orquestra Sinfônica Teatro Nacional Claudio Santoro",
                              hoje) is None


def test_tolera_um_dia_pra_tras_por_causa_de_fuso() -> None:
    """Publicado 17h59 de uma quarta que já virou quinta em UTC — não pode
    rejeitar por isso."""
    hoje = date(2026, 9, 23)
    cands = [_c("Orquestra Sinfônica Teatro Nacional", dias_a_partir_de_hoje=-1, hoje=hoje)]
    assert sy.escolher_evento(cands, "Orquestra Sinfônica Teatro Nacional", hoje) is not None


def test_rejeita_evento_de_uma_semana_atras() -> None:
    hoje = date(2026, 9, 23)
    cands = [_c("Orquestra Sinfônica Teatro Nacional", dias_a_partir_de_hoje=-7, hoje=hoje)]
    assert sy.escolher_evento(cands, "Orquestra Sinfônica Teatro Nacional", hoje) is None


def test_escolhe_o_mais_proximo_entre_varios_validos() -> None:
    hoje = date(2026, 9, 23)
    cands = [
        _c("Orquestra Sinfônica Teatro Nacional — daqui a 10 dias", 10, hoje),
        _c("Orquestra Sinfônica Teatro Nacional — amanhã", 1, hoje),
        _c("Orquestra Sinfônica Teatro Nacional — daqui a 3 dias", 3, hoje),
    ]
    escolhido = sy.escolher_evento(cands, "Orquestra Sinfônica Teatro Nacional", hoje)
    assert "amanhã" in escolhido.titulo


def test_sem_candidato_nenhum_devolve_none_sem_inventar() -> None:
    assert sy.escolher_evento([], "Orquestra Sinfônica", date(2026, 9, 23)) is None


def test_query_so_com_stopwords_nao_casa_qualquer_coisa() -> None:
    """Sem termos úteis (só 'de'/'do'/'da'), não pode virar match universal —
    seria escolher um evento às cegas."""
    hoje = date(2026, 9, 23)
    cands = [_c("Qualquer Evento Aleatório", 1, hoje)]
    assert sy.escolher_evento(cands, "de do da", hoje) is None


# ───────────────────── evento_da_hydration ─────────────────────
# Formatos REAIS extraídos da página ao vivo durante o levantamento de
# 23/09/2026 (evento 3149828 e 3577985), não inventados.

def test_le_evento_aberto_do_hydration_real() -> None:
    payload = {
        "props": {"pageProps": {"hydrationData": {"eventHydration": {"event": {
            "id": 3149828,
            "name": "Orquestra Sinfônica do Teatro Nacional Claudio Santoro",
            "startDate": "2025-10-09 20:00:00",
            "isClosed": False,
        }}}}}
    }
    ev = sy.evento_da_hydration("https://x/3149828", payload)
    assert ev is not None
    assert ev.data_inicio == date(2025, 10, 9)
    assert ev.encerrado is False


def test_le_evento_encerrado_do_hydration_real() -> None:
    """O 'Concerto das Nações' (id 3577985) — já aconteceu, isClosed=True."""
    payload = {
        "props": {"pageProps": {"hydrationData": {"eventHydration": {"event": {
            "id": 3577985,
            "name": "Orquestra Sinfônica do Teatro Nacional Claudio Santoro | Concerto das Nações",
            "startDate": "2026-09-17 20:00:00",
            "endDate": "2026-09-17 22:00:00",
            "published": True,
            "isClosed": True,
            "cancelled": False,
        }}}}}
    }
    ev = sy.evento_da_hydration("https://x/3577985", payload)
    assert ev is not None
    assert ev.encerrado is True
    assert ev.data_inicio == date(2026, 9, 17)


def test_payload_sem_o_formato_esperado_nao_estoura() -> None:
    assert sy.evento_da_hydration("https://x", {}) is None
    assert sy.evento_da_hydration("https://x", {"props": {}}) is None
    assert sy.evento_da_hydration("https://x", None) is None


def test_data_ilegivel_nao_derruba_o_resto() -> None:
    payload = {
        "props": {"pageProps": {"hydrationData": {"eventHydration": {"event": {
            "name": "X", "startDate": "lixo-nao-e-data", "isClosed": False,
        }}}}}
    }
    ev = sy.evento_da_hydration("https://x", payload)
    assert ev is not None
    assert ev.data_inicio is None


# ───────────────────── proxima_janela ─────────────────────

def test_antes_da_janela_na_mesma_quarta() -> None:
    # Quarta 23/09/2026, 10h — janela é 18h da MESMA quarta.
    agora = datetime(2026, 9, 23, 10, 0, tzinfo=BRT)
    alvo = sy.proxima_janela(agora, weekday=2, hora=18)
    assert alvo == datetime(2026, 9, 23, 18, 0, tzinfo=BRT)


def test_depois_da_janela_pula_pra_proxima_semana() -> None:
    agora = datetime(2026, 9, 23, 18, 30, tzinfo=BRT)  # quarta, já passou 18h
    alvo = sy.proxima_janela(agora, weekday=2, hora=18)
    assert alvo == datetime(2026, 9, 30, 18, 0, tzinfo=BRT)


def test_dia_diferente_da_semana() -> None:
    agora = datetime(2026, 9, 21, 12, 0, tzinfo=BRT)  # segunda
    alvo = sy.proxima_janela(agora, weekday=2, hora=18)
    assert alvo == datetime(2026, 9, 23, 18, 0, tzinfo=BRT)


def test_nunca_devolve_algo_no_passado() -> None:
    import random
    random.seed(42)
    for _ in range(200):
        agora = datetime(2026, 9, 23, tzinfo=BRT) + timedelta(
            hours=random.uniform(0, 24 * 10))
        alvo = sy.proxima_janela(agora, weekday=random.randint(0, 6),
                                 hora=random.randint(0, 23))
        assert alvo > agora


# ───────────────────── credenciais (kv_settings) ─────────────────────

def _sessionmaker():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool, connect_args={"check_same_thread": False},
    )
    return async_sessionmaker(engine, expire_on_commit=False)


async def _com_tabelas(sm):
    from bot.db.models import Base
    async with sm.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def test_ciclo_completo_de_credenciais() -> None:
    async def _main():
        sm = _sessionmaker()
        await _com_tabelas(sm)
        async with sm() as s:
            assert await sy.get_credenciais(s) is None  # nada configurado
            await sy.save_email(s, "  vinicius@example.com  ")
            await sy.save_senha(s, "senha-forte-123")
            assert await sy.get_credenciais(s) is None  # falta o nome ainda
            await sy.save_nome(s, "Vinicius L Silva")
            creds = await sy.get_credenciais(s)
            assert creds is not None
            assert creds.email == "vinicius@example.com"
            assert creds.cpf is None
            await sy.save_cpf(s, "123.456.789-09")
            creds = await sy.get_credenciais(s)
            assert creds.cpf == "12345678909"
    asyncio.run(_main())


def test_email_invalido_recusado() -> None:
    async def _main():
        sm = _sessionmaker()
        await _com_tabelas(sm)
        async with sm() as s:
            with pytest.raises(sy.SymplaError):
                await sy.save_email(s, "nao-e-email")
    asyncio.run(_main())


def test_nome_sem_sobrenome_recusado() -> None:
    async def _main():
        sm = _sessionmaker()
        await _com_tabelas(sm)
        async with sm() as s:
            with pytest.raises(sy.SymplaError):
                await sy.save_nome(s, "Vinicius")
    asyncio.run(_main())


def test_cpf_com_digitos_errados_recusado() -> None:
    async def _main():
        sm = _sessionmaker()
        await _com_tabelas(sm)
        async with sm() as s:
            with pytest.raises(sy.SymplaError):
                await sy.save_cpf(s, "123")
    asyncio.run(_main())


def test_descrever_email_mascara_sem_reexibir() -> None:
    assert sy.descrever_email("vi@example.com") == "v•@example.com"
    masked = sy.descrever_email("vinicius@example.com")
    assert masked.startswith("v") and masked.endswith("s@example.com")
    assert "inicius" not in masked


# ───────────────────── help (regra do projeto) ─────────────────────

@pytest.mark.parametrize("frase", [
    "como configuro a sympla",
    "quero o ingresso da orquestra sinfonica",
    "como pego o ingresso do concerto",
])
def test_help_roteia_para_a_secao_sympla(frase: str) -> None:
    from bot.handlers.start import HELP_TEXT, find_help_sections

    assert "/sympla_setup" in HELP_TEXT
    assert "/sympla_testar" in HELP_TEXT
    secoes = find_help_sections(frase)
    assert any("sympla" in s.lower() for s in secoes), frase
