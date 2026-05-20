from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from bot.config import settings
from bot.services.llm.base import Tool, ToolContext
from bot.services.reminders import (
    create_reminder,
    delete_reminder,
    list_pending,
)
from bot.services.tasks import (
    create_task,
    delete_task,
    list_open_tasks,
    mark_done,
)
from bot.services.traffic import USER_AGENT, TrafficError, fetch_traffic
from bot.services.weather import WeatherError, fetch_today_weather, format_weather_line

logger = logging.getLogger(__name__)


async def _h_criar_tarefa(args: dict, ctx: ToolContext) -> str:
    texto = (args.get("texto") or "").strip()
    if not texto:
        return "erro: parâmetro 'texto' vazio"
    t = await create_task(ctx.session, ctx.user.id, texto)
    return f"ok: tarefa #{t.id} criada: {t.text}"


async def _h_listar_tarefas(_args: dict, ctx: ToolContext) -> str:
    items = await list_open_tasks(ctx.session, ctx.user.id)
    if not items:
        return "ok: nenhuma tarefa pendente"
    return "ok: " + " | ".join(f"#{t.id} {t.text}" for t in items)


async def _h_concluir_tarefa(args: dict, ctx: ToolContext) -> str:
    tid = args.get("id")
    if not isinstance(tid, int):
        return "erro: parâmetro 'id' inválido"
    t = await mark_done(ctx.session, ctx.user.id, tid)
    if t is None:
        return f"erro: tarefa #{tid} não encontrada"
    return f"ok: tarefa #{tid} concluída"


async def _h_apagar_tarefa(args: dict, ctx: ToolContext) -> str:
    tid = args.get("id")
    if not isinstance(tid, int):
        return "erro: parâmetro 'id' inválido"
    t = await delete_task(ctx.session, ctx.user.id, tid)
    if t is None:
        return f"erro: tarefa #{tid} não encontrada"
    return f"ok: tarefa #{tid} apagada"


async def _h_criar_lembrete(args: dict, ctx: ToolContext) -> str:
    texto = (args.get("texto") or "").strip()
    quando_iso = (args.get("quando_iso") or "").strip()
    if not texto or not quando_iso:
        return "erro: parâmetros 'texto' e 'quando_iso' são obrigatórios"
    tz = ZoneInfo(ctx.tz)
    try:
        # Aceita 'YYYY-MM-DDTHH:MM' ou 'YYYY-MM-DD HH:MM' (com ou sem segundos).
        dt_local = datetime.fromisoformat(quando_iso.replace(" ", "T"))
    except ValueError:
        return (
            f"erro: 'quando_iso' inválido ({quando_iso!r}). "
            f"Use formato ISO local: 'YYYY-MM-DDTHH:MM'."
        )
    if dt_local.tzinfo is None:
        dt_local = dt_local.replace(tzinfo=tz)
    due_utc = dt_local.astimezone(timezone.utc)
    if due_utc <= datetime.now(timezone.utc):
        return f"erro: data/hora ({quando_iso}) já passou"
    rem = await create_reminder(ctx.session, ctx.user.id, texto, due_utc)
    local = due_utc.astimezone(tz)
    return (
        f"ok: lembrete #{rem.id} criado: {texto} "
        f"em {local.strftime('%d/%m %H:%M')}"
    )


async def _h_listar_lembretes(_args: dict, ctx: ToolContext) -> str:
    items = await list_pending(ctx.session, ctx.user.id)
    if not items:
        return "ok: nenhum lembrete pendente"
    tz = ZoneInfo(ctx.tz)
    return "ok: " + " | ".join(
        f"#{r.id} {r.due_at.astimezone(tz).strftime('%d/%m %H:%M')} {r.text}"
        for r in items
    )


async def _h_apagar_lembrete(args: dict, ctx: ToolContext) -> str:
    rid = args.get("id")
    if not isinstance(rid, int):
        return "erro: parâmetro 'id' inválido"
    rem = await delete_reminder(ctx.session, ctx.user.id, rid)
    if rem is None:
        return f"erro: lembrete #{rid} não encontrado (ou já enviado)"
    return f"ok: lembrete #{rid} apagado"


async def _h_consultar_clima(args: dict, ctx: ToolContext) -> str:
    coords = (args.get("coords") or "").strip() or settings.home_coords
    if not coords:
        return "erro: coords não fornecido e HOME_COORDS não configurado"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            w = await fetch_today_weather(client, coords, ctx.tz)
    except WeatherError as e:
        return f"erro: {e}"
    return "ok: " + format_weather_line(w)


async def _h_consultar_transito(args: dict, ctx: ToolContext) -> str:
    origem = (args.get("origem") or "").strip()
    destino = (args.get("destino") or "").strip()
    if not origem or not destino:
        return "erro: parâmetros 'origem' e 'destino' são obrigatórios"
    if not settings.google_maps_api_key:
        return "erro: GOOGLE_MAPS_API_KEY não configurada"
    key = settings.google_maps_api_key.get_secret_value()
    try:
        async with httpx.AsyncClient(
            timeout=15.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}
        ) as client:
            infos = await fetch_traffic(client, key, origem, destino, [], alternatives=False)
    except TrafficError as e:
        return f"erro: {e}"
    info = infos[0]
    return (
        f"ok: ~{info.duration_minutes} min agora (típico ~{info.typical_minutes} min), "
        f"{info.distance_km} km via {info.summary or 'rota padrão'}"
    )


TOOLS: list[Tool] = [
    Tool(
        name="criar_tarefa",
        description="Cria uma nova tarefa pendente para o usuário.",
        parameters={
            "type": "object",
            "properties": {"texto": {"type": "string", "description": "Descrição da tarefa"}},
            "required": ["texto"],
        },
        handler=_h_criar_tarefa,
    ),
    Tool(
        name="listar_tarefas",
        description="Lista todas as tarefas pendentes (não concluídas) do usuário.",
        parameters={"type": "object", "properties": {}},
        handler=_h_listar_tarefas,
    ),
    Tool(
        name="concluir_tarefa",
        description="Marca uma tarefa como concluída pelo id.",
        parameters={
            "type": "object",
            "properties": {"id": {"type": "integer", "description": "Id da tarefa"}},
            "required": ["id"],
        },
        handler=_h_concluir_tarefa,
    ),
    Tool(
        name="apagar_tarefa",
        description="Apaga permanentemente uma tarefa pendente pelo id.",
        parameters={
            "type": "object",
            "properties": {"id": {"type": "integer", "description": "Id da tarefa"}},
            "required": ["id"],
        },
        handler=_h_apagar_tarefa,
    ),
    Tool(
        name="criar_lembrete",
        description=(
            "Cria um lembrete com data/hora absoluta. Calcule a data ISO usando "
            "a 'Data/hora atual' do system prompt como referência. "
            "Atenção: 'às 16h' é hora absoluta (16:00 daquele dia); "
            "'em 16h' é duração relativa (16 horas a partir de agora) — não confunda."
        ),
        parameters={
            "type": "object",
            "properties": {
                "texto": {"type": "string", "description": "O que lembrar"},
                "quando_iso": {
                    "type": "string",
                    "description": (
                        "Data/hora local no formato ISO 'YYYY-MM-DDTHH:MM' "
                        "(timezone do usuário). Ex: '2026-05-21T16:00' para "
                        "amanhã às 16h se hoje é 2026-05-20."
                    ),
                },
            },
            "required": ["texto", "quando_iso"],
        },
        handler=_h_criar_lembrete,
    ),
    Tool(
        name="listar_lembretes",
        description="Lista os lembretes pendentes do usuário.",
        parameters={"type": "object", "properties": {}},
        handler=_h_listar_lembretes,
    ),
    Tool(
        name="apagar_lembrete",
        description="Apaga um lembrete pendente pelo id.",
        parameters={
            "type": "object",
            "properties": {"id": {"type": "integer", "description": "Id do lembrete"}},
            "required": ["id"],
        },
        handler=_h_apagar_lembrete,
    ),
    Tool(
        name="consultar_clima",
        description=(
            "Consulta previsão do tempo de hoje. Sem coords, usa HOME_COORDS."
        ),
        parameters={
            "type": "object",
            "properties": {
                "coords": {
                    "type": "string",
                    "description": "Coordenadas 'lat,lng' (opcional)",
                }
            },
        },
        handler=_h_consultar_clima,
    ),
    Tool(
        name="consultar_transito",
        description=(
            "Calcula tempo atual de viagem entre origem e destino. "
            "Origem/destino podem ser 'lat,lng' ou endereço."
        ),
        parameters={
            "type": "object",
            "properties": {
                "origem": {"type": "string"},
                "destino": {"type": "string"},
            },
            "required": ["origem", "destino"],
        },
        handler=_h_consultar_transito,
    ),
]


def get_tool(name: str) -> Tool | None:
    return next((t for t in TOOLS if t.name == name), None)
