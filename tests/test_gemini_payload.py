"""Corpo enviado ao Gemini: nada de part vazio.

`{"text": ""}` é recusado com 400 INVALID_ARGUMENT ("Request contains an
invalid argument"), sem dizer QUAL argumento. Basta uma mensagem vazia no
histórico — resposta que veio em branco, turno só de tool call — pra derrubar
toda conversa seguinte, e o erro aponta pro modelo, não pra causa.
"""
from __future__ import annotations

from bot.services.llm.gemini_impl import _messages_to_contents, _to_genai_parts


def test_texto_vazio_nao_vira_part() -> None:
    assert _to_genai_parts("") == []
    assert _to_genai_parts("   \n ") == []


def test_texto_normal_vira_part() -> None:
    parts = _to_genai_parts("oi")
    assert len(parts) == 1 and parts[0].text == "oi"


def test_bloco_de_texto_vazio_e_descartado() -> None:
    parts = _to_genai_parts([
        {"type": "text", "text": ""},
        {"type": "text", "text": "vale"},
        {"type": "text", "text": "  "},
    ])
    assert [p.text for p in parts] == ["vale"]


def test_mensagem_vazia_sai_do_payload() -> None:
    """O caso real: assistente respondeu vazio numa rodada anterior e aquela
    linha ficou no histórico."""
    contents = _messages_to_contents([
        {"role": "user", "content": "oi"},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "tudo bem?"},
    ])
    assert [c.role for c in contents] == ["user", "user"]
    assert all(c.parts for c in contents), "Content sem parts dá o mesmo 400"


def test_papeis_seguem_o_contrato_do_gemini() -> None:
    """user/model — 'assistant' não existe na API do Gemini."""
    contents = _messages_to_contents([
        {"role": "user", "content": "oi"},
        {"role": "assistant", "content": "olá"},
    ])
    assert [c.role for c in contents] == ["user", "model"]


def test_imagem_sem_texto_continua_valendo() -> None:
    """O filtro de vazio não pode derrubar anexo: part de bytes não tem texto."""
    import base64
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 32).decode()
    parts = _to_genai_parts([
        {"type": "text", "text": ""},
        {"type": "image", "data": png, "media_type": "image/png"},
    ])
    assert len(parts) == 1 and parts[0].inline_data is not None
