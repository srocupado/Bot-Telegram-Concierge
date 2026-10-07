"""Gemini: thinking por NÍVEL, sem thinking_budget e sem temperature.

E-mail do Google (07/10/2026): nos próximos modelos, thinking_budget dá 400
INVALID_ARGUMENT e temperature/top_p/top_k dão erro (sem efeito desde o
3.6 Flash). Trocar por thinking_level ("minimal", "low", "medium", "high")
ou omitir.

Medido na API real antes destes testes, com os modelos do dono:
- gemini-3.8-flash RECUSA "minimal" (400 "Thinking level MINIMAL is not
  supported for this model"); aceita low/medium/high;
- gemini-3.1-flash-lite aceita os quatro;
- thinking_budget=0 (o "desligar" antigo) no 3.8-flash pensava mesmo assim
  (358 tokens) — o Google remapeava pra um nível.

Garantias que vieram dos testes antigos de budget e continuam valendo: a
queda automática quando o modelo recusa, a escolha do usuário vencendo o
.env, e nenhum caminho chamando a API sem passar pelo `gerar`.
"""
from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest

import bot
from bot.services import dou_monitor, translator, voice
from bot.services.llm import gemini_impl as gi
from bot.services.llm.factory import get_provider_for_user


@pytest.fixture(autouse=True)
def _limpa():
    gi._NIVEL_SUBSTITUTO.clear()
    gi._SEM_AMOSTRAGEM.clear()
    yield
    gi._NIVEL_SUBSTITUTO.clear()
    gi._SEM_AMOSTRAGEM.clear()


class _Resp:
    text = "ok"


class _Cliente:
    """`aceita`: níveis que o modelo aceita (ex.: 3.8-flash sem "minimal").
    `erro_sempre`: 400 que não tem a ver com thinking (imagem, schema)."""

    def __init__(self, aceita=("minimal", "low", "medium", "high"),
                 erro_sempre=None, recusa_temperature=False):
        self.recusa_temperature = recusa_temperature
        self.aceita = {n.upper() for n in aceita}
        self.erro_sempre = erro_sempre
        self.pedidos: list[str | None] = []
        self.configs: list = []

    @property
    def models(self):
        return self

    def generate_content(self, *, model, contents, config):
        tc = config.thinking_config
        nivel = str(tc.thinking_level.value if hasattr(tc.thinking_level, "value")
                    else tc.thinking_level) if tc else None
        self.pedidos.append(nivel)
        self.configs.append(config)
        if self.erro_sempre:
            raise RuntimeError(self.erro_sempre)
        if self.recusa_temperature and config.temperature is not None:
            raise RuntimeError("400 INVALID_ARGUMENT. Request contains an invalid argument.")
        if nivel is not None and nivel not in self.aceita:
            raise RuntimeError("400 INVALID_ARGUMENT. Thinking level "
                               f"{nivel} is not supported for this model.")
        return _Resp()


# ───────────── o pedido ─────────────

def test_manda_thinking_level_e_nunca_thinking_budget() -> None:
    cli = _Cliente()
    gi.gerar(cli, "gemini-3.1-flash-lite", [], "chat", nivel="low")
    tc = cli.configs[0].thinking_config
    assert cli.pedidos == ["LOW"]
    assert tc.thinking_budget is None


def test_automatico_nao_manda_thinking_config() -> None:
    cli = _Cliente()
    gi.gerar(cli, "gemini-3.8-flash", [], "dou:pesquisa", nivel=None)
    assert cli.pedidos == [None]


# ───────────── queda automática ─────────────

def test_minimal_recusado_cai_pra_low_e_memoriza() -> None:
    """O caso medido: 3.8-flash recusa minimal."""
    cli = _Cliente(aceita=("low", "medium", "high"))
    assert gi.gerar(cli, "gemini-3.8-flash", [], "voice:stt", nivel="minimal").text == "ok"
    assert cli.pedidos == ["MINIMAL", "LOW"]
    gi.gerar(cli, "gemini-3.8-flash", [], "voice:stt", nivel="minimal")
    assert cli.pedidos[2:] == ["LOW"], "sem pagar a ida e volta dupla de novo"


def test_modelo_que_aceita_minimal_continua_com_minimal() -> None:
    """A queda num modelo não pode contaminar outro: thinking ligado por
    engano trunca a nota (JSON cortado)."""
    gi.gerar(_Cliente(aceita=("low",)), "gemini-3.8-flash", [], "x", nivel="minimal")
    cli = _Cliente()
    gi.gerar(cli, "gemini-3.1-flash-lite", [], "dou:nota", nivel="minimal")
    assert cli.pedidos == ["MINIMAL"]


def test_sem_nivel_aceito_cai_pro_padrao_do_modelo() -> None:
    cli = _Cliente(aceita=())
    gi.gerar(cli, "gemini-x", [], "tradutor", nivel="minimal")
    assert cli.pedidos == ["MINIMAL", "LOW", None]


def test_nivel_alto_recusado_cai_direto_pro_padrao() -> None:
    """3-pro-preview não tem "medium": não inventa outro nível, usa o padrão."""
    cli = _Cliente(aceita=("low", "high"))
    gi.gerar(cli, "gemini-3-pro-preview", [], "chat", nivel="medium")
    assert cli.pedidos == ["MEDIUM", None]


def test_400_que_nao_e_do_thinking_sobe_e_nao_envenena() -> None:
    cli = _Cliente(erro_sempre="400 INVALID_ARGUMENT. Unsupported image.")
    with pytest.raises(RuntimeError, match="image"):
        gi.gerar(cli, "gemini-3.8-flash", [], "chat", nivel="low")
    assert gi._NIVEL_SUBSTITUTO == {}


def test_erro_que_nao_e_invalid_argument_sobe_sem_retry() -> None:
    cli = _Cliente(erro_sempre="503 UNAVAILABLE")
    with pytest.raises(RuntimeError, match="503"):
        gi.gerar(cli, "gemini-3.8-flash", [], "chat", nivel="low")
    assert cli.pedidos == ["LOW"]


# ───────────── de onde vem o nível ─────────────

@pytest.mark.parametrize("budget,nivel", [
    (-1, None), (None, None), (0, "minimal"), (512, "low"), (2048, "low"),
    (4096, "medium"), (8192, "medium"), (24576, "high"),
])
def test_budget_antigo_vira_o_nivel_mais_proximo(budget, nivel) -> None:
    """Quem tinha GEMINI_THINKING_BUDGET ou /provider thinking 512 salvo não
    precisa reconfigurar nada."""
    assert gi._nivel_do_budget(budget) == nivel


def test_escolha_do_usuario_vence_o_env(monkeypatch) -> None:
    from bot.config import settings
    monkeypatch.setattr(settings, "gemini_thinking_level", "high")
    assert gi.nivel_efetivo("low") == "low"
    assert gi.nivel_efetivo("auto") is None
    assert gi.nivel_efetivo(None, 0) == "minimal", "budget antigo do usuário"
    assert gi.nivel_efetivo(None, None) == "high"


def test_env_vazio_usa_o_budget_antigo_do_env(monkeypatch) -> None:
    from bot.config import settings
    monkeypatch.setattr(settings, "gemini_thinking_level", "")
    monkeypatch.setattr(settings, "gemini_thinking_budget", 0)
    assert gi.nivel_efetivo() == "minimal"
    monkeypatch.setattr(settings, "gemini_thinking_budget", -1)
    assert gi.nivel_efetivo() is None


def _user(**kw):
    base = dict(provider="gemini", gemini_model="gemini-3.8-flash",
                anthropic_model=None, openai_model=None,
                gemini_thinking_budget=None, gemini_thinking_level=None)
    return SimpleNamespace(**{**base, **kw})


def test_provider_do_usuario_carrega_o_nivel(monkeypatch) -> None:
    from bot.config import settings
    monkeypatch.setattr(settings, "gemini_api_key", "k")
    a = get_provider_for_user(_user(gemini_thinking_level="low"))
    b = get_provider_for_user(_user(gemini_thinking_budget=24576))
    c = get_provider_for_user(_user(gemini_thinking_level="auto"))
    assert (a.thinking_level, b.thinking_level, c.thinking_level) == ("low", "high", None)


# ───────────── todos os caminhos ─────────────

@pytest.mark.parametrize("modulo", [dou_monitor, voice, translator])
def test_nenhum_caminho_chama_generate_content_direto(modulo) -> None:
    fonte = inspect.getsource(modulo)
    assert "client.models.generate_content(" not in fonte
    assert "gerar(" in fonte


@pytest.mark.parametrize("modulo", [dou_monitor, voice, translator])
def test_nota_voz_e_tradutor_pedem_o_minimo(modulo) -> None:
    assert 'nivel="minimal"' in inspect.getsource(modulo)


def test_nenhum_codigo_manda_thinking_budget_nem_top_p_top_k() -> None:
    """O que o Google vai recusar. temperature na voz/tradutor fica por ora
    (decisão pendente do dono: medido que ela ainda estabiliza o 3.1-lite)."""
    from pathlib import Path
    for arq in Path(bot.__path__[0]).rglob("*.py"):
        fonte = arq.read_text(encoding="utf-8")
        assert "ThinkingConfig(thinking_budget" not in fonte, arq
        assert "top_p=" not in fonte and "top_k=" not in fonte, arq


def test_helper_e_publico() -> None:
    assert hasattr(gi, "gerar") and not hasattr(gi, "_gerar")


# ───────────── /provider thinking ─────────────

class _Msg:
    def __init__(self):
        self.r: list[str] = []

    async def answer(self, t, **kw):
        self.r.append(t)


class _Sess:
    async def commit(self):
        pass


def _cmd(args, user):
    from bot.handlers import provider as ph
    m = _Msg()
    asyncio.run(ph.cmd_provider(m, SimpleNamespace(args=args), user, _Sess()))
    return m.r[-1]


def test_comando_aceita_nivel_e_limpa_o_budget_antigo() -> None:
    u = _user(gemini_thinking_budget=512)
    assert "baixo" in _cmd("thinking low", u)
    assert (u.gemini_thinking_level, u.gemini_thinking_budget) == ("low", None)
    assert "automático" in _cmd("thinking auto", u)
    assert u.gemini_thinking_level == "auto"
    _cmd("thinking padrao", u)
    assert (u.gemini_thinking_level, u.gemini_thinking_budget) == (None, None)


def test_comando_recusa_numero_de_tokens() -> None:
    u = _user()
    assert "descontinuado" in _cmd("thinking 512", u)
    assert u.gemini_thinking_level is None


# ───────────── temperature: mantida, com queda automática (opção b do dono) ─────────────

def test_temperature_continua_indo_pra_quem_aceita() -> None:
    """3.1-flash-lite: medido que ela ainda estabiliza a voz."""
    cli = _Cliente()
    gi.gerar(cli, "gemini-3.1-flash-lite", [], "voice:stt", nivel="minimal", temperature=0.0)
    assert len(cli.configs) == 1 and cli.configs[0].temperature == 0.0


def test_modelo_que_recusa_temperature_repete_sem_e_memoriza() -> None:
    """O modelo do futuro, como o e-mail anuncia."""
    cli = _Cliente(recusa_temperature=True)
    assert gi.gerar(cli, "gemini-futuro", [], "voice:stt", nivel="minimal",
                    temperature=0.0).text == "ok"
    assert [c.temperature for c in cli.configs] == [0.0, None]
    assert cli.pedidos == ["MINIMAL", "MINIMAL"], "o nível não pode cair à toa"
    assert "gemini-futuro" in gi._SEM_AMOSTRAGEM
    gi.gerar(cli, "gemini-futuro", [], "voice:stt", nivel="minimal", temperature=0.0)
    assert len(cli.configs) == 3, "da 2ª vez já vai sem, numa chamada só"


def test_400_de_thinking_mantem_a_temperature() -> None:
    """O caso real do 3.8-flash: recusa minimal, aceita temperature — que
    não pode ser descartada por engano."""
    cli = _Cliente(aceita=("low", "medium", "high"))
    gi.gerar(cli, "gemini-3.8-flash", [], "voice:stt", nivel="minimal", temperature=0.0)
    assert cli.pedidos == ["MINIMAL", "LOW"]
    assert [c.temperature for c in cli.configs] == [0.0, 0.0]
    assert gi._SEM_AMOSTRAGEM == set()


def test_recusa_os_dois_cai_nos_dois() -> None:
    cli = _Cliente(aceita=("low",), recusa_temperature=True)
    gi.gerar(cli, "gemini-futuro", [], "x", nivel="minimal", temperature=0.0)
    assert cli.pedidos[-1] == "LOW" and cli.configs[-1].temperature is None


def test_400_que_nao_era_da_temperature_nao_a_marca_como_culpada() -> None:
    cli = _Cliente(erro_sempre="400 INVALID_ARGUMENT. Unsupported image.")
    with pytest.raises(RuntimeError, match="image"):
        gi.gerar(cli, "gemini-3.1-flash-lite", [], "x", nivel="low", temperature=0.0)
    assert gi._SEM_AMOSTRAGEM == set()


def test_voz_e_tradutor_mandam_temperature() -> None:
    assert "temperature=0.0" in inspect.getsource(voice)
    assert "temperature=0.2" in inspect.getsource(translator._translate_gemini)
