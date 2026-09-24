"""Setup e comandos manuais da retirada automática de ingresso na Sympla.

`/sympla_setup` captura e-mail, senha, nome completo e CPF em passos
sucessivos (texto simples — cada mensagem do dono é APAGADA do chat logo
após ser lida, pra não deixar senha/CPF sentados no histórico).

`/sympla_testar` roda o fluxo AGORA, fora da janela de quarta — útil pra
validar login/seleção de evento sem esperar a próxima liberação (fora da
janela real não deve haver evento publicado, então o desfecho esperado é
"não achei o evento", que já confirma login e busca funcionando).
"""
from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import User
from bot.utils import as_utc
from bot.services.sympla import (
    SymplaError,
    descrever_email,
    get_credenciais,
    retirar_ingresso,
    save_cpf,
    save_email,
    save_nome,
    save_senha,
)

logger = logging.getLogger(__name__)
router = Router(name="sympla")

_ETAPAS = ("email", "senha", "nome", "cpf")
AWAITING_WINDOW = timedelta(minutes=5)


def _is_owner(user: User) -> bool:
    """Credencial pessoal do dono (login dele na Sympla) — não faz sentido
    outro usuário configurar ou disparar em nome dele."""
    return not settings.owner_telegram_id or user.id == settings.owner_telegram_id


async def _apagar(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        pass  # sem permissão de apagar não é motivo pra falhar o setup


@router.message(Command("sympla_setup"))
async def cmd_setup(
    message: Message, command: CommandObject, user: User, session: AsyncSession,
) -> None:
    if not user.is_authorized:
        return
    if not _is_owner(user):
        await message.answer(
            "Essa credencial é pessoal (login SEU na Sympla) — só o dono do "
            "bot configura.", parse_mode=None,
        )
        return

    args = (command.args or "").strip()
    if not args:
        creds = await get_credenciais(session)
        if creds:
            await message.answer(
                f"✅ Configurado: <code>{descrever_email(creds.email)}</code>, "
                f"{creds.nome_completo}"
                + (f", CPF ***.{creds.cpf[-6:-2]}.{creds.cpf[-2:]}-**"
                   if creds.cpf and len(creds.cpf) == 11 else " (sem CPF)")
                + "\n\nPra trocar algo: <code>/sympla_setup email</code>, "
                "<code>senha</code>, <code>nome</code> ou <code>cpf</code>.",
                parse_mode="HTML",
            )
        else:
            await message.answer(
                "🔧 <b>Setup da retirada automática (Sympla)</b>\n\n"
                "Preciso de 4 coisas, uma por vez. Comece por:\n"
                "<code>/sympla_setup email</code>",
                parse_mode="HTML",
            )
        return

    etapa = args.split(maxsplit=1)[0].lower()
    if etapa not in _ETAPAS:
        await message.answer(
            f"Etapa inválida. Use: {', '.join(_ETAPAS)}.", parse_mode=None,
        )
        return

    user.awaiting_sympla_field = etapa
    user.awaiting_sympla_until = datetime.now(timezone.utc) + AWAITING_WINDOW
    await session.commit()

    proximo = {
        "email": "Manda o e-mail da sua conta Sympla.",
        "senha": "Manda a senha da sua conta Sympla.",
        "nome": "Manda seu NOME COMPLETO (como vai no ingresso).",
        "cpf": "Manda seu CPF (só números, ou com pontuação — tanto faz).",
    }[etapa]
    await message.answer(proximo, parse_mode=None)


@router.message(lambda m: bool(getattr(m, "text", None)))
async def capturar_campo(message: Message, user: User, session: AsyncSession) -> None:
    """Só age dentro da janela aberta por /sympla_setup <etapa>. Fora dela,
    deixa a mensagem seguir pro catch-all normal (chat livre)."""
    from aiogram.dispatcher.event.bases import SkipHandler

    etapa = getattr(user, "awaiting_sympla_field", None)
    if not etapa or not _is_owner(user):
        raise SkipHandler
    prazo = as_utc(getattr(user, "awaiting_sympla_until", None))
    if prazo is None or datetime.now(timezone.utc) > prazo:
        # Janela expirou: limpa o estado (evita reprocessar msgs futuras
        # numa data inconsistente) e deixa a mensagem seguir pro chat normal.
        user.awaiting_sympla_field = None
        await session.commit()
        raise SkipHandler

    texto = (message.text or "").strip()
    user.awaiting_sympla_field = None
    await session.commit()
    await _apagar(message)  # some do histórico ANTES de qualquer resposta

    try:
        if etapa == "email":
            await save_email(session, texto)
            ok = "E-mail salvo."
        elif etapa == "senha":
            await save_senha(session, texto)
            ok = "Senha salva."
        elif etapa == "nome":
            await save_nome(session, texto)
            ok = "Nome salvo."
        else:
            await save_cpf(session, texto)
            ok = "CPF salvo."
    except SymplaError as e:
        await message.answer(f"❌ {e} Tente <code>/sympla_setup {etapa}</code> de novo.",
                             parse_mode="HTML")
        return

    creds = await get_credenciais(session)
    faltando = []
    if not creds:
        faltando = [e for e in ("email", "senha", "nome") if e != etapa]
    if faltando:
        await message.answer(
            f"✅ {ok} Falta: {', '.join(faltando)}.\n"
            f"<code>/sympla_setup {faltando[0]}</code>", parse_mode="HTML",
        )
    else:
        await message.answer(
            f"✅ {ok} Setup completo — a retirada roda sozinha toda quarta "
            "às 17h55. Pra testar agora: <code>/sympla_testar</code>",
            parse_mode="HTML",
        )


@router.message(Command("sympla_testar"))
async def cmd_testar(message: Message, user: User, session: AsyncSession) -> None:
    if not user.is_authorized or not _is_owner(user):
        return
    creds = await get_credenciais(session)
    if creds is None:
        await message.answer(
            "Falta configurar — comece com <code>/sympla_setup email</code>.",
            parse_mode="HTML",
        )
        return

    cabecalho = (
        "🎫 Testando: login + UMA busca do evento (fora da janela de quarta, "
        "o esperado é 'não achei nenhum evento aberto' — isso já confirma "
        "login e busca). Leva de 1 a 2 minutos."
    )
    aviso = await message.answer(cabecalho, parse_mode=None)

    async def _progresso(etapa: str) -> None:
        await aviso.edit_text(f"{cabecalho}\n\n⏳ etapa: {etapa}…", parse_mode=None)

    # Nada aqui pode morrer calado: o 1º teste depois do login novo não
    # devolveu nada ao dono (causa não confirmada — sem log do Pi). Um
    # caminho certo de silêncio: o detalhe traz texto cru do Playwright
    # (ex.: "<input ...>") e ia como HTML sem escape; o Telegram recusa e a
    # exceção morria no handler.
    try:
        resultado = await retirar_ingresso(
            creds, settings.sympla_search_query, settings.sympla_qty,
            poll_timeout_s=0, on_etapa=_progresso,
        )
        texto = (
            f"{'✅' if resultado.sucesso else 'ℹ️'} <b>{html.escape(resultado.etapa)}</b>\n"
            f"{html.escape(resultado.detalhe[:3500])}"
        )
        if resultado.evento_url:
            texto += f"\n{html.escape(resultado.evento_url)}"
        await aviso.edit_text(texto, parse_mode="HTML")
        if resultado.screenshot:
            await message.answer_photo(
                BufferedInputFile(resultado.screenshot, filename="sympla.png"),
            )
    except Exception as exc:
        logger.exception("sympla_testar: falha ao rodar/reportar")
        await message.answer(
            f"❌ O teste quebrou antes de conseguir me reportar: "
            f"{type(exc).__name__}: {str(exc)[:1500]}",
            parse_mode=None,
        )
