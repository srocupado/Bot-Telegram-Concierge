"""Modo viagem: /viagem <destino> DD/MM a DD/MM [moeda <nome>] · /viagem off.

Durante o período: clima do briefing = destino, fuso efetivo = local (lembretes
e janelas do proativo), moeda local opcional no briefing. Auto-desliga no fim.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import User
from bot.services.viagem import parse_periodo, resolver_destino, viagem_ativa

logger = logging.getLogger(__name__)
router = Router(name="viagem")

_OFF = ("off", "fim", "encerrar", "cancelar", "desligar")


def _fmt(iso: str) -> str:
    try:
        return date.fromisoformat(iso).strftime("%d/%m/%Y")
    except ValueError:
        return iso


@router.message(Command("viagem"))
async def cmd_viagem(
    message: Message, command: CommandObject, user: User, session: AsyncSession,
) -> None:
    if not user.is_authorized:
        return
    arg = (command.args or "").strip()

    if not arg:  # status
        if user.viagem_destino and user.viagem_fim:
            estado = "ATIVA ✈️" if viagem_ativa(user) else "agendada 🗓️"
            linhas = [
                f"Viagem {estado}: <b>{user.viagem_destino}</b>",
                f"Período: {_fmt(user.viagem_inicio)} → {_fmt(user.viagem_fim)}",
            ]
            if user.viagem_tz:
                linhas.append(f"Fuso: {user.viagem_tz}")
            if user.viagem_moeda:
                linhas.append(f"Moeda no briefing: {user.viagem_moeda}")
            linhas.append("\nDurante a viagem: clima do destino no briefing, "
                          "lembretes e janelas no fuso local. /viagem off encerra.")
            await message.answer("\n".join(linhas), parse_mode="HTML")
        else:
            await message.answer(
                "Sem viagem configurada.\n"
                "Use: /viagem <destino> DD/MM a DD/MM [moeda <nome>]\n"
                "Ex.: /viagem Fortaleza 22/08 a 27/08\n"
                "     /viagem Tóquio 01/11 a 15/11 moeda iene",
                parse_mode=None,
            )
        return

    if arg.lower() in _OFF:
        user.viagem_destino = None
        user.viagem_inicio = None
        user.viagem_fim = None
        user.viagem_tz = None
        user.viagem_coords = None
        user.viagem_moeda = None
        await session.commit()
        await message.answer("✈️ Modo viagem desligado — tudo de volta ao normal.", parse_mode=None)
        return

    hoje = datetime.now(ZoneInfo(user.timezone)).date()
    parsed = parse_periodo(arg, hoje)
    if parsed is None:
        await message.answer(
            "Não achei o período. Formato: /viagem <destino> DD/MM a DD/MM "
            "(ex.: /viagem Fortaleza 22/08 a 27/08)",
            parse_mode=None,
        )
        return
    ini, fim, resto = parsed

    # moeda opcional: "... moeda iene"
    moeda = None
    tokens = resto.split()
    if "moeda" in [t.lower() for t in tokens]:
        i = [t.lower() for t in tokens].index("moeda")
        if i + 1 < len(tokens):
            moeda = tokens[i + 1].strip().lower()
        tokens = tokens[:i] + tokens[i + 2:]
    destino = " ".join(tokens).strip(" ,-—")
    if not destino:
        await message.answer("Faltou o destino (ex.: /viagem Fortaleza 22/08 a 27/08).", parse_mode=None)
        return

    await message.answer(f"🔎 Configurando a viagem pra {destino}…", parse_mode=None)
    coords, tz, endereco = await resolver_destino(destino)

    user.viagem_destino = destino[:64]
    user.viagem_inicio = ini.isoformat()
    user.viagem_fim = fim.isoformat()
    user.viagem_coords = coords
    user.viagem_tz = tz
    user.viagem_moeda = moeda
    await session.commit()

    linhas = [f"✈️ Viagem configurada: <b>{destino}</b>",
              f"Período: {ini.strftime('%d/%m/%Y')} → {fim.strftime('%d/%m/%Y')}"]
    if endereco and endereco.lower() != destino.lower():
        linhas.append(f"Local: {endereco}")
    if tz and tz != user.timezone:
        linhas.append(f"Fuso local: {tz} — lembretes e briefing passam a valer "
                      "no horário do destino durante a viagem.")
    elif tz:
        linhas.append("Mesmo fuso de casa — nada muda nos horários.")
    else:
        linhas.append("⚠️ Não consegui resolver o fuso/coords do destino "
                      "(sem chave do Maps ou API fora) — clima e fuso seguem os de casa.")
    if coords:
        linhas.append("Clima do briefing: do destino, durante o período.")
    if moeda:
        linhas.append(f"Briefing inclui a cotação: {moeda}.")
    linhas.append("\n/viagem off desliga antes da hora; no fim do período desliga sozinho.")
    await message.answer("\n".join(linhas), parse_mode="HTML")
