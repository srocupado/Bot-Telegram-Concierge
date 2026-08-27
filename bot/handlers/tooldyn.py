"""Tools dinâmicas — o bot desenvolve as próprias ferramentas (owner-only).

/tool_nova <pedido>     → agente escreve a candidata (workspace/tool_candidata/)
/tool_ativar <nome>     → valida em subprocesso + teste, mostra o código e
                          espera os botões ✅/❌ do dono
/tools_dinamicas        → lista as ativas
/tool_rm <nome>         → manda o .py de backup e remove

Guardrail central: tool dinâmica roda EM PROCESSO com os poderes do bot —
NADA ativa sem o dono ler o código e clicar em aprovar. Ver o contrato em
bot/services/tools_dinamicas.py.
"""
from __future__ import annotations

import html
import logging
import shutil
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.config import settings
from bot.db.models import User
from bot.services import tools_dinamicas as td

logger = logging.getLogger(__name__)

router = Router(name="tooldyn")


def _dir_candidatas() -> Path:
    return Path(settings.agent_workspace) / "tool_candidata"


def _e_owner(user_id: int) -> bool:
    return bool(settings.owner_telegram_id) and user_id == settings.owner_telegram_id


_SPEC_AGENTE = """Crie uma TOOL DINÂMICA para o bot Concierge que resolva: {pedido}

Escreva UM arquivo em tool_candidata/<nome>.py (crie a pasta se preciso; escolha
um nome curto [a-z0-9_]) seguindo EXATAMENTE este contrato:

    TOOL_NOME = "<mesmo nome do arquivo, sem .py>"
    TOOL_DESCRICAO = "quando o LLM do chat deve usá-la (>=20 chars, seja específico)"
    TOOL_PARAMETROS = {{"type": "object", "properties": {{...}}, "required": [...]}}
    async def executar(args: dict, ctx) -> str:
        ...

Regras do projeto (obrigatórias):
- `executar` devolve "ok: ..." ou "erro: ..." (o LLM lê isso). Resultado
  determinístico que o usuário deve ver EXATO (números, listas de fonte
  externa) vai em ctx.direct_html (com html.escape) e retorna "ok: enviado
  verbatim (não escreva nada)".
- Falha de fonte externa é DITA ("erro: não consegui consultar X"), nunca
  virar resposta vazia ou inventada. Timeouts explícitos em toda chamada de
  rede (httpx, já disponível).
- Só stdlib + libs já instaladas no bot (httpx, bs4, sqlalchemy, dateutil).
  NUNCA tocar em segredos/env, banco do bot ou arquivos fora do necessário.
- LATÊNCIA: a tool roda DENTRO do turno do chat — o dono fica esperando na
  tela. Alvo: poucos segundos. Trabalho paralelizável (varrer N hosts,
  consultar N itens) usa asyncio.gather com concorrência alta e timeout
  CURTO por item; nunca laço sequencial com timeout longo.
- REDE: o processo do bot ALCANÇA a rede local do dono — unicast TCP/UDP
  para a LAN funciona (medido: HTTP 200 do gateway 192.168.15.1). O que NÃO
  atravessa a bridge do Docker é multicast/broadcast (WS-Discovery e afins
  voltam vazios). Não presuma isolamento por causa do IP 172.x.
- Escreva também tool_candidata/test_<nome>.py (pytest, OFFLINE — rede
  mockada com monkeypatch) cobrindo o caminho feliz e uma falha, e RODE
  `python -m pytest tool_candidata/ -q` até passar.

Ao final, informe o nome escolhido e diga que a ativação é
`/tool_ativar <nome>`."""


@router.message(Command("tool_nova"))
async def cmd_tool_nova(message: Message, command: CommandObject, user: User) -> None:
    if not _e_owner(user.id):
        return
    pedido = (command.args or "").strip()
    if not pedido:
        await message.answer(
            "Uso: /tool_nova <o que a ferramenta deve fazer>\n"
            "Ex.: /tool_nova consultar a tabela FIPE de um carro pelo modelo",
            parse_mode=None,
        )
        return
    from bot.handlers.agent import start_background_task

    status = start_background_task(_SPEC_AGENTE.format(pedido=pedido), user.id)
    if status == "disabled":
        await message.answer("⚠️ Agente desabilitado (OWNER_TELEGRAM_ID/ANTHROPIC_API_KEY).",
                             parse_mode=None)
        return
    if status == "busy":
        await message.answer("⏳ O agente já está numa tarefa — tenta quando ele terminar.",
                             parse_mode=None)
        return
    await message.answer(
        "🛠️ Agente iniciado: ele escreve a ferramenta, testa offline e te "
        "avisa o nome. Depois é só /tool_ativar <nome> — eu valido de novo, "
        "te mostro o código e NADA ativa sem o seu botão de aprovação.",
        parse_mode=None,
    )


@router.message(Command("tool_ativar"))
async def cmd_tool_ativar(message: Message, command: CommandObject, user: User) -> None:
    """Wrapper que garante RESPOSTA. Falha inesperada aqui virava silêncio
    puro (o aiogram loga o traceback e o dono fica sem nada na tela) — foi
    exatamente o que aconteceu em 27/08/2026 com a legenda em Markdown."""
    if not _e_owner(user.id):
        return
    try:
        await _tool_ativar(message, command)
    except Exception as exc:
        logger.exception("/tool_ativar falhou")
        await message.answer(
            f"❌ Falha inesperada no /tool_ativar ({type(exc).__name__}: {exc}). "
            "NADA foi ativado. O log do container tem o traceback completo.",
            parse_mode=None,
        )


async def _tool_ativar(message: Message, command: CommandObject) -> None:
    nome = (command.args or "").strip().lower().removesuffix(".py")
    if not nome:
        await message.answer("Uso: /tool_ativar <nome>", parse_mode=None)
        return
    origem = _dir_candidatas() / f"{nome}.py"
    if not origem.is_file():
        await message.answer(
            f"Não achei tool_candidata/{nome}.py no workspace. O /tool_nova "
            "gera lá; confira o nome que o agente informou.", parse_mode=None,
        )
        return
    # Sinal IMEDIATO: validação + teste levam até ~2min e o silêncio parecia
    # comando morto (dono, 27/08/2026).
    await message.answer(
        f"🔎 Validando '{nome}' (import isolado + teste da candidata). "
        "Pode levar até 2 minutos…", parse_mode=None,
    )
    ok, detalhe = await td.validar_em_subprocesso(origem)
    if not ok:
        await message.answer(
            "❌ A candidata REPROVOU na validação (nada foi ativado):\n"
            f"{detalhe}\n\nPeça o conserto com /tool_nova de novo ou edite via /agente.",
            parse_mode=None,
        )
        return
    # Roda o teste da candidata, se o agente o escreveu (offline, subprocesso).
    teste = _dir_candidatas() / f"test_{nome}.py"
    aviso_teste = "⚠️ sem arquivo de teste (test_%s.py)" % nome
    if teste.is_file():
        import asyncio
        import sys
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pytest", str(teste), "-q",
            cwd=str(_dir_candidatas()),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
            saida = out_b.decode("utf-8", errors="replace")[-600:]
            if "No module named pytest" in saida:
                # Executor ausente ≠ teste reprovado: culpar o teste aqui
                # mandaria o dono caçar bug onde não há (o pytest sai da
                # imagem a cada rebuild se não estiver no Dockerfile).
                aviso_teste = ("⚠️ pytest ausente na imagem — teste NÃO "
                               "verificado")
                logger.warning("tool_ativar: pytest ausente; teste não rodou")
            elif proc.returncode != 0:
                await message.answer(
                    f"❌ O TESTE da candidata falhou (nada foi ativado):\n{saida}",
                    parse_mode=None,
                )
                return
            else:
                aviso_teste = "✅ teste passou"
        except asyncio.TimeoutError:
            proc.kill()
            await message.answer("❌ Teste não terminou em 120s — nada ativado.",
                                 parse_mode=None)
            return
    # Staging: congela a versão validada — o workspace pode mudar entre a
    # validação e o clique de aprovar.
    td.DIR_PENDENTES.mkdir(parents=True, exist_ok=True)
    pendente = td.DIR_PENDENTES / f"{nome}.py"
    shutil.copy2(origem, pendente)
    # parse_mode=None OBRIGATÓRIO: o bot usa Markdown por padrão e o nome da
    # tool contém underscore por contrato ([a-z0-9_]) — 'onvif_scan' abria um
    # itálico que nunca fechava, o Telegram recusava a mensagem inteira e o
    # handler morria ANTES dos botões (bug real do dono, 27/08/2026).
    await message.answer_document(
        FSInputFile(pendente, filename=f"{nome}.py"),
        caption=(f"🔎 Código da tool '{nome}' ({aviso_teste}). LEIA antes de "
                 "aprovar: ela roda com os poderes do bot."),
        parse_mode=None,
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Aprovar e ativar", callback_data=f"dyn:ok:{nome}"),
        InlineKeyboardButton(text="❌ Descartar", callback_data=f"dyn:no:{nome}"),
    ]])
    await message.answer(
        f"Ativo a tool '{nome}'? Ela entra no catálogo do chat na próxima "
        "mensagem e sobrevive a deploys (fica em /app/data).",
        reply_markup=kb, parse_mode=None,
    )


@router.callback_query(F.data.startswith("dyn:"))
async def cb_tool_dinamica(query, user: User) -> None:
    if not _e_owner(query.from_user.id):
        await query.answer("recurso do dono do bot", show_alert=True)
        return
    try:
        _, acao, nome = query.data.split(":", 2)
    except ValueError:
        await query.answer("botão inválido", show_alert=True)
        return
    pendente = td.DIR_PENDENTES / f"{nome}.py"
    if acao == "no":
        pendente.unlink(missing_ok=True)
        await query.answer("Descartada")
        try:
            await query.message.edit_text(f"❌ Tool '{nome}' descartada.",
                                          parse_mode=None)
        except Exception:
            pass
        return
    if not pendente.is_file():
        await query.answer("Pendente sumiu (bot reiniciou?) — rode /tool_ativar de novo.",
                           show_alert=True)
        return
    erro = td.ativar(nome, pendente)
    if erro:
        await query.answer()
        await query.message.answer(
            f"❌ Falha ao ativar '{nome}': {erro}", parse_mode=None)
        return
    pendente.unlink(missing_ok=True)
    await query.answer("Ativada")
    try:
        await query.message.edit_text(
            f"✅ Tool '{nome}' ATIVA — já vale na próxima mensagem do chat. "
            f"Veja com /tools_dinamicas; remova com /tool_rm {nome}.",
            parse_mode=None)
    except Exception:
        pass


@router.message(Command("tools_dinamicas"))
async def cmd_tools_dinamicas(message: Message, user: User) -> None:
    if not _e_owner(user.id):
        return
    tools = td.ativas()
    if not tools:
        await message.answer(
            "Nenhuma tool dinâmica ativa. Crie com /tool_nova <o que ela faz>.",
            parse_mode=None,
        )
        return
    linhas = ["🧩 Tools dinâmicas ativas:"]
    for t in tools:
        primeira = (t.description or "").strip().splitlines()[0][:90]
        linhas.append(f"• {t.name} — {primeira}")
    linhas.append("\nRemover: /tool_rm <nome>")
    await message.answer("\n".join(linhas), parse_mode=None)


@router.message(Command("tool_rm"))
async def cmd_tool_rm(message: Message, command: CommandObject, user: User) -> None:
    if not _e_owner(user.id):
        return
    nome = (command.args or "").strip().lower().removesuffix(".py")
    if not nome:
        await message.answer("Uso: /tool_rm <nome> (veja /tools_dinamicas)", parse_mode=None)
        return
    path = td.remover(nome)
    if path is None:
        await message.answer(f"Não existe tool dinâmica '{nome}'.", parse_mode=None)
        return
    # Backup ANTES de apagar: remoção reversível (é só reativar o arquivo).
    try:
        await message.answer_document(
            FSInputFile(path, filename=f"{nome}.py"),
            caption=f"Backup da tool '{nome}' (removida).",
            parse_mode=None,
        )
    except Exception:
        logger.exception("tool_rm: backup não enviado — removendo mesmo assim")
        await message.answer(
            f"⚠️ Não consegui enviar o backup de {nome}.py; removendo assim mesmo.",
            parse_mode=None,
        )
    path.unlink(missing_ok=True)
    await message.answer(f"🗑️ Tool '{nome}' removida — sai do catálogo na próxima mensagem.",
                         parse_mode=None)
