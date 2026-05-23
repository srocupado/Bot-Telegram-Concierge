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
    is_valid_recurrence,
    list_pending,
)
from bot.services.tasks import (
    create_task,
    delete_task,
    list_open_tasks,
    mark_done,
)
from bot.services.traffic import USER_AGENT, TrafficError, fetch_traffic
from bot.services.user_facts import (
    delete_fact,
    get_fact,
    list_facts,
    upsert_fact,
)
from bot.services.financeiro import (
    FinanceiroError,
    NotConfiguredError,
    apagar_lancamento,
    consultar_lancamentos,
    lancar_despesa_cartao,
    lancar_movimento_banco,
    registrar_aporte_tesouro,
)
from bot.services.workouts import (
    CANONICAL_GROUPS,
    delete_workouts_on_date,
    format_summary,
    log_workout,
    normalize_groups,
    summary_current_week,
)
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
        + (f" [recorrente: {r.recurrence}]" if r.recurrence else "")
        for r in items
    )


async def _h_criar_lembrete_pagamento(args: dict, ctx: ToolContext) -> str:
    beneficiario = (args.get("beneficiario") or "").strip()
    valor = args.get("valor")
    vencimento_iso = (args.get("vencimento_iso") or "").strip()
    descricao = (args.get("descricao") or "").strip()
    if not beneficiario or valor is None or not vencimento_iso:
        return "erro: 'beneficiario', 'valor' e 'vencimento_iso' são obrigatórios"
    try:
        valor_f = float(valor)
    except (TypeError, ValueError):
        return "erro: 'valor' deve ser número (em reais)"

    tz = ZoneInfo(ctx.tz)
    try:
        dt_local = datetime.fromisoformat(vencimento_iso.replace(" ", "T"))
    except ValueError:
        return f"erro: 'vencimento_iso' inválido ({vencimento_iso!r}). Use 'YYYY-MM-DDTHH:MM' ou 'YYYY-MM-DD'."
    if dt_local.tzinfo is None:
        # Sem hora → assume 09:00 do dia de vencimento (lembrete matinal)
        if dt_local.hour == 0 and dt_local.minute == 0:
            dt_local = dt_local.replace(hour=9)
        dt_local = dt_local.replace(tzinfo=tz)
    due_utc = dt_local.astimezone(timezone.utc)
    if due_utc <= datetime.now(timezone.utc):
        return f"erro: vencimento ({vencimento_iso}) já passou"

    valor_fmt = f"R$ {valor_f:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    texto = f"💰 Pagar {beneficiario} — {valor_fmt}"
    if descricao:
        texto += f" ({descricao})"
    rem = await create_reminder(ctx.session, ctx.user.id, texto, due_utc)
    local = due_utc.astimezone(tz)
    return (
        f"ok: lembrete de pagamento #{rem.id} criado: "
        f"{beneficiario} {valor_fmt} em {local.strftime('%d/%m %H:%M')}"
    )


async def _h_criar_lembrete_recorrente(args: dict, ctx: ToolContext) -> str:
    texto = (args.get("texto") or "").strip()
    primeiro_iso = (args.get("primeiro_iso") or "").strip()
    recurrencia = (args.get("recurrencia") or "").strip().lower()
    if not texto or not primeiro_iso or not recurrencia:
        return "erro: 'texto', 'primeiro_iso' e 'recurrencia' são obrigatórios"
    if not is_valid_recurrence(recurrencia):
        return (
            "erro: recurrencia inválida. Use 'daily', 'weekday', 'weekend', "
            "'monthly' ou 'weekly:mon,wed,fri' (dias = mon|tue|wed|thu|fri|sat|sun "
            "ou seg|ter|qua|qui|sex|sab|dom)."
        )
    tz = ZoneInfo(ctx.tz)
    try:
        dt_local = datetime.fromisoformat(primeiro_iso.replace(" ", "T"))
    except ValueError:
        return f"erro: 'primeiro_iso' inválido ({primeiro_iso!r}). Use 'YYYY-MM-DDTHH:MM'."
    if dt_local.tzinfo is None:
        dt_local = dt_local.replace(tzinfo=tz)
    due_utc = dt_local.astimezone(timezone.utc)
    if due_utc <= datetime.now(timezone.utc):
        return f"erro: primeiro disparo ({primeiro_iso}) já passou"
    rem = await create_reminder(
        ctx.session, ctx.user.id, texto, due_utc,
        recurrence=recurrencia,
    )
    local = due_utc.astimezone(tz)
    return (
        f"ok: lembrete recorrente #{rem.id} criado: {texto} ({recurrencia}) "
        f"— primeiro: {local.strftime('%d/%m %H:%M')}"
    )


async def _h_apagar_lembrete(args: dict, ctx: ToolContext) -> str:
    rid = args.get("id")
    if not isinstance(rid, int):
        return "erro: parâmetro 'id' inválido"
    rem = await delete_reminder(ctx.session, ctx.user.id, rid)
    if rem is None:
        return f"erro: lembrete #{rid} não encontrado (ou já enviado)"
    return f"ok: lembrete #{rid} apagado"


async def _h_agendar_comando(args: dict, ctx: ToolContext) -> str:
    from bot.services.scheduled_actions import VALID_KINDS

    tipo = (args.get("tipo") or "").strip()
    parametros = (args.get("parametros") or "").strip()
    quando_iso = (args.get("quando_iso") or "").strip()
    if not tipo or not quando_iso:
        return "erro: 'tipo' e 'quando_iso' são obrigatórios"
    if tipo not in VALID_KINDS:
        return f"erro: tipo inválido. Use um de: {sorted(VALID_KINDS)}"
    if tipo == "chat" and not parametros:
        return "erro: para tipo='chat', 'parametros' deve conter o prompt a executar"

    tz = ZoneInfo(ctx.tz)
    try:
        dt_local = datetime.fromisoformat(quando_iso.replace(" ", "T"))
    except ValueError:
        return f"erro: 'quando_iso' inválido ({quando_iso!r}). Use 'YYYY-MM-DDTHH:MM'."
    if dt_local.tzinfo is None:
        dt_local = dt_local.replace(tzinfo=tz)
    due_utc = dt_local.astimezone(timezone.utc)
    if due_utc <= datetime.now(timezone.utc):
        return f"erro: data/hora ({quando_iso}) já passou"

    descricao_map = {
        "transito_casa": "trânsito → casa",
        "transito_trabalho": "trânsito → trabalho",
        "congresso": "pauta do congresso",
        "clima": "clima",
        "chat": parametros[:60] + ("…" if len(parametros) > 60 else ""),
    }
    texto = f"[agendado] {descricao_map.get(tipo, tipo)}"
    rem = await create_reminder(
        ctx.session, ctx.user.id, texto, due_utc,
        command_kind=tipo, command_args=parametros or None,
    )
    local = due_utc.astimezone(tz)
    return (
        f"ok: comando #{rem.id} agendado: {descricao_map.get(tipo, tipo)} "
        f"em {local.strftime('%d/%m %H:%M')}"
    )


async def _h_lembrar_fato(args: dict, ctx: ToolContext) -> str:
    chave = (args.get("chave") or "").strip()
    valor = (args.get("valor") or "").strip()
    if not chave or not valor:
        return "erro: 'chave' e 'valor' são obrigatórios"
    fact = await upsert_fact(ctx.session, ctx.user.id, chave, valor)
    return f"ok: fato '{fact.key}' salvo: {fact.value}"


async def _h_recuperar_fato(args: dict, ctx: ToolContext) -> str:
    chave = (args.get("chave") or "").strip()
    if not chave:
        return "erro: 'chave' obrigatória"
    fact = await get_fact(ctx.session, ctx.user.id, chave)
    if fact is None:
        return f"ok: nenhum fato salvo com chave '{chave}'"
    return f"ok: {fact.key} = {fact.value}"


async def _h_listar_fatos(_args: dict, ctx: ToolContext) -> str:
    items = await list_facts(ctx.session, ctx.user.id)
    if not items:
        return "ok: nenhum fato salvo"
    return "ok: " + " | ".join(f"{f.key}={f.value}" for f in items)


async def _h_esquecer_fato(args: dict, ctx: ToolContext) -> str:
    chave = (args.get("chave") or "").strip()
    if not chave:
        return "erro: 'chave' obrigatória"
    ok = await delete_fact(ctx.session, ctx.user.id, chave)
    if not ok:
        return f"erro: nenhum fato com chave '{chave}'"
    return f"ok: fato '{chave}' apagado"


async def _h_registrar_treino(args: dict, ctx: ToolContext) -> str:
    grupos_raw = args.get("grupos") or []
    if not isinstance(grupos_raw, list):
        return "erro: 'grupos' deve ser lista (ex: ['peito', 'cardio'])"
    grupos = normalize_groups(grupos_raw)
    if not grupos:
        return (
            f"erro: nenhum grupo canônico encontrado em {grupos_raw}. "
            f"Use {sorted(CANONICAL_GROUPS)}."
        )

    tz = ZoneInfo(ctx.tz)
    data_iso = (args.get("data_iso") or "").strip()
    if data_iso:
        try:
            workout_date = datetime.fromisoformat(data_iso.replace(" ", "T")).date()
        except ValueError:
            return f"erro: 'data_iso' inválido ({data_iso!r}). Use 'YYYY-MM-DD'."
    else:
        workout_date = datetime.now(tz).date()

    cardio_min = args.get("cardio_minutos")
    if cardio_min is not None:
        try:
            cardio_min = int(cardio_min)
        except (TypeError, ValueError):
            return "erro: 'cardio_minutos' deve ser inteiro"
    if "cardio" in grupos and cardio_min is None:
        return "erro: cardio mencionado mas cardio_minutos não fornecido"

    observacao = (args.get("observacao") or "").strip() or None
    try:
        log = await log_workout(
            ctx.session, ctx.user.id, workout_date, grupos,
            cardio_minutes=cardio_min, notes=observacao,
        )
    except ValueError as e:
        return f"erro: {e}"

    label = " + ".join(log.groups.split(","))
    if log.cardio_minutes:
        label += f" ({log.cardio_minutes}min cardio)"
    return f"ok: treino #{log.id} registrado em {log.date.strftime('%d/%m')} — {label}"


async def _h_consultar_treinos(_args: dict, ctx: ToolContext) -> str:
    summary = await summary_current_week(ctx.session, ctx.user.id, ctx.tz)
    return "ok: " + format_summary(summary)


async def _h_apagar_treino_dia(args: dict, ctx: ToolContext) -> str:
    tz = ZoneInfo(ctx.tz)
    data_iso = (args.get("data_iso") or "").strip()
    if data_iso:
        try:
            target = datetime.fromisoformat(data_iso.replace(" ", "T")).date()
        except ValueError:
            return f"erro: 'data_iso' inválido ({data_iso!r}). Use 'YYYY-MM-DD'."
    else:
        target = datetime.now(tz).date()
    n = await delete_workouts_on_date(ctx.session, ctx.user.id, target)
    if n == 0:
        return f"ok: nenhum treino registrado em {target.strftime('%d/%m')}"
    plural = "treino" if n == 1 else "treinos"
    return f"ok: {n} {plural} apagado(s) em {target.strftime('%d/%m')}"


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


def _resolve_data_iso(args: dict, tz_name: str) -> str:
    raw = (args.get("data_iso") or "").strip()
    if raw:
        try:
            return datetime.fromisoformat(raw.replace(" ", "T")).date().isoformat()
        except ValueError:
            return ""  # sinaliza erro pro chamador
    return datetime.now(ZoneInfo(tz_name)).date().isoformat()


async def _h_lancar_movimento_banco(args: dict, ctx: ToolContext) -> str:
    desc = (args.get("desc") or "").strip()
    valor = args.get("valor")
    tipo = (args.get("tipo") or "").strip()
    if not desc or valor is None or not tipo:
        return "erro: 'desc', 'valor' e 'tipo' são obrigatórios"
    try:
        valor_f = float(valor)
    except (TypeError, ValueError):
        return "erro: 'valor' deve ser número (em reais)"
    data_iso = _resolve_data_iso(args, ctx.tz)
    if not data_iso:
        return f"erro: 'data_iso' inválido ({args.get('data_iso')!r}). Use 'YYYY-MM-DD'."
    categoria = (args.get("categoria") or "outros").strip() or "outros"
    recorrente = bool(args.get("recorrente") or False)
    try:
        entry = await lancar_movimento_banco(
            ctx.session, ctx.user, desc, valor_f, tipo, data_iso,
            categoria=categoria, recorrente=recorrente,
        )
    except NotConfiguredError as e:
        return f"erro: {e}"
    except FinanceiroError as e:
        return f"erro: {e}"
    sign = "+" if entry["amount"] >= 0 else ""
    return (
        f"ok: lançamento {entry['id']} no banco — "
        f"{sign}R$ {entry['amount']:.2f} {entry['desc']} "
        f"em {entry['date']} [{entry['category']}]"
    )


async def _h_lancar_despesa_cartao(args: dict, ctx: ToolContext) -> str:
    desc = (args.get("desc") or "").strip()
    valor = args.get("valor")
    if not desc or valor is None:
        return "erro: 'desc' e 'valor' são obrigatórios"
    try:
        valor_f = float(valor)
    except (TypeError, ValueError):
        return "erro: 'valor' deve ser número (em reais)"
    data_iso = _resolve_data_iso(args, ctx.tz)
    if not data_iso:
        return f"erro: 'data_iso' inválido ({args.get('data_iso')!r}). Use 'YYYY-MM-DD'."
    categoria = (args.get("categoria") or "outros").strip() or "outros"
    parcelas = args.get("parcelas") or 1
    try:
        parcelas = int(parcelas)
    except (TypeError, ValueError):
        return "erro: 'parcelas' deve ser inteiro"
    try:
        entry = await lancar_despesa_cartao(
            ctx.session, ctx.user, desc, valor_f, data_iso,
            categoria=categoria, parcelas=parcelas,
        )
    except NotConfiguredError as e:
        return f"erro: {e}"
    except FinanceiroError as e:
        return f"erro: {e}"
    par_label = f" em {parcelas}x" if parcelas > 1 else ""
    return (
        f"ok: compra {entry['id']} no cartão — -R$ {entry['amount']:.2f} "
        f"{entry['desc']}{par_label} em {entry['date']} [{entry['category']}]"
    )


async def _h_registrar_aporte_tesouro(args: dict, ctx: ToolContext) -> str:
    titulo = (args.get("titulo") or "").strip()
    valor = args.get("valor")
    if not titulo or valor is None:
        return "erro: 'titulo' e 'valor' são obrigatórios"
    try:
        valor_f = float(valor)
    except (TypeError, ValueError):
        return "erro: 'valor' deve ser número"
    data_iso = _resolve_data_iso(args, ctx.tz)
    if not data_iso:
        return f"erro: 'data_iso' inválido ({args.get('data_iso')!r}). Use 'YYYY-MM-DD'."
    taxa = args.get("taxa")
    if taxa is not None:
        try:
            taxa = float(taxa)
        except (TypeError, ValueError):
            return "erro: 'taxa' deve ser número"
    try:
        res = await registrar_aporte_tesouro(
            ctx.session, ctx.user, titulo, valor_f, data_iso, taxa=taxa,
        )
    except NotConfiguredError as e:
        return f"erro: {e}"
    except FinanceiroError as e:
        return f"erro: {e}"
    taxa_label = f" @ {taxa}%" if taxa is not None else ""
    return (
        f"ok: aporte de R$ {valor_f:.2f}{taxa_label} no '{res['titulo']}' "
        f"em {data_iso}"
    )


async def _h_apagar_lancamento(args: dict, ctx: ToolContext) -> str:
    modulo = (args.get("modulo") or "").strip().lower()
    entry_id = (args.get("id") or "").strip()
    if not modulo or not entry_id:
        return "erro: 'modulo' e 'id' são obrigatórios"
    try:
        res = await apagar_lancamento(ctx.session, ctx.user, modulo, entry_id)
    except NotConfiguredError as e:
        return f"erro: {e}"
    except FinanceiroError as e:
        return f"erro: {e}"
    rem = res.get("removido") or res.get("contribution") or {}
    desc = rem.get("desc") or f"aporte em {res.get('titulo', '?')}"
    amt = float(rem.get("amount") or 0)
    date = rem.get("date", "?")
    return f"ok: removido {entry_id} ({res['modulo']}) — {desc} R$ {abs(amt):.2f} em {date}"


async def _h_consultar_lancamentos(args: dict, ctx: ToolContext) -> str:
    modulo = (args.get("modulo") or "tudo").strip().lower()
    dias = args.get("dias") or 30
    try:
        dias = int(dias)
    except (TypeError, ValueError):
        return "erro: 'dias' deve ser inteiro"
    today_iso = datetime.now(ZoneInfo(ctx.tz)).date().isoformat()
    try:
        out = await consultar_lancamentos(
            ctx.session, ctx.user, modulo, dias, today_iso,
        )
    except NotConfiguredError as e:
        return f"erro: {e}"
    except FinanceiroError as e:
        return f"erro: {e}"
    return "ok:\n" + out


async def _h_consultar_transito(args: dict, ctx: ToolContext) -> str:
    origem = (args.get("origem") or "").strip()
    destino = (args.get("destino") or "").strip()
    if not origem or not destino:
        return "erro: parâmetros 'origem' e 'destino' são obrigatórios"
    # Atalhos: 'casa' e 'trabalho' viram HOME_COORDS / WORK_COORDS.
    aliases = {"casa": settings.home_coords, "trabalho": settings.work_coords}
    origem_resolved = aliases.get(origem.lower(), origem)
    destino_resolved = aliases.get(destino.lower(), destino)
    if origem_resolved is None:
        return f"erro: 'casa'/'trabalho' usado em origem mas HOME_COORDS/WORK_COORDS não configurado"
    if destino_resolved is None:
        return f"erro: 'casa'/'trabalho' usado em destino mas HOME_COORDS/WORK_COORDS não configurado"
    if not settings.google_maps_api_key:
        return "erro: GOOGLE_MAPS_API_KEY não configurada"
    key = settings.google_maps_api_key.get_secret_value()
    try:
        async with httpx.AsyncClient(
            timeout=15.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}
        ) as client:
            infos = await fetch_traffic(
                client, key, origem_resolved, destino_resolved, [], alternatives=False,
            )
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
        name="criar_lembrete_pagamento",
        description=(
            "Cria lembrete específico de pagamento a partir de dados extraídos "
            "de uma foto/PDF de boleto, conta ou fatura. Use IMEDIATAMENTE após "
            "ver uma imagem de boleto/conta/fatura — extraia beneficiário, "
            "valor (em reais, como número) e vencimento (formato ISO local). "
            "Se a hora não vier no documento, deixe só a data ('YYYY-MM-DD') "
            "e a tool agenda às 09:00. Inclua descrição se útil "
            "(ex: 'energia 03/2026')."
        ),
        parameters={
            "type": "object",
            "properties": {
                "beneficiario": {
                    "type": "string",
                    "description": "Quem recebe (ex: 'Enel', 'Caesb', 'João Silva')",
                },
                "valor": {
                    "type": "number",
                    "description": "Valor em reais como número (ex: 245.67)",
                },
                "vencimento_iso": {
                    "type": "string",
                    "description": "Vencimento ISO local: 'YYYY-MM-DD' ou 'YYYY-MM-DDTHH:MM'",
                },
                "descricao": {
                    "type": "string",
                    "description": "Opcional — referência/mês/serviço",
                },
            },
            "required": ["beneficiario", "valor", "vencimento_iso"],
        },
        handler=_h_criar_lembrete_pagamento,
    ),
    Tool(
        name="criar_lembrete_recorrente",
        description=(
            "Cria um lembrete que se repete em intervalo regular. Use SEMPRE "
            "que o usuário disser 'todo dia', 'toda semana', 'segundas e "
            "quartas', 'dia útil', 'fim de semana', 'todo mês'. Recurrencias "
            "aceitas: 'daily', 'weekday' (seg-sex), 'weekend' (sab-dom), "
            "'monthly' (mesmo dia do mês), 'weekly:mon,wed,fri' (dias "
            "específicos em inglês ou pt: mon|tue|wed|thu|fri|sat|sun ou "
            "seg|ter|qua|qui|sex|sab|dom)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "texto": {"type": "string", "description": "O que lembrar"},
                "primeiro_iso": {
                    "type": "string",
                    "description": "Data/hora do primeiro disparo (ISO local 'YYYY-MM-DDTHH:MM')",
                },
                "recurrencia": {
                    "type": "string",
                    "description": "daily | weekday | weekend | monthly | weekly:<dias>",
                },
            },
            "required": ["texto", "primeiro_iso", "recurrencia"],
        },
        handler=_h_criar_lembrete_recorrente,
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
        name="agendar_comando",
        description=(
            "Agenda uma ação automática pra rodar em data/hora futura, "
            "sem precisar você mandar comando na hora. Aparece junto dos "
            "lembretes em /lembretes. Tipos suportados:\n"
            "- 'transito_casa' / 'transito_trabalho': dispara consulta de "
            "trânsito (rota trabalho→casa ou casa→trabalho).\n"
            "- 'congresso': dispara resumo da pauta do Congresso.\n"
            "- 'clima': consulta previsão do tempo (parametros='lat,lng' opcional, default HOME_COORDS).\n"
            "- 'chat': envia um prompt livre pro assistente como se o usuário "
            "tivesse digitado. Use parametros pro prompt completo. Ex: "
            "'me dê um resumo das notícias da semana e clima de hoje'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "tipo": {
                    "type": "string",
                    "enum": ["transito_casa", "transito_trabalho", "congresso", "clima", "chat"],
                },
                "parametros": {
                    "type": "string",
                    "description": (
                        "Args opcionais (coords pra clima; prompt pra chat). "
                        "Vazio pra transito/congresso."
                    ),
                },
                "quando_iso": {
                    "type": "string",
                    "description": "Data/hora local ISO 'YYYY-MM-DDTHH:MM'.",
                },
            },
            "required": ["tipo", "quando_iso"],
        },
        handler=_h_agendar_comando,
    ),
    Tool(
        name="lembrar_fato",
        description=(
            "Salva um fato persistente sobre o usuário (chave/valor). Use SEMPRE "
            "que o usuário disser algo sobre si próprio que queira que você lembre "
            "no futuro (preferências, nomes de família, alergias, ferramentas "
            "preferidas, etc). Sobrescreve se a chave já existir. Chaves "
            "minúsculas, snake_case curto. Ex: lembrar_fato("
            "chave='esposa_nome', valor='Dani'); lembrar_fato("
            "chave='alergia', valor='amendoim'); lembrar_fato("
            "chave='editor_preferido', valor='nvim')."
        ),
        parameters={
            "type": "object",
            "properties": {
                "chave": {"type": "string", "description": "Identificador curto (snake_case)"},
                "valor": {"type": "string", "description": "Conteúdo do fato"},
            },
            "required": ["chave", "valor"],
        },
        handler=_h_lembrar_fato,
    ),
    Tool(
        name="recuperar_fato",
        description="Lê um fato salvo pela chave exata.",
        parameters={
            "type": "object",
            "properties": {"chave": {"type": "string"}},
            "required": ["chave"],
        },
        handler=_h_recuperar_fato,
    ),
    Tool(
        name="listar_fatos",
        description=(
            "Lista TODOS os fatos salvos sobre o usuário. Use no início "
            "de conversas pra recuperar contexto sobre quem ele é."
        ),
        parameters={"type": "object", "properties": {}},
        handler=_h_listar_fatos,
    ),
    Tool(
        name="esquecer_fato",
        description="Remove um fato salvo pela chave.",
        parameters={
            "type": "object",
            "properties": {"chave": {"type": "string"}},
            "required": ["chave"],
        },
        handler=_h_esquecer_fato,
    ),
    Tool(
        name="registrar_treino",
        description=(
            "Registra um treino de academia. Categorias canônicas: 'peito', "
            "'costas', 'pernas', 'cardio'. Normalize variações: supino/voador/"
            "crossover → 'peito'; remada/puxada → 'costas'; agachamento/leg "
            "press/panturrilha → 'pernas'; corrida/esteira/bike → 'cardio'. "
            "Ombros/braços/abdomen NÃO entram (usuário não quer detalhar). "
            "Quando 'cardio' está em grupos, OBRIGATÓRIO informar "
            "cardio_minutos. Se usuário diz 'ontem' ou data específica, "
            "calcule data_iso usando a Data/hora atual do system prompt."
        ),
        parameters={
            "type": "object",
            "properties": {
                "grupos": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["peito", "costas", "pernas", "cardio"]},
                    "description": "Grupos canônicos treinados",
                },
                "cardio_minutos": {
                    "type": "integer",
                    "description": "Minutos de cardio (obrigatório se 'cardio' em grupos)",
                },
                "data_iso": {
                    "type": "string",
                    "description": "Data ISO 'YYYY-MM-DD' (default: hoje na timezone do usuário)",
                },
                "observacao": {
                    "type": "string",
                    "description": "Nota livre opcional sobre o treino",
                },
            },
            "required": ["grupos"],
        },
        handler=_h_registrar_treino,
    ),
    Tool(
        name="consultar_treinos",
        description=(
            "Retorna resumo da semana atual de academia (domingo → sábado): "
            "treinos por dia, totais por grupo e cardio acumulado. Histórico "
            "é descartado todo domingo, então só mostra a semana corrente. "
            "Use quando o usuário perguntar sobre rotina, malhação, semana "
            "de academia, quantos dias treinou, etc."
        ),
        parameters={"type": "object", "properties": {}},
        handler=_h_consultar_treinos,
    ),
    Tool(
        name="apagar_treino_dia",
        description=(
            "Apaga TODOS os registros de treino de um dia (corrige erro de "
            "lançamento). Use quando o usuário disser 'apaga o treino de "
            "hoje/ontem/X', 'errei o treino', 'na verdade não treinei isso'. "
            "Default: hoje. Pra dia específico, passe data_iso usando a "
            "Data/hora atual do system prompt como referência."
        ),
        parameters={
            "type": "object",
            "properties": {
                "data_iso": {
                    "type": "string",
                    "description": "Data ISO 'YYYY-MM-DD' (default: hoje)",
                },
            },
        },
        handler=_h_apagar_treino_dia,
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
            "Origem/destino podem ser 'lat,lng', endereço, ou os atalhos "
            "'casa'/'trabalho' (mapeiam pra HOME_COORDS/WORK_COORDS do servidor)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "origem": {
                    "type": "string",
                    "description": "Origem: 'casa', 'trabalho', 'lat,lng' ou endereço",
                },
                "destino": {
                    "type": "string",
                    "description": "Destino: 'casa', 'trabalho', 'lat,lng' ou endereço",
                },
            },
            "required": ["origem", "destino"],
        },
        handler=_h_consultar_transito,
    ),
    Tool(
        name="lancar_movimento_banco",
        description=(
            "Registra um lançamento na conta bancária do gerenciador-financeiro. "
            "Use quando o usuário falar de pagamento de conta, recebimento, "
            "transferência, depósito, débito automático etc. (que NÃO seja "
            "cartão de crédito). 'tipo': use 'credito' para entradas "
            "('recebi', 'salário', 'pix de fulano') e 'debito' para saídas "
            "('paguei', 'gastei', 'conta de luz'). Normalize valores em PT-BR "
            "(ex: 'R$ 1.250,50' → 1250.50, 'mil e duzentos' → 1200). "
            "Categoria default 'outros' se não inferir. Data: usa Data/hora "
            "atual pra resolver 'hoje', 'ontem', 'dia 15'. CHAME UMA VEZ "
            "por pedido — não duplique."
        ),
        parameters={
            "type": "object",
            "properties": {
                "desc": {"type": "string", "description": "Descrição curta (ex: 'Mercado', 'Salário')"},
                "valor": {"type": "number", "description": "Valor em reais (sempre positivo; tipo define o sinal)"},
                "tipo": {
                    "type": "string",
                    "enum": ["credito", "debito", "credit", "debit", "receita", "despesa"],
                    "description": "credito/receita = entrada; debito/despesa = saída",
                },
                "data_iso": {"type": "string", "description": "Data ISO 'YYYY-MM-DD' (default: hoje na tz do usuário)"},
                "categoria": {"type": "string", "description": "Id de categoria (default 'outros')"},
                "recorrente": {"type": "boolean", "description": "true se é despesa fixa mensal"},
            },
            "required": ["desc", "valor", "tipo"],
        },
        handler=_h_lancar_movimento_banco,
    ),
    Tool(
        name="lancar_despesa_cartao",
        description=(
            "Registra uma compra no cartão de crédito do gerenciador-"
            "financeiro. Use quando o usuário falar 'no cartão', 'cartão de "
            "crédito', 'parcelei', 'comprei no crédito'. Se for débito/PIX/"
            "boleto, use lancar_movimento_banco. Para parcelado, passe "
            "'parcelas' (ex: '10x de 200' → valor=200, parcelas=10 — o "
            "frontend cuida das próximas faturas). Data = data da compra "
            "(que vira mês da fatura). CHAME UMA VEZ por pedido."
        ),
        parameters={
            "type": "object",
            "properties": {
                "desc": {"type": "string"},
                "valor": {"type": "number", "description": "Valor da parcela (ou total à vista)"},
                "data_iso": {"type": "string", "description": "Data ISO 'YYYY-MM-DD' (default: hoje)"},
                "categoria": {"type": "string", "description": "Id de categoria (default 'outros')"},
                "parcelas": {"type": "integer", "description": "Número de parcelas (default 1)"},
            },
            "required": ["desc", "valor"],
        },
        handler=_h_lancar_despesa_cartao,
    ),
    Tool(
        name="registrar_aporte_tesouro",
        description=(
            "Registra um aporte (contribuição) em um título de Tesouro Direto "
            "já existente. NÃO cria título novo — se o usuário pedir aporte "
            "em algo não cadastrado, a tool retorna erro listando os títulos "
            "disponíveis; pra criar título precisa ser feito no app web. "
            "Use quando ouvir 'aportei X no Tesouro Y', 'comprei X de IPCA+', "
            "etc. 'titulo' pode ser nome parcial (match case-insensitive)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "titulo": {"type": "string", "description": "Nome (ou trecho) do título já cadastrado"},
                "valor": {"type": "number", "description": "Valor aportado em reais"},
                "data_iso": {"type": "string", "description": "Data ISO 'YYYY-MM-DD' (default: hoje)"},
                "taxa": {"type": "number", "description": "Taxa específica do aporte (% a.a.), se mencionada"},
            },
            "required": ["titulo", "valor"],
        },
        handler=_h_registrar_aporte_tesouro,
    ),
    Tool(
        name="consultar_lancamentos",
        description=(
            "Consulta lançamentos do gerenciador-financeiro nos últimos N "
            "dias. Use quando o usuário perguntar 'como tá meu cartão', "
            "'meus gastos da semana', 'quanto recebi esse mês', 'meus "
            "aportes no tesouro'. 'modulo' = 'banco' (conta), 'cartao' "
            "(crédito), 'tesouro' (títulos) ou 'tudo'. Default dias=30."
        ),
        parameters={
            "type": "object",
            "properties": {
                "modulo": {
                    "type": "string",
                    "enum": ["banco", "cartao", "tesouro", "tudo"],
                },
                "dias": {"type": "integer", "description": "Janela em dias (default 30)"},
            },
            "required": ["modulo"],
        },
        handler=_h_consultar_lancamentos,
    ),
    Tool(
        name="apagar_lancamento",
        description=(
            "Apaga UM lançamento do gerenciador-financeiro pelo id. Use "
            "SEMPRE que o usuário pedir pra apagar, remover, cancelar ou "
            "deletar um lançamento — NUNCA crie um lançamento espelho com "
            "valor oposto pra 'compensar', isso duplica registro em vez "
            "de apagar.\n"
            "Fluxo correto:\n"
            "  1) Se você não tem o id em mente, chame consultar_lancamentos "
            "primeiro pra ver os ids disponíveis (cada linha começa com "
            "[id]).\n"
            "  2) Confirme com o usuário qual lançamento apagar se houver "
            "ambiguidade (ex: dois 'Mercado R$ 100' no mesmo dia).\n"
            "  3) Chame apagar_lancamento(modulo=..., id=...).\n"
            "Módulos: 'banco' (bankTransactions), 'cartao' (cardEntries), "
            "'tesouro' (contribuição de Tesouro Direto)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "modulo": {
                    "type": "string",
                    "enum": ["banco", "cartao", "tesouro"],
                },
                "id": {
                    "type": "string",
                    "description": "Id do lançamento (7 chars; aparece entre [] em consultar_lancamentos)",
                },
            },
            "required": ["modulo", "id"],
        },
        handler=_h_apagar_lancamento,
    ),
]


def get_tool(name: str) -> Tool | None:
    return next((t for t in TOOLS if t.name == name), None)
