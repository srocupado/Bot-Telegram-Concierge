"""OpenRouter como quarto provider do chat (pedido do dono, 26/09/2026).

Validado ao vivo antes destes testes (chave do dono): 8 modelos de 8
fornecedores passaram pelo laço de tools do bot; visão leu texto de print
real; /provider listou, filtrou, aceitou id válido e recusou inválido.
Aqui ficam as garantias offline.
"""
from __future__ import annotations

import asyncio
import types

import pytest

from bot.config import Settings, settings
from bot.handlers import provider as ph
from bot.services.llm import catalog
from bot.services.llm.factory import SUPPORTED_PROVIDERS, get_provider, get_provider_for_user
from bot.services.llm.openai_impl import (
    OPENROUTER_BASE_URL, OpenAIProvider, OpenRouterProvider,
)


@pytest.fixture(autouse=True)
def _chave(monkeypatch):
    monkeypatch.setattr(settings, "openrouter_api_key", "sk-or-teste")


def _user(**kw):
    base = dict(provider="openrouter", gemini_model=None, anthropic_model=None,
                openai_model=None, openrouter_model=None,
                gemini_thinking_budget=None, vision_provider=None,
                is_authorized=True)
    return types.SimpleNamespace(**{**base, **kw})


def test_factory_monta_openrouter_com_o_modelo_do_usuario() -> None:
    p = get_provider_for_user(_user(openrouter_model="qwen/qwen3.8-flash"))
    assert isinstance(p, OpenRouterProvider)
    assert p.model == "qwen/qwen3.8-flash"
    assert str(p.client.base_url).rstrip("/") == OPENROUTER_BASE_URL


def test_sem_override_usa_o_modelo_do_env() -> None:
    p = get_provider("openrouter")
    assert p.model == settings.openrouter_model


def test_sem_chave_falha_com_mensagem_clara(monkeypatch) -> None:
    monkeypatch.setattr(settings, "openrouter_api_key", None)
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        get_provider("openrouter", openrouter_model="x/y-sem-cache")


def test_openai_direto_nao_mudou() -> None:
    assert OpenRouterProvider.__mro__[1] is OpenAIProvider
    assert "openrouter" in SUPPORTED_PROVIDERS


def test_piso_de_tokens_pra_modelo_que_raciocina() -> None:
    """Medido: qwen3.8-max-prime gastou 908 de 1024 (897 raciocinando)."""
    p = OpenRouterProvider("k", "qwen/qwen3.8-max-prime")
    visto = {}

    def _create(**kw):
        visto.update(kw)
        return "ok"
    p.client = types.SimpleNamespace(chat=types.SimpleNamespace(
        completions=types.SimpleNamespace(create=_create)))
    p._create(max_tokens=1024, model=p.model, messages=[])
    assert visto["max_tokens"] >= 4096
    assert "max_completion_tokens" not in visto


def test_nota_tecnica_nao_aceita_openrouter() -> None:
    """A nota usa recursos próprios de cada provedor; DOU_MP_PROVIDER
    errado no .env tem que falhar no boot, não mandar a nota pro caminho
    do Claude com um id do OpenRouter."""
    with pytest.raises(Exception):
        Settings(BOT_TOKEN="t", ACCESS_PASSWORD="p", DOU_MP_PROVIDER="openrouter")
    ok = Settings(BOT_TOKEN="t", ACCESS_PASSWORD="p", AI_PROVIDER="openrouter",
                  VISION_PROVIDER="openrouter")
    assert ok.ai_provider == "openrouter"


# ───────────── catálogo ─────────────

_CATALOGO = {"data": [
    {"id": "deepseek/deepseek-v4.1-flash", "name": "DeepSeek Flash",
     "supported_parameters": ["tools", "max_tokens"],
     "architecture": {"input_modalities": ["text"]}},
    {"id": "deepseek/deepseek-v4.1-flash:batch", "name": "DeepSeek Flash (batch)",
     "supported_parameters": ["tools"], "architecture": {"input_modalities": ["text"]}},
    {"id": "google/gemini-3.8-flash", "name": "Gemini Flash",
     "supported_parameters": ["tools"],
     "architecture": {"input_modalities": ["text", "image"]}},
    {"id": "alguem/sem-tools", "name": "Sem tools",
     "supported_parameters": ["max_tokens"], "architecture": {"input_modalities": ["text"]}},
]}


def _catalogo_falso(monkeypatch, dados=_CATALOGO):
    class _R:
        def raise_for_status(self):
            pass

        def json(self):
            return dados

    class _C:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            assert url == "https://openrouter.ai/api/v1/models"
            return _R()
    monkeypatch.setattr(catalog.httpx, "AsyncClient", _C)


def test_catalogo_so_lista_quem_usa_tools_e_tira_batch(monkeypatch) -> None:
    _catalogo_falso(monkeypatch)
    ids = [m for m, _ in asyncio.run(catalog.list_models("openrouter"))]
    assert ids == ["deepseek/deepseek-v4.1-flash", "google/gemini-3.8-flash"]


def test_catalogo_de_visao_exige_imagem_na_entrada(monkeypatch) -> None:
    _catalogo_falso(monkeypatch)
    ids = [m for m, _ in asyncio.run(catalog.list_models("openrouter", "vision"))]
    assert ids == ["google/gemini-3.8-flash"]


def test_lista_grande_vira_resumo_que_cabe_na_mensagem(monkeypatch) -> None:
    muitos = {"data": [
        {"id": f"forn{i % 7}/modelo-{i}", "name": f"M{i}",
         "supported_parameters": ["tools"], "architecture": {"input_modalities": ["text"]}}
        for i in range(400)]}
    _catalogo_falso(monkeypatch, muitos)
    txt = asyncio.run(ph._format_model_list("openrouter"))
    assert len(txt) < 4096
    assert "400" in txt and "forn0" in txt
    # "modelo" casa os 400: com filtro não vira resumo, é cortado com aviso.
    filtrado = asyncio.run(ph._format_model_list("openrouter", filtro="modelo"))
    assert len(filtrado) < 4096 and "e mais 340" in filtrado


# ───────────── /provider openrouter ─────────────

class _Msg:
    def __init__(self):
        self.r: list[str] = []

    async def answer(self, t, **kw):
        self.r.append(t)


class _Sess:
    async def commit(self):
        pass


def _cmd(args, user):
    m = _Msg()
    asyncio.run(ph.cmd_provider(m, types.SimpleNamespace(args=args), user, _Sess()))
    return m.r[-1]


def test_escolhe_modelo_valido(monkeypatch) -> None:
    _catalogo_falso(monkeypatch)
    u = _user(provider="gemini")
    assert "openrouter (deepseek/deepseek-v4.1-flash)" in _cmd(
        "openrouter deepseek/deepseek-v4.1-flash", u)
    assert u.provider == "openrouter"
    assert u.openrouter_model == "deepseek/deepseek-v4.1-flash"


def test_recusa_modelo_sem_tools_e_nao_troca_o_provider(monkeypatch) -> None:
    _catalogo_falso(monkeypatch)
    u = _user(provider="gemini")
    assert "aceitam ferramentas" in _cmd("openrouter alguem/sem-tools", u)
    assert u.provider == "gemini" and u.openrouter_model is None


def test_id_sem_fornecedor_explica_o_formato(monkeypatch) -> None:
    u = _user(provider="gemini")
    assert "fornecedor na frente" in _cmd("openrouter deepseek", u)
    assert u.provider == "gemini"


def test_memoria_aceita_modelo_do_openrouter(monkeypatch) -> None:
    from bot.services import memoria
    monkeypatch.setattr(settings, "memory_summary_model", "openrouter:z-ai/glm-5.3-flash")
    p = memoria._provider_do_resumo(_user())
    assert isinstance(p, OpenRouterProvider) and p.model == "z-ai/glm-5.3-flash"
