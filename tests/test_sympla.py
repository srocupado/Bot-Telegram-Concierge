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


# ───────────────── falha real de 23/09/2026: "Entrar" não existe ─────────────
# Primeiro uso real: TimeoutError esperando get_by_role("button", name=~entrar)
# — investigação contra os bundles JS baixados no levantamento mostrou que
# "Entrar" não existe como STRING em lugar nenhum do código da Sympla (o
# gatilho do header provavelmente é ícone sem texto, com rótulo vindo de
# tradução externa). A correção troca "adivinha um texto de botão" por "vai
# direto na rota oficial de login" (sympla.com.br/login redireciona pra
# /#login — confirmado contra o site real, não é chute) e só cai pra
# candidatos de texto como plano B.

class _ElementoFalso:
    def __init__(self, texto):
        self._texto = texto

    async def wait_for(self, **kw):
        pass


class _PaginaFalsa:
    """Dublê mínimo só pra exercitar _dump_clicaveis sem navegador real."""

    def __init__(self, textos):
        self._textos = textos

    async def eval_on_selector_all(self, _sel, _js):
        return self._textos


def test_dump_clicaveis_lista_e_deduplica() -> None:
    pagina = _PaginaFalsa(["Entrar", "Cadastrar", "Entrar", "  ", "Buscar"])
    out = asyncio.run(sy._dump_clicaveis(pagina))
    assert out == "Entrar | Cadastrar | Buscar"


def test_dump_clicaveis_sem_elementos_nao_finge_achar_algo() -> None:
    pagina = _PaginaFalsa([])
    out = asyncio.run(sy._dump_clicaveis(pagina))
    assert "nenhum elemento" in out


def test_dump_clicaveis_trunca_pra_nao_estourar_mensagem() -> None:
    pagina = _PaginaFalsa([f"Botão número {i} com texto longo" for i in range(200)])
    out = asyncio.run(sy._dump_clicaveis(pagina))
    assert len(out) <= 801  # 800 + "…"
    assert out.endswith("…")


def test_dump_clicaveis_pagina_que_estoura_nao_derruba_o_caller() -> None:
    class _Explode:
        async def eval_on_selector_all(self, *a):
            raise RuntimeError("página fechou no meio")
    out = asyncio.run(sy._dump_clicaveis(_Explode()))
    assert "não consegui listar" in out


def _sem_comentarios(src: str) -> str:
    """Remove linhas de comentário puro — evita que uma asserção sobre o
    CÓDIGO passe só porque a string aparece num comentário/docstring
    explicando a decisão (bug real do 1º rascunho deste teste: a mutação
    que trocava o goto() continuava passando, porque "/#login" sobrevivia
    no comentário logo acima)."""
    return "\n".join(
        ln for ln in src.splitlines()
        if not ln.strip().startswith("#")
    )


def test_login_carrega_pagina_limpa_e_delega_a_abertura() -> None:
    """2ª falha real (23/09/2026): a 1ª versão ia direto pra "…/#login" —
    mas o PRÓPRIO dump que ela trouxe na falha provou que isso não abre
    nada (nenhuma palavra de conta/login apareceu na tela). Motivo provável:
    o roteador só reage ao EVENTO hashchange, não ao hash já presente no
    carregamento inicial. Agora a página carrega LIMPA e quem muda o hash
    é _abrir_login, depois de montada — ver os testes dela abaixo."""
    import inspect
    src = _sem_comentarios(inspect.getsource(sy._login))
    assert 'page.goto(BASE_URL,' in src
    assert '"{BASE_URL}/#login"' not in src, "voltou a carregar já com o hash"
    assert "_abrir_login(page)" in src


def test_login_usa_tipo_de_input_nao_label_ou_classe_css() -> None:
    """Rótulo ARIA e classe CSS são coisas que a Sympla controla e pode
    trocar a qualquer deploy; tipo de input é HTML semântico padrão."""
    import inspect
    src = inspect.getsource(sy._login)
    assert "input[type=email]" in src
    assert "get_by_label" not in src, "voltou a depender de rótulo ARIA"


def test_abrir_login_dispara_hashchange_de_verdade() -> None:
    """Não pode voltar a ser um goto() com o hash já pronto — tem que ser
    uma mudança de hash DEPOIS da página montada, pra disparar o evento."""
    import inspect
    src = _sem_comentarios(inspect.getsource(sy._abrir_login))
    assert "window.location.hash = 'login'" in src
    assert "page.evaluate(" in src


def test_abrir_login_tenta_a_pista_open_dropdown() -> None:
    """A pista concreta do dump real: "Open Dropdown" é texto em inglês
    solto numa tela em português — cara de rótulo de biblioteca não
    traduzido, forte candidato a ícone de conta sem aria-label."""
    import inspect
    src = inspect.getsource(sy._abrir_login)
    assert "open dropdown" in src.lower()


def test_abrir_login_tenta_varias_ocorrencias_do_dropdown_nao_so_a_primeira() -> None:
    """"Open Dropdown" pode não ser único (idioma, moeda, notificação usam o
    mesmo rótulo genérico) — usar só .first arriscaria abrir o dropdown
    errado e nunca sobrar tentativa pro certo."""
    import inspect
    src = inspect.getsource(sy._abrir_login)
    assert ".all()" in src
    assert "Escape" in src, "não fecha o dropdown errado antes do próximo"


def test_abrir_login_ainda_tenta_texto_candidato_como_ultimo_recurso() -> None:
    import inspect
    src = inspect.getsource(sy._abrir_login)
    assert "entrar" in src.lower() and "fazer login" in src.lower()


def test_abrir_login_sem_estrategia_nenhuma_reporta_com_diagnostico() -> None:
    """Falha de TODAS as estratégias tem que vir com o dump — é a diferença
    entre 'estourou de novo' e 'aqui está o texto real da tela'."""
    import inspect
    src = inspect.getsource(sy._abrir_login)
    assert "_dump_clicaveis" in src
    assert "SymplaError" in src


def test_falha_fora_do_login_tambem_carrega_o_dump() -> None:
    import inspect
    src = inspect.getsource(sy.retirar_ingresso)
    assert "_dump_clicaveis" in src
    assert "isinstance(exc, SymplaError)" in src, (
        "sem essa checagem o dump do login duplicaria dentro dele mesmo"
    )
