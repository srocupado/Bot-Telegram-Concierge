"""Busca de lançamento por DESCRIÇÃO no histórico.

Dono, 13/09/2026: "Eu não tinha comprado um drone parcelado?" — o bot
respondeu despejando a fatura aberta inteira (37 itens) e não respondeu nada.

Duas faltas somadas:

1. Nenhuma tool procurava por TEXTO. O modelo chamou consultar_lancamentos,
   recebeu a fatura verbatim e o short_circuit encerrou o turno — ele nem
   conseguiu dizer "não achei, quer que eu procure no histórico?".
2. A fatura aberta é o lugar ERRADO pra procurar. Parcelada comprada há mais
   tempo pode já ter TERMINADO: some da fatura e da lista de "parceladas
   ativas", mas continua no histórico. Era o caso — a fatura tinha "Case
   Avata 2" (acessório), e o drone estava mais atrás.
"""
from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

from bot.services import financeiro as fin

UID = "uid-dono"
HOJE = date(2026, 9, 13)
FECHAMENTO = 20          # cardClosingDay do dono

# Recorte real da conta: o acessório está na fatura aberta, a parcelada do
# drone terminou meses atrás, e há uma parcelada ainda correndo.
_STATE = {
    "settings": {"cardClosingDay": FECHAMENTO, "cardDueDay": 1},
    "cardEntries": [
        {"id": "d1", "date": "2025-10-05", "desc": "DJI Avata 2 Fly More",
         "category": "outros", "amount": 6000.0, "installments": 10},
        {"id": "c1", "date": "2026-08-26", "desc": "Case Avata 2",
         "category": "outros", "amount": 150.0, "installments": 1},
        {"id": "g1", "date": "2026-05-11", "desc": "GoPro Hero 13",
         "category": "compras", "amount": 2299.0, "installments": 10},
        {"id": "x1", "date": "2026-09-06", "desc": "Compra no crédito",
         "category": "outros", "amount": 1620.0, "installments": 3},
    ],
    "bankTransactions": [
        {"id": "b1", "date": "2026-03-02", "desc": "Seguro do drone",
         "category": "outros", "type": "debito", "amount": 320.0},
    ],
}


@pytest.fixture(autouse=True)
def _firestore_dublado(monkeypatch):
    async def _db(_session):
        return object()

    async def _state(_db, _uid):
        return _STATE

    class _Relogio(fin.datetime):
        @classmethod
        def now(cls, tz=None):
            return fin.datetime(2026, 9, 13, 12, 0, tzinfo=tz)

    monkeypatch.setattr(fin, "_get_db", _db)
    monkeypatch.setattr(fin, "_read_state", _state)
    monkeypatch.setattr(fin, "datetime", _Relogio)


def _buscar(*termos):
    user = SimpleNamespace(firebase_uid=UID)
    return asyncio.run(fin.buscar_lancamentos(None, user, list(termos)))


# ───────────────── a pergunta que originou tudo ─────────────────

def test_acha_parcelada_concluida_que_sumiu_da_fatura() -> None:
    """O drone: 10x a partir de out/2025, terminada muito antes de hoje. Não
    está na fatura aberta nem nas 'parceladas ativas' — só no histórico."""
    out = _buscar("drone", "avata", "dji")
    assert "DJI Avata 2 Fly More" in out
    assert "concluída" in out, out
    assert "10x" in out


def test_nao_confunde_o_acessorio_com_a_compra() -> None:
    """'Case Avata 2' também casa com 'avata' — e deve aparecer, mas como
    compra à vista de R$ 150, não como o drone."""
    out = _buscar("avata")
    assert "Case Avata 2" in out and "DJI Avata 2" in out
    linha_case = next(l for l in out.splitlines() if "Case Avata 2" in l)
    assert "R$ 150,00" in linha_case
    assert "x de" not in linha_case, "tratou compra à vista como parcelada"


def test_parcelada_em_curso_diz_onde_esta_e_quanto_falta() -> None:
    """GoPro: compra 11/05/2026, fechamento dia 20 → 1ª parcela na fatura
    05/2026. Em 13/09 (fatura 09/2026) são 4 faturas decorridas → 5ª parcela,
    restam 5, última em 02/2027."""
    out = _buscar("gopro")
    assert "5/10" in out, out
    assert "restam 5" in out
    assert "02/2027" in out


def test_busca_ignora_acento_e_caixa() -> None:
    _STATE["cardEntries"].append(
        {"id": "s1", "date": "2026-05-27", "desc": "Sofá Galeano",
         "category": "outros", "amount": 7550.0, "installments": 10})
    try:
        assert "Sofá Galeano" in _buscar("SOFA")
    finally:
        _STATE["cardEntries"].pop()


def test_procura_tambem_no_banco() -> None:
    out = _buscar("drone")
    assert "Seguro do drone" in out
    assert "−R$ 320,00" in out, "débito sem sinal de saída"


def test_sem_resultado_nao_finge_e_explica_a_limitacao() -> None:
    """Busca é por texto: dizer só 'não achei' esconde que o nome pode ser
    outro. O dono precisa saber pra tentar de novo."""
    out = _buscar("geladeira")
    assert "Não achei" in out
    assert "outro nome" in out


def test_descricao_do_usuario_e_escapada() -> None:
    """A descrição vem do usuário e o envio é HTML — '<b>' cru quebraria a
    mensagem inteira no Telegram (parse_mode error), não só a linha."""
    _STATE["cardEntries"].append(
        {"id": "h1", "date": "2026-09-01", "desc": "TV 50<b> polegadas",
         "category": "outros", "amount": 2000.0, "installments": 1})
    try:
        out = _buscar("polegadas")
        assert "50&lt;b&gt; polegadas" in out
    finally:
        _STATE["cardEntries"].pop()


# ─────────────────────── contrato da tool ───────────────────────

def test_tool_manda_verbatim_e_encerra(monkeypatch) -> None:
    from bot.services import tools
    from bot.services.llm.base import ToolContext

    ctx = ToolContext(user=SimpleNamespace(id=1, firebase_uid=UID),
                      session=None, tz="America/Sao_Paulo")
    r = asyncio.run(tools._h_buscar_lancamento(
        {"termos": ["drone", "avata", "dji"]}, ctx))
    assert ctx.short_circuit is True
    assert "não escreva nada" in r
    assert "DJI Avata 2" in (ctx.direct_html or "")
    # HTML já montado: escapar de novo mostraria as tags na tela.
    assert "<b>" in ctx.direct_html and "&lt;b&gt;" not in ctx.direct_html


def test_termo_unico_nao_acha_o_que_tem_outro_nome() -> None:
    """A limitação, DOCUMENTADA e não escondida: a descrição do drone é "DJI
    Avata 2 Fly More" — a palavra "drone" não está nela. Por isso a spec da
    tool manda o modelo enviar sinônimos e marcas, e a resposta vazia diz que
    o nome pode ser outro."""
    so_drone = _buscar("drone")
    assert "DJI Avata 2" not in so_drone          # só o "Seguro do drone", do banco
    com_sinonimos = _buscar("drone", "avata", "dji")
    assert "DJI Avata 2" in com_sinonimos


def test_data_da_busca_traz_o_ANO() -> None:
    """Busca varre anos: "05/10" não responde "quando comprei?"."""
    out = _buscar("avata", "dji")
    assert "05/10/2025" in out, out


def test_tool_aceita_string_solta() -> None:
    """Modelo leve às vezes manda 'termos' como string em vez de lista."""
    from bot.services import tools
    from bot.services.llm.base import ToolContext

    ctx = ToolContext(user=SimpleNamespace(id=1, firebase_uid=UID),
                      session=None, tz="America/Sao_Paulo")
    asyncio.run(tools._h_buscar_lancamento({"termos": "avata"}, ctx))
    assert "Avata" in (ctx.direct_html or "")


def test_tool_registrada_e_com_spec_valido() -> None:
    from bot.services.tools import TOOLS

    t = next(t for t in TOOLS if t.name == "buscar_lancamento")
    assert t.parameters["required"] == ["termos"]
    # A descrição precisa DESVIAR do consultar_lancamentos, senão o modelo
    # segue despejando a fatura — que foi o bug.
    assert "consultar_lancamentos" in t.description
    assert "CONCLUÍDA" in t.description


# ──────────────────────────── help ────────────────────────────

@pytest.mark.parametrize("frase", [
    "eu nao tinha comprado um drone parcelado?",
    "quantas parcelas faltam do sofa?",
    "ja paguei o parcelamento do notebook?",
])
def test_help_roteia_para_o_financeiro(frase: str) -> None:
    from bot.handlers.start import HELP_TEXT, find_help_sections

    assert "procura pela descrição em TODO o histórico" in HELP_TEXT
    secoes = find_help_sections(frase)
    assert any("financeiro" in s.lower() for s in secoes), frase


def test_help_nao_roubou_a_lista_de_compras() -> None:
    """'comprar' pertence à lista de compras — a busca nova não pode
    sequestrar."""
    from bot.handlers.start import find_help_sections

    secoes = find_help_sections("preciso comprar arroz")
    assert any("Lista de compras" in s for s in secoes)


# ───────── invariante do help: exemplo documentado tem que rotear ─────────
# Medição de 13/09/2026: 14 dos 79 exemplos que o help ENSINA não achavam
# seção nenhuma — o bot respondia "não sei" pra frase que ele mesmo sugere.
# Dois eram meus, escritos no mesmo dia, apesar da regra do CLAUDE.md que
# manda verificar o matching. Regra que depende de lembrar não se sustenta;
# este teste faz a lacuna errar alto.

def _exemplos_do_help():
    """Frases entre <i>"..."</i> que um usuário digitaria LITERALMENTE.

    Fora: trechos truncados com "…" (não são frases, são recortes) e
    templates com placeholder solto ("procura X")."""
    import re as _re
    from bot.handlers.start import _HELP_SECTIONS

    for titulo, bloco in _HELP_SECTIONS:
        for a, b in _re.findall(r'<i>&quot;(.+?)&quot;</i>|<i>"(.+?)"</i>', bloco):
            ex = _re.sub(r"<[^>]+>", "", a or b)
            ex = ex.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
            if "…" in ex or _re.search(r"\b[A-Z]\b\s*$", ex):
                continue
            yield _re.sub(r"<[^>]+>", "", titulo), ex


def test_todo_exemplo_do_help_acha_alguma_secao() -> None:
    from bot.handlers.start import find_help_sections

    pares = list(_exemplos_do_help())
    assert len(pares) > 50, "o extrator de exemplos quebrou"
    orfaos = [(t, e) for t, e in pares if not find_help_sections(e)]
    assert not orfaos, (
        "o help ENSINA estas frases e o `ajuda` responde 'não sei' pra elas:\n"
        + "\n".join(f"  [{t}] {e!r}" for t, e in orfaos)
    )
