"""Execução de ações agendadas via `Reminder.command_kind`.

Cada `run_*` recebe `bot` + chat_id + args opcional e envia o resultado
direto pra conversa. Reutiliza serviços existentes — não toca em
`Message` (que o scheduler não tem).
"""
from __future__ import annotations

import logging
from datetime import datetime

import httpx
from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import User
from bot.services.chat_memory import memory
from bot.services.congress import (
    USER_AGENT as CONGRESS_USER_AGENT,
    CongressScrapeError,
    fetch_week_mps,
    format_week_message,
)
from bot.services.llm.base import ToolContext
from bot.services.llm.factory import get_provider_for_user
from bot.services.tools import TOOLS
from bot.services.traffic import (
    USER_AGENT as TRAFFIC_USER_AGENT,
    TrafficError,
    fetch_traffic_with_alternative,
    format_traffic_message_dual,
    parse_route_waypoints,
)
from bot.services.weather import WeatherError, fetch_today_weather, format_weather_line

logger = logging.getLogger(__name__)


# Valores aceitos para o tipo de ação agendada.
VALID_KINDS = {
    "transito_casa", "transito_trabalho", "congresso", "clima", "chat",
    "agente", "shell",
}

# Tipos restritos ao dono do bot (executam código/comandos no container).
OWNER_KINDS = {"agente", "shell"}

# Timeout e cap de saída do tipo 'shell'.
SHELL_TIMEOUT_SECONDS = 300
_SHELL_OUTPUT_CAP = 3000

# Prefixo opcional do 'shell': só notifica se o comando falhar (exit != 0).
SHELL_QUIET_PREFIX = "@silencioso"


async def _send_html_with_fallback(bot: Bot, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        logger.exception("HTML send failed for chat %d; retrying as plain", chat_id)
        await bot.send_message(chat_id, text, parse_mode=None, disable_web_page_preview=True)


async def _run_transito(bot: Bot, chat_id: int, destino: str) -> None:
    if not (settings.home_coords and settings.work_coords and settings.google_maps_api_key):
        await bot.send_message(chat_id, "⚠️ Trânsito não configurado (HOME/WORK_COORDS, GOOGLE_MAPS_API_KEY).")
        return
    if destino == "casa":
        origin, destination, label, reverse = settings.work_coords, settings.home_coords, "trabalho → casa", True
    else:
        origin, destination, label, reverse = settings.home_coords, settings.work_coords, "casa → trabalho", False
    api_key = settings.google_maps_api_key.get_secret_value()
    try:
        async with httpx.AsyncClient(
            timeout=20.0, follow_redirects=True, headers={"User-Agent": TRAFFIC_USER_AGENT},
        ) as client:
            waypoints: list[str] = []
            if settings.route_google_maps_url:
                waypoints = await parse_route_waypoints(client, settings.route_google_maps_url)
                if reverse:
                    waypoints = list(reversed(waypoints))
            pref, alt = await fetch_traffic_with_alternative(
                client, api_key, origin, destination, waypoints,
                maps_url=settings.route_google_maps_url or "",
            )
    except TrafficError:
        logger.exception("scheduled transito fetch failed")
        await bot.send_message(chat_id, "⚠️ Não consegui calcular o trânsito agora.")
        return
    text = format_traffic_message_dual(pref, alt, label)
    await _send_html_with_fallback(bot, chat_id, f"⏰ <i>(agendado)</i>\n{text}")


async def _run_congresso(bot: Bot, chat_id: int) -> None:
    if not settings.congress_digest_enabled:
        await bot.send_message(chat_id, "⚠️ Congresso digest desativado (CONGRESS_DIGEST_ENABLED=false).")
        return
    try:
        async with httpx.AsyncClient(
            timeout=30.0, follow_redirects=True, headers={"User-Agent": CONGRESS_USER_AGENT},
        ) as client:
            items = await fetch_week_mps(client, datetime.now().date())
    except CongressScrapeError:
        logger.exception("scheduled congresso fetch failed")
        await bot.send_message(chat_id, "⚠️ Não consegui buscar a pauta agora.")
        return
    message = format_week_message(items, datetime.now().date())
    await _send_html_with_fallback(bot, chat_id, f"⏰ <i>(agendado)</i>\n{message}")


async def _run_clima(bot: Bot, chat_id: int, coords: str | None) -> None:
    target = (coords or "").strip() or settings.home_coords
    if not target:
        await bot.send_message(chat_id, "⚠️ Clima: coords não fornecidas e HOME_COORDS vazio.")
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            w = await fetch_today_weather(client, target)
    except WeatherError:
        logger.exception("scheduled clima fetch failed")
        await bot.send_message(chat_id, "⚠️ Não consegui consultar o clima agora.")
        return
    await bot.send_message(chat_id, f"⏰ (agendado)\n{format_weather_line(w)}")


async def _run_chat(
    bot: Bot, chat_id: int, user: User, session: AsyncSession, prompt: str,
) -> None:
    if not prompt:
        await bot.send_message(chat_id, "⚠️ Prompt agendado vazio.")
        return
    from bot.handlers.chat import _build_system_prompt, inject_context
    from bot.services.memoria import get_summary

    history = memory.get(chat_id)
    history.append({"role": "user", "content": prompt})
    summary = await get_summary(session, user.id)
    try:
        provider = get_provider_for_user(user)
        ctx = ToolContext(user=user, session=session, tz=user.timezone)
        reply = await provider.chat_with_tools(
            inject_context(history, user.timezone, summary), tools=TOOLS, ctx=ctx,
            system=_build_system_prompt(),
            max_tokens=800,
        )
    except Exception:
        logger.exception("scheduled chat failed")
        await bot.send_message(chat_id, f"⚠️ Erro ao executar prompt agendado: {prompt[:80]}")
        return
    memory.append(chat_id, "user", prompt)
    memory.append(chat_id, "assistant", reply)
    await bot.send_message(chat_id, f"⏰ <i>(agendado)</i>\n{reply}", parse_mode="HTML")


async def _run_agente(bot: Bot, chat_id: int, prompt: str) -> bool:
    """Dispara o agente de execução em background. False = ocupado (o
    scheduler mantém o reminder pendente e tenta de novo no próximo tick)."""
    # Import tardio: handlers.agent importa serviços; aqui é o caminho inverso.
    from bot.handlers.agent import start_background_task

    if not prompt:
        await bot.send_message(chat_id, "⚠️ Tarefa agendada do agente sem prompt.")
        return True
    status = start_background_task(prompt, chat_id, scheduled=True)
    if status == "busy":
        logger.info("scheduled agente adiado: runner ocupado")
        return False
    if status == "disabled":
        await bot.send_message(
            chat_id,
            "⚠️ Tarefa agendada do agente pulada: agente desabilitado "
            "(OWNER_TELEGRAM_ID/ANTHROPIC_API_KEY).",
        )
    elif status == "budget":
        # Teto diário de custo das execuções agendadas — aviso já foi
        # enviado pelo handler (1x/dia); só consome a ocorrência.
        logger.warning("scheduled agente pulado: teto diário de custo atingido")
    return True


async def _run_shell(bot: Bot, chat_id: int, command: str) -> None:
    """Executa um comando fixo no shell do container (sem LLM) e reporta.

    Comando é de autoria do owner (validado na criação) — roda sem
    restrição de path, mas com env mínimo (sem tokens do bot) e timeout.
    """
    import asyncio as _asyncio
    import os
    from pathlib import Path

    command = (command or "").strip()
    quiet = False
    if command.startswith(SHELL_QUIET_PREFIX):
        quiet = True
        command = command[len(SHELL_QUIET_PREFIX):].strip()
    if not command:
        await bot.send_message(chat_id, "⚠️ Comando shell agendado vazio.")
        return

    workspace = Path(settings.agent_workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/root"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    try:
        proc = await _asyncio.create_subprocess_shell(
            command,
            cwd=str(workspace),
            env=env,
            stdout=_asyncio.subprocess.PIPE,
            stderr=_asyncio.subprocess.STDOUT,
        )
        try:
            async with _asyncio.timeout(SHELL_TIMEOUT_SECONDS):
                out_bytes, _ = await proc.communicate()
        except TimeoutError:
            proc.kill()
            await bot.send_message(
                chat_id,
                f"⏰ ⚠️ shell agendado: timeout de {SHELL_TIMEOUT_SECONDS}s\n"
                f"$ {command[:200]}",
                parse_mode=None,
            )
            return
    except Exception as e:
        logger.exception("scheduled shell falhou ao iniciar")
        await bot.send_message(
            chat_id, f"⏰ ⚠️ shell agendado falhou ao iniciar: {e}", parse_mode=None,
        )
        return

    rc = proc.returncode
    if quiet and rc == 0:
        logger.info("scheduled shell ok (silencioso): %s", command[:120])
        return
    output = out_bytes.decode("utf-8", errors="replace").strip()
    if len(output) > _SHELL_OUTPUT_CAP:
        output = "…" + output[-_SHELL_OUTPUT_CAP:]
    status = "✅" if rc == 0 else f"⚠️ exit {rc}"
    text = f"⏰ shell agendado {status}\n$ {command[:200]}"
    if output:
        text += f"\n\n{output}"
    await bot.send_message(chat_id, text[:4000], parse_mode=None)


async def run_action(
    bot: Bot,
    session: AsyncSession,
    user: User,
    kind: str,
    args: str | None,
) -> bool:
    """Executa a ação. Retorna False quando NÃO consumiu a ocorrência (ex.:
    agente ocupado) — o scheduler tenta de novo no próximo tick."""
    if kind in OWNER_KINDS and user.id != settings.owner_telegram_id:
        # Defesa em profundidade: a criação já é owner-only.
        logger.warning("scheduled %s negado pra user %d (não-owner)", kind, user.id)
        return True
    if kind == "transito_casa":
        await _run_transito(bot, user.id, "casa")
    elif kind == "transito_trabalho":
        await _run_transito(bot, user.id, "trabalho")
    elif kind == "congresso":
        await _run_congresso(bot, user.id)
    elif kind == "clima":
        await _run_clima(bot, user.id, args)
    elif kind == "chat":
        await _run_chat(bot, user.id, user, session, args or "")
    elif kind == "agente":
        return await _run_agente(bot, user.id, args or "")
    elif kind == "shell":
        await _run_shell(bot, user.id, args or "")
    else:
        logger.warning("unknown command_kind=%r", kind)
        await bot.send_message(user.id, f"⚠️ Ação agendada desconhecida: {kind}")
    return True
