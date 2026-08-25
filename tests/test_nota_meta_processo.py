"""A nota técnica fala DA MP, nunca do processo de produção dela.

Caso real (dono, 25/08/2026): a nota da MP 1.388/2026 saiu com "…sem que o
dossiê disponibilize, até o momento, cobertura jornalística ou dados
quantitativos…" — o dossiê de pesquisa é insumo interno do prompt; o leitor
não sabe (nem deve saber) que ele existe. A regra de prompt proíbe; o filtro
_filtrar_meta_processo é a rede determinística pra quando o modelo
desobedecer.
"""
from __future__ import annotations

import asyncio

from bot.services import dou_monitor
from bot.services.dou_monitor import _aparar_frase_meta, _filtrar_meta_processo

# A frase da nota entregue, verbatim (com o começo legítimo que a precedia).
_FRASE_DO_CASO = (
    "A medida amplia a subvenção ao setor — configurando reforço financeiro "
    "adicional a essa política setorial, sem que o dossiê disponibilize, até "
    "o momento, cobertura jornalística ou dados quantitativos sobre o mercado "
    "de combustíveis que tenham motivado especificamente este crédito."
)


def test_caso_da_mp_1388_corta_so_a_oracao_do_dossie() -> None:
    """O começo da frase era conteúdo legítimo — o corte preserva a cabeça e
    remove só a oração que menciona o aparato."""
    out = _aparar_frase_meta(_FRASE_DO_CASO)
    assert out == (
        "A medida amplia a subvenção ao setor — configurando reforço "
        "financeiro adicional a essa política setorial."
    )


def test_frase_inteiramente_meta_cai_e_vizinhas_ficam() -> None:
    nota = {
        "ementa": "Abre crédito extraordinário.",
        "p1_contexto": (
            "A MP destina R$ 10.000.000.000,00 (dez bilhões de reais) à ANP. "
            "O dossiê de pesquisa não traz cobertura jornalística sobre o tema. "
            "O crédito ampara-se no art. 167, § 3º, da Constituição Federal."
        ),
        "p2_dispositivos": "O art. 1º abre o crédito em favor do MME.",
    }
    out = _filtrar_meta_processo(nota)
    assert "dossiê" not in out["p1_contexto"]
    assert "cobertura" not in out["p1_contexto"]
    assert "R$ 10.000.000.000,00" in out["p1_contexto"]
    assert "art. 167, § 3º" in out["p1_contexto"]
    assert out["p2_dispositivos"] == "O art. 1º abre o crédito em favor do MME."


def test_paragrafo_limpo_sai_intacto_byte_a_byte() -> None:
    """Sem marcador o filtro não pode reescrever NADA — nem re-juntar frases
    (abreviações como 'art. 5º' e 'Lei nº 11.977' não podem virar quebra)."""
    limpo = (
        "O art. 5º, § 1º, da Lei nº 11.977, de 7 de julho de 2009, passa a "
        "viger com nova redação. Ficam destinados R$ 1.305.000.000,00 (um "
        "bilhão trezentos e cinco milhões de reais) ao FAR."
    )
    nota = {"p1_contexto": limpo, "p2_dispositivos": ""}
    assert _filtrar_meta_processo(nota)["p1_contexto"] == limpo


def test_marcadores_casam_com_e_sem_acento() -> None:
    assert _aparar_frase_meta("Não há cobertura jornalística disponível.") is None
    assert _aparar_frase_meta("Nao ha cobertura jornalistica disponivel.") is None
    assert _aparar_frase_meta("O DOSSIÊ indica valores relevantes.") is None


def test_generate_nota_tecnica_aplica_o_filtro(monkeypatch) -> None:
    """O filtro fica no ponto único (generate_nota_tecnica) pra valer pros
    dois providers — nota vazada por qualquer um chega limpa à entrega."""
    async def _fake(mp, *, model_override=None):
        return {"ementa": "Abre crédito.", "p1_contexto": _FRASE_DO_CASO,
                "p2_dispositivos": "O art. 1º abre o crédito."}

    monkeypatch.setattr(dou_monitor, "_gen_nota_anthropic", _fake)
    monkeypatch.setattr(dou_monitor.settings, "dou_mp_provider", "anthropic")
    nota = asyncio.run(dou_monitor.generate_nota_tecnica({"numero": "1388"}))
    assert "dossiê" not in nota["p1_contexto"]
    assert "política setorial." in nota["p1_contexto"]


def test_generate_nota_tecnica_none_passa_reto(monkeypatch) -> None:
    async def _fake(mp, *, model_override=None):
        return None

    monkeypatch.setattr(dou_monitor, "_gen_nota_anthropic", _fake)
    monkeypatch.setattr(dou_monitor.settings, "dou_mp_provider", "anthropic")
    assert asyncio.run(dou_monitor.generate_nota_tecnica({"numero": "1388"})) is None
