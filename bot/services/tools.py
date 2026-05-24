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
from bot.services.actions import record_action, undo_last
from bot.services.financeiro import (
    FinanceiroError,
    NotConfiguredError,
    analisar_gastos,
    apagar_lancamento,
    consultar_lancamentos,
    lancar_despesa_cartao,
    lancar_movimento_banco,
    registrar_aporte_tesouro,
)
from bot.db.models import ShoppingItem
from bot.services.shopping import (
    add_item,
    clear_all,
    clear_checked,
    find_by_text,
    format_item,
    list_items,
    mark_checked,
    remove_item,
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
    await record_action(
        ctx.session, ctx.user.id, "tarefa",
        f"tarefa #{t.id}: {t.text}", {"task_id": t.id},
    )
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
    await record_action(
        ctx.session, ctx.user.id, "lembrete",
        f"lembrete #{rem.id}: {texto}", {"reminder_id": rem.id},
    )
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
    await record_action(
        ctx.session, ctx.user.id, "lembrete",
        f"lembrete de pagamento #{rem.id}: {beneficiario} {valor_fmt}",
        {"reminder_id": rem.id},
    )
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
    await record_action(
        ctx.session, ctx.user.id, "lembrete",
        f"lembrete recorrente #{rem.id}: {texto} ({recurrencia})",
        {"reminder_id": rem.id},
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
    recorrencia = (args.get("recorrencia") or "").strip().lower()
    if not tipo or not quando_iso:
        return "erro: 'tipo' e 'quando_iso' são obrigatórios"
    if tipo not in VALID_KINDS:
        return f"erro: tipo inválido. Use um de: {sorted(VALID_KINDS)}"
    if tipo == "chat" and not parametros:
        return "erro: para tipo='chat', 'parametros' deve conter o prompt a executar"
    if recorrencia and not is_valid_recurrence(recorrencia):
        return (
            "erro: recorrencia inválida. Use 'daily', 'weekday', 'weekend', "
            "'monthly' ou 'weekly:mon,wed,fri'."
        )

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
    label = descricao_map.get(tipo, tipo)
    texto = f"[agendado] {label}"
    rem = await create_reminder(
        ctx.session, ctx.user.id, texto, due_utc,
        command_kind=tipo, command_args=parametros or None,
        recurrence=recorrencia or None,
    )
    await record_action(
        ctx.session, ctx.user.id, "lembrete",
        f"comando agendado #{rem.id}: {label}" + (f" ({recorrencia})" if recorrencia else ""),
        {"reminder_id": rem.id},
    )
    local = due_utc.astimezone(tz)
    rec_label = f" — repete: {recorrencia}" if recorrencia else ""
    return (
        f"ok: comando #{rem.id} agendado: {label} "
        f"em {local.strftime('%d/%m %H:%M')}{rec_label}"
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
    await record_action(
        ctx.session, ctx.user.id, "financeiro",
        f"lançamento no banco: {entry['desc']} R$ {entry['amount']:.2f}",
        {"modulo": "banco", "entry_id": entry["id"]},
    )
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
    await record_action(
        ctx.session, ctx.user.id, "financeiro",
        f"compra no cartão: {entry['desc']} R$ {entry['amount']:.2f}",
        {"modulo": "cartao", "entry_id": entry["id"]},
    )
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
    contrib_id = (res.get("contribution") or {}).get("id")
    if contrib_id:
        await record_action(
            ctx.session, ctx.user.id, "financeiro",
            f"aporte de R$ {valor_f:.2f} no '{res['titulo']}'",
            {"modulo": "tesouro", "entry_id": contrib_id},
        )
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
    escopo_cartao = (args.get("escopo_cartao") or "fatura_aberta").strip().lower()
    if escopo_cartao not in ("fatura_aberta", "ultimos_dias"):
        return "erro: 'escopo_cartao' deve ser 'fatura_aberta' ou 'ultimos_dias'"
    today_iso = datetime.now(ZoneInfo(ctx.tz)).date().isoformat()
    try:
        out = await consultar_lancamentos(
            ctx.session, ctx.user, modulo, dias, today_iso,
            escopo_cartao=escopo_cartao,
        )
    except NotConfiguredError as e:
        return f"erro: {e}"
    except FinanceiroError as e:
        return f"erro: {e}"
    return "ok:\n" + out


async def _h_adicionar_lista_compras(args: dict, ctx: ToolContext) -> str:
    itens = args.get("itens") or []
    if not isinstance(itens, list) or not itens:
        return "erro: 'itens' deve ser lista não vazia"
    added = []
    for raw in itens:
        if isinstance(raw, str):
            text, qty = raw, None
        elif isinstance(raw, dict):
            text = (raw.get("text") or raw.get("texto") or "").strip()
            qty = (raw.get("quantidade") or raw.get("quantity") or None)
            if qty is not None:
                qty = qty.strip() if isinstance(qty, str) else str(qty)
        else:
            continue
        if not text:
            continue
        item = await add_item(ctx.session, ctx.user.id, text, qty)
        added.append(item)
    if not added:
        return "erro: nenhum item válido em 'itens'"
    await record_action(
        ctx.session, ctx.user.id, "compras",
        "itens de compra: " + ", ".join(i.text for i in added),
        {"item_ids": [i.id for i in added]},
    )
    if len(added) == 1:
        return f"ok: adicionado: {format_item(added[0])}"
    return "ok: adicionados " + " | ".join(format_item(i) for i in added)


async def _h_listar_compras(args: dict, ctx: ToolContext) -> str:
    escopo = (args.get("escopo") or "pendentes").strip().lower()
    only_pending = escopo != "todos"
    items = await list_items(ctx.session, ctx.user.id, only_pending=only_pending)
    if not items:
        if only_pending:
            return "ok: lista de compras vazia (sem itens pendentes)"
        return "ok: lista de compras vazia"
    head = "lista de compras (pendentes)" if only_pending else "lista de compras (todos)"
    return "ok: " + head + ":\n" + "\n".join(format_item(i) for i in items)


async def _h_marcar_comprado(args: dict, ctx: ToolContext) -> str:
    ids_raw = args.get("ids") or []
    texto = (args.get("texto") or "").strip()
    if not ids_raw and not texto:
        return "erro: passe 'ids' (lista de inteiros) ou 'texto' (busca por nome)"

    targets: list = []
    if ids_raw:
        if not isinstance(ids_raw, list):
            return "erro: 'ids' deve ser lista"
        for x in ids_raw:
            if not isinstance(x, int):
                return f"erro: id inválido em 'ids': {x!r}"
            item = await ctx.session.get(ShoppingItem, x)
            if item is None or item.user_id != ctx.user.id:
                return f"erro: item #{x} não encontrado"
            targets.append(item)
    else:
        matches = await find_by_text(ctx.session, ctx.user.id, texto, include_checked=False)
        if not matches:
            return f"erro: nenhum item pendente com '{texto}' na lista"
        if len(matches) > 1:
            names = " | ".join(f"#{m.id} {m.text}" for m in matches)
            return f"erro: ambíguo — vários itens batem em '{texto}': {names}. Use 'ids' pra desambiguar."
        targets = matches

    marked = []
    for t in targets:
        m = await mark_checked(ctx.session, ctx.user.id, t.id, checked=True)
        if m is not None:
            marked.append(m)
    if not marked:
        return "erro: nada foi marcado"
    return "ok: comprado(s): " + " | ".join(format_item(i) for i in marked)


async def _h_desmarcar_compra(args: dict, ctx: ToolContext) -> str:
    ids_raw = args.get("ids") or []
    if not isinstance(ids_raw, list) or not ids_raw:
        return "erro: 'ids' deve ser lista não vazia"
    unmarked = []
    for x in ids_raw:
        if not isinstance(x, int):
            return f"erro: id inválido: {x!r}"
        m = await mark_checked(ctx.session, ctx.user.id, x, checked=False)
        if m is not None:
            unmarked.append(m)
    if not unmarked:
        return "erro: nada foi desmarcado"
    return "ok: desmarcado(s): " + " | ".join(format_item(i) for i in unmarked)


async def _h_remover_lista_compras(args: dict, ctx: ToolContext) -> str:
    ids_raw = args.get("ids") or []
    texto = (args.get("texto") or "").strip()
    if not ids_raw and not texto:
        return "erro: passe 'ids' ou 'texto'"

    if ids_raw:
        if not isinstance(ids_raw, list):
            return "erro: 'ids' deve ser lista"
        removed = []
        for x in ids_raw:
            if not isinstance(x, int):
                return f"erro: id inválido: {x!r}"
            r = await remove_item(ctx.session, ctx.user.id, x)
            if r is not None:
                removed.append(r)
        if not removed:
            return "erro: nenhum item removido"
        return "ok: removido(s): " + " | ".join(f"#{r.id} {r.text}" for r in removed)

    matches = await find_by_text(ctx.session, ctx.user.id, texto, include_checked=True)
    if not matches:
        return f"erro: nenhum item bate em '{texto}'"
    if len(matches) > 1:
        names = " | ".join(f"#{m.id} {m.text}" for m in matches)
        return f"erro: ambíguo: {names}. Use 'ids' pra desambiguar."
    r = await remove_item(ctx.session, ctx.user.id, matches[0].id)
    return f"ok: removido #{r.id} {r.text}"


async def _h_limpar_comprados(_args: dict, ctx: ToolContext) -> str:
    n = await clear_checked(ctx.session, ctx.user.id)
    if n == 0:
        return "ok: nenhum item comprado pra limpar"
    return f"ok: {n} item(ns) comprado(s) removido(s) da lista"


async def _h_zerar_lista_compras(_args: dict, ctx: ToolContext) -> str:
    n = await clear_all(ctx.session, ctx.user.id)
    return f"ok: lista zerada ({n} item(ns) removido(s))"


async def _h_desfazer_ultima_acao(_args: dict, ctx: ToolContext) -> str:
    msg = await undo_last(ctx.session, ctx.user)
    return msg


async def _h_analisar_gastos(args: dict, ctx: ToolContext) -> str:
    agrupar_por = (args.get("agrupar_por") or "categoria").strip().lower()
    fonte = (args.get("fonte") or "tudo").strip().lower()

    today = datetime.now(ZoneInfo(ctx.tz)).date()
    inicio_iso = (args.get("inicio_iso") or "").strip()
    fim_iso = (args.get("fim_iso") or "").strip()

    # Se não vier intervalo, aceita 'dias' (janela até hoje) ou default 30d.
    if not inicio_iso or not fim_iso:
        dias = args.get("dias") or 30
        try:
            dias = int(dias)
        except (TypeError, ValueError):
            return "erro: 'dias' deve ser inteiro (ou passe inicio_iso+fim_iso)"
        from datetime import timedelta as _td
        fim_iso = today.isoformat()
        inicio_iso = (today - _td(days=dias)).isoformat()

    try:
        out = await analisar_gastos(
            ctx.session, ctx.user, inicio_iso, fim_iso,
            agrupar_por=agrupar_por, fonte=fonte,
        )
    except NotConfiguredError as e:
        return f"erro: {e}"
    except FinanceiroError as e:
        return f"erro: {e}"
    return "ok:\n" + out


async def _h_consultar_mp_dou(args: dict, ctx: ToolContext) -> str:
    from datetime import date as _date

    from bot.services.dou_monitor import DouError, fetch_mps

    data_iso = (args.get("data_iso") or "").strip()
    if data_iso:
        try:
            target = _date.fromisoformat(data_iso)
        except ValueError:
            return f"erro: 'data_iso' inválido ({data_iso!r}). Use 'YYYY-MM-DD'."
    else:
        target = datetime.now(ZoneInfo(ctx.tz)).date()

    try:
        mps = await fetch_mps(target)
    except DouError as e:
        return f"erro: {e}"
    except Exception:
        return "erro: falha ao consultar o DOU"

    if not mps:
        return f"ok: nenhuma MP publicada no DOU em {target.strftime('%d/%m/%Y')}"
    linhas = [f"ok: {len(mps)} MP(s) no DOU em {target.strftime('%d/%m/%Y')}:"]
    for mp in mps:
        ementa = (mp.get("ementa") or "")[:160]
        linhas.append(f"  • MP {mp['numero']}/{mp['ano']}: {ementa}")
    linhas.append("(nota técnica completa + DOCX via /mp_dou_agora ou no digest diário)")
    return "\n".join(linhas)


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
            "tivesse digitado. Use parametros pro prompt completo. É o tipo "
            "MAIS PODEROSO — serve pra agendar QUALQUER coisa que o bot saiba "
            "fazer (resumo de gastos, fatura do cartão, lista de compras, "
            "consulta de treino, etc). Ex: parametros='me manda o resumo dos "
            "meus gastos da semana e quanto sobrou no orçamento'.\n"
            "Para agendamentos RECORRENTES (todo dia/semana/mês), passe "
            "'recorrencia'. Ex: 'todo domingo 20h me manda o resumo da "
            "semana' → tipo='chat', parametros='resumo dos meus gastos e "
            "treinos da semana', quando_iso=<próximo domingo 20h>, "
            "recorrencia='weekly:sun'."
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
                    "description": "Data/hora local ISO 'YYYY-MM-DDTHH:MM' do primeiro disparo.",
                },
                "recorrencia": {
                    "type": "string",
                    "description": (
                        "Opcional. Pra repetir: 'daily', 'weekday', 'weekend', "
                        "'monthly' ou 'weekly:<dias>' (ex: 'weekly:mon,wed,fri' "
                        "ou 'weekly:sun'). Vazio = dispara só uma vez."
                    ),
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
                "categoria": {
                    "type": "string",
                    "description": (
                        "Hint de categoria em PT-BR — pode ser nome amigável, "
                        "id ou termo coloquial ('mercado', 'uber', 'netflix', "
                        "'plano de saúde', 'alimentação'). Servidor normaliza "
                        "contra defaults (alimentacao, transporte, moradia, "
                        "saude, lazer, educacao, compras, servicos, outros) + "
                        "customCategories do usuário, usando sinônimos comuns. "
                        "Fallback 'outros' se nada bater."
                    ),
                },
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
            "boleto, use lancar_movimento_banco.\n"
            "IMPORTANTE — 'valor' é SEMPRE o VALOR TOTAL DA COMPRA, "
            "nunca o valor da parcela:\n"
            "  - 'comprei celular 2400 em 12x' → valor=2400, parcelas=12 "
            "(NÃO valor=200).\n"
            "  - '10x de 200' → valor=2000, parcelas=10 (multiplique).\n"
            "  - 'fone 350 à vista' → valor=350, parcelas=1.\n"
            "O frontend calcula o valor de cada parcela como "
            "amount/installments na hora de exibir.\n"
            "Data = data da compra (frontend usa isso pra decidir em qual "
            "fatura cair). CHAME UMA VEZ por pedido."
        ),
        parameters={
            "type": "object",
            "properties": {
                "desc": {"type": "string"},
                "valor": {
                    "type": "number",
                    "description": "VALOR TOTAL da compra em reais (sempre o total, NUNCA da parcela)",
                },
                "data_iso": {"type": "string", "description": "Data ISO 'YYYY-MM-DD' da COMPRA (default: hoje)"},
                "categoria": {
                    "type": "string",
                    "description": (
                        "Hint de categoria em PT-BR — pode ser nome amigável, "
                        "id ou termo coloquial. Servidor normaliza contra "
                        "defaults + customCategories e sinônimos comuns. "
                        "Fallback 'outros'."
                    ),
                },
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
            "Consulta lançamentos do gerenciador-financeiro. Use quando o "
            "usuário perguntar 'como tá meu cartão', 'meus gastos da "
            "semana', 'quanto recebi esse mês', 'meus aportes no "
            "tesouro'.\n"
            "Módulos: 'banco' (conta), 'cartao' (crédito), 'tesouro' "
            "(títulos) ou 'tudo'.\n"
            "Janela padrão para 'banco' e 'tesouro' = últimos 'dias' "
            "dias (default 30).\n"
            "Para 'cartao' o escopo padrão é a FATURA EM ABERTO (não "
            "últimos N dias). Use escopo_cartao='ultimos_dias' apenas "
            "quando o usuário explicitamente pedir histórico mais longo "
            "do cartão (ex: 'gastos do cartão dos últimos 90 dias', "
            "'meu cartão nos últimos 2 meses'). Pedidos genéricos como "
            "'lista meus gastos do cartão', 'como tá o cartão', 'fatura "
            "do cartão' → use o default fatura_aberta."
        ),
        parameters={
            "type": "object",
            "properties": {
                "modulo": {
                    "type": "string",
                    "enum": ["banco", "cartao", "tesouro", "tudo"],
                },
                "dias": {"type": "integer", "description": "Janela em dias (default 30; aplicável a banco/tesouro e a cartão apenas quando escopo_cartao='ultimos_dias')"},
                "escopo_cartao": {
                    "type": "string",
                    "enum": ["fatura_aberta", "ultimos_dias"],
                    "description": "Como filtrar cartão. Default 'fatura_aberta' (mês corrente de fatura). 'ultimos_dias' só quando o usuário pedir histórico explicitamente.",
                },
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
            "GUARDA: a tool SÓ apaga lançamentos criados pelo próprio bot "
            "(marca interna source='bot'). Lançamentos feitos no app web "
            "estão protegidos — a tool retorna erro pedindo pra apagar no "
            "próprio app.\n"
            "Fluxo correto:\n"
            "  1) Se você não tem o id em mente, chame consultar_lancamentos "
            "primeiro. Cada linha vem como [id|origem]; só apague os com "
            "origem 'bot'.\n"
            "  2) Confirme com o usuário qual lançamento apagar se houver "
            "ambiguidade.\n"
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
    Tool(
        name="adicionar_lista_compras",
        description=(
            "Adiciona um ou mais itens à lista de compras do usuário "
            "(persistente entre sessões). Use sempre que ouvir 'acabou X', "
            "'preciso comprar Y', 'adiciona Z na lista', 'bota arroz na "
            "lista', 'lembra de Z amanhã no mercado'.\n"
            "Aceita uma lista de strings simples ou de objetos {text, "
            "quantidade}. Se o usuário falar várias coisas de uma vez "
            "('compra detergente, papel higiênico e 2kg de açúcar'), "
            "passe os 3 itens numa única chamada — NÃO chame a tool 3 "
            "vezes. Extraia quantidade quando explícita ('2kg de "
            "açúcar' → text='açúcar', quantidade='2kg')."
        ),
        parameters={
            "type": "object",
            "properties": {
                "itens": {
                    "type": "array",
                    "items": {
                        "oneOf": [
                            {"type": "string"},
                            {
                                "type": "object",
                                "properties": {
                                    "text": {"type": "string"},
                                    "quantidade": {"type": "string"},
                                },
                                "required": ["text"],
                            },
                        ],
                    },
                },
            },
            "required": ["itens"],
        },
        handler=_h_adicionar_lista_compras,
    ),
    Tool(
        name="listar_compras",
        description=(
            "Lista os itens da lista de compras. Use quando o usuário "
            "perguntar 'o que tem na minha lista', 'to indo no mercado', "
            "'minha lista de compras', 'o que preciso comprar'. "
            "Por padrão mostra só os PENDENTES (não comprados). Passe "
            "escopo='todos' pra incluir os já marcados como comprados."
        ),
        parameters={
            "type": "object",
            "properties": {
                "escopo": {
                    "type": "string",
                    "enum": ["pendentes", "todos"],
                },
            },
        },
        handler=_h_listar_compras,
    ),
    Tool(
        name="marcar_comprado",
        description=(
            "Marca item(ns) da lista como comprado. Use quando o usuário "
            "disser 'comprei o sal', 'já peguei o detergente', 'tenho o "
            "açúcar'. Aceita 'ids' (lista de int — preferível quando você "
            "tem certeza) ou 'texto' (busca substring case-insensitive). "
            "Se 'texto' bater em vários itens, a tool retorna erro listando "
            "as opções — você então confirma com o usuário e usa 'ids'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "ids": {"type": "array", "items": {"type": "integer"}},
                "texto": {"type": "string"},
            },
        },
        handler=_h_marcar_comprado,
    ),
    Tool(
        name="desmarcar_compra",
        description=(
            "Desmarca item(ns) que tinham sido marcados como comprados "
            "(volta pra pendente). Use quando o usuário disser 'na verdade "
            "não comprei o X', 'desmarca o Y'. Aceita só 'ids'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "ids": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["ids"],
        },
        handler=_h_desmarcar_compra,
    ),
    Tool(
        name="remover_lista_compras",
        description=(
            "Remove um ou mais itens da lista (apaga permanentemente, "
            "diferente de marcar como comprado). Use quando o usuário "
            "disser 'tira X da lista', 'apaga o Y', 'não preciso mais "
            "do Z'. Aceita 'ids' ou 'texto' (busca substring)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "ids": {"type": "array", "items": {"type": "integer"}},
                "texto": {"type": "string"},
            },
        },
        handler=_h_remover_lista_compras,
    ),
    Tool(
        name="limpar_comprados",
        description=(
            "Remove da lista TODOS os itens já marcados como comprados, "
            "deixando só os pendentes. Use quando o usuário disser "
            "'voltei do mercado, limpa os comprados', 'tira tudo que "
            "comprei', 'limpa a lista'."
        ),
        parameters={"type": "object", "properties": {}},
        handler=_h_limpar_comprados,
    ),
    Tool(
        name="zerar_lista_compras",
        description=(
            "Zera TODA a lista de compras (apaga pendentes e comprados). "
            "Só use quando o usuário PEDIR EXPLICITAMENTE 'zera a lista', "
            "'apaga tudo'. NÃO use ao voltar do mercado — pra isso use "
            "limpar_comprados, que preserva pendentes."
        ),
        parameters={"type": "object", "properties": {}},
        handler=_h_zerar_lista_compras,
    ),
    Tool(
        name="desfazer_ultima_acao",
        description=(
            "Desfaz a ÚLTIMA ação reversível que você executou pra este "
            "usuário (último lançamento financeiro, tarefa, lembrete, "
            "comando agendado ou item de compras criado). Use quando o "
            "usuário disser 'desfaz', 'desfaz isso', 'cancela o que você "
            "acabou de fazer', 'errei, desfaz'. Chamar de novo desfaz a "
            "ação anterior a essa (encadeia). NÃO reverte ações feitas no "
            "app web — só o que o bot criou. Repasse a mensagem de "
            "retorno fielmente."
        ),
        parameters={"type": "object", "properties": {}},
        handler=_h_desfazer_ultima_acao,
    ),
    Tool(
        name="analisar_gastos",
        description=(
            "Análise de gastos num intervalo, agrupada por categoria, mês "
            "ou semana. Considera só SAÍDAS (débitos do banco + gastos do "
            "cartão por fatura). Use pra perguntas analíticas: 'quanto "
            "gastei com alimentação em maio', 'qual minha maior categoria "
            "no trimestre', 'evolução dos meus gastos mês a mês', 'comparar "
            "maio com junho' (use agrupar_por='mes' cobrindo os dois "
            "meses).\n"
            "Passe inicio_iso+fim_iso pra intervalo exato, OU 'dias' pra "
            "janela até hoje (default 30). Para cruzar com outros dados "
            "(ex: treinos), combine com consultar_treinos numa mesma "
            "resposta e sintetize você mesmo."
        ),
        parameters={
            "type": "object",
            "properties": {
                "inicio_iso": {"type": "string", "description": "Início 'YYYY-MM-DD' (opcional se usar 'dias')"},
                "fim_iso": {"type": "string", "description": "Fim 'YYYY-MM-DD' (opcional se usar 'dias')"},
                "dias": {"type": "integer", "description": "Janela em dias até hoje (default 30; ignorado se inicio+fim vierem)"},
                "agrupar_por": {
                    "type": "string",
                    "enum": ["categoria", "mes", "semana"],
                },
                "fonte": {
                    "type": "string",
                    "enum": ["banco", "cartao", "tudo"],
                    "description": "Default 'tudo'",
                },
            },
        },
        handler=_h_analisar_gastos,
    ),
    Tool(
        name="consultar_mp_dou",
        description=(
            "Consulta Medidas Provisórias publicadas no Diário Oficial da "
            "União (DOU) numa data. Use quando o usuário perguntar 'saiu MP "
            "nova hoje?', 'tem medida provisória no diário oficial?', "
            "'foi publicada alguma MP essa semana?'. Retorna número + "
            "ementa de cada MP. Para a NOTA TÉCNICA completa + documento "
            "DOCX, oriente o usuário a usar /mp_dou_agora (ou esperar o "
            "digest diário se ele for assinante). Default: hoje."
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
        handler=_h_consultar_mp_dou,
    ),
]


def get_tool(name: str) -> Tool | None:
    return next((t for t in TOOLS if t.name == name), None)
