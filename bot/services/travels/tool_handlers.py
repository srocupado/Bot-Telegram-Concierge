"""Tool handlers do agente — buscar voo/hotel e gerenciar watches.

São chamados via `chat_with_tools` (texto livre ou voz) e usam
`ctx.direct_html` + `ctx.short_circuit` pra que o resultado formatado
chegue ao usuário sem o LLM parafrasear.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from bot.config import settings
from bot.db.models import TravelWatch
from bot.services.llm.base import ToolContext
from bot.services.travels.serpapi_client import (
    SerpAPIClient,
    SerpAPIError,
    attach_return_leg,
    extract_best_flight,
    extract_best_hotel,
    extract_price_insights,
    format_flight,
    format_hotel,
    hotel_name_matches,
)

logger = logging.getLogger(__name__)


def _serpapi_or_error() -> SerpAPIClient | str:
    if settings.serpapi_key is None:
        return "erro: SERPAPI_KEY não configurada no .env"
    return SerpAPIClient(settings.serpapi_key.get_secret_value())


async def _h_buscar_voo(args: dict, ctx: ToolContext) -> str:
    origin = (args.get("origin_iata") or "").upper().strip()
    dest = (args.get("destination_iata") or "").upper().strip()
    depart = (args.get("depart_date") or "").strip()
    ret = (args.get("return_date") or "").strip() or None
    adults = int(args.get("adults") or 1)
    travel_class = int(args.get("travel_class") or 1)
    if not (origin and dest and depart):
        return "erro: precisa de origin_iata, destination_iata e depart_date (YYYY-MM-DD)"

    client = _serpapi_or_error()
    if isinstance(client, str):
        return client
    try:
        async with client as serpapi:
            raw = await serpapi.search_flights(
                origin, dest, depart, ret, adults, "BRL", travel_class,
            )
            best = extract_best_flight(raw)
            insights = extract_price_insights(raw)
            if best is None:
                return f"sem ofertas pra {origin}→{dest} em {depart}"
            price, payload = best
            if ret:
                await attach_return_leg(
                    serpapi, payload, origin, dest, depart, ret, adults, "BRL", travel_class,
                )
            html_msg = (
                f"✈️ <b>{origin} → {dest}</b>\n"
                + format_flight(price, payload, depart, ret, insights)
            )
            ctx.direct_html = html_msg
            ctx.short_circuit = True
            return f"ok: melhor preço R$ {price:.2f} enviado ao usuário"
    except SerpAPIError as e:
        return f"erro SerpAPI: {e}"


_LODGING_WORDS = ("hotel", "pousada", "resort", "hostel", "inn", "flat")


async def _h_buscar_hotel(args: dict, ctx: ToolContext) -> str:
    location = (args.get("location") or "").strip()
    hotel = (args.get("hotel") or "").strip()
    ci = (args.get("check_in") or "").strip()
    co = (args.get("check_out") or "").strip()
    adults = int(args.get("adults") or 2)
    if not (location and ci and co):
        return "erro: precisa de location, check_in e check_out (YYYY-MM-DD)"

    # Hotel NOMEADO: a query precisa do formato-ENTIDADE pro Google resolver o
    # hotel específico (com todas as fontes de preço) em vez de listar a cidade.
    # Testado ao vivo: 'Gran Marquise Fortaleza' → 9 hotéis da cidade (sem ele);
    # 'Hotel Gran Marquise Fortaleza' → o hotel, na raiz, com 19 preços.
    if hotel:
        prefixo = "" if any(w in hotel.lower() for w in _LODGING_WORDS) else "Hotel "
        q = f"{prefixo}{hotel} {location}"
    else:
        q = location

    client = _serpapi_or_error()
    if isinstance(client, str):
        return client
    try:
        async with client as serpapi:
            raw = await serpapi.search_hotels(q, ci, co, adults, "BRL")
            best = extract_best_hotel(raw, prefer_name=hotel or None)
            if best is None:
                alvo = f"{hotel} em {location}" if hotel else location
                return f"sem hotéis pra '{alvo}' ({ci}→{co})"
            price, payload = best
            # Pediu hotel específico mas veio OUTRO → não finge: avisa e mostra
            # o mais barato da cidade como referência rotulada.
            if hotel and not hotel_name_matches(hotel, payload.get("name")):
                html_msg = (
                    f"⚠️ Não achei diária pro <b>{hotel}</b> em {location} "
                    f"nesse período ({ci} → {co}) — pode estar esgotado ou "
                    f"fora do Google Hotels. Confirme direto com o hotel.\n\n"
                    f"<b>Referência, o mais barato na cidade:</b>\n"
                    + format_hotel(price, payload, ci, co)
                )
                ctx.direct_html = html_msg
                ctx.short_circuit = True
                return "ok: aviso enviado ao usuário (não escreva nada)"
            html_msg = (
                f"🏨 <b>{hotel or location}</b>\n"
                + format_hotel(price, payload, ci, co)
            )
            ctx.direct_html = html_msg
            ctx.short_circuit = True
            return f"ok: melhor diária R$ {price:.2f} enviado ao usuário"
    except SerpAPIError as e:
        return f"erro SerpAPI: {e}"


async def _h_criar_watch_voo(args: dict, ctx: ToolContext) -> str:
    origin = (args.get("origin_iata") or "").upper().strip()
    dest = (args.get("destination_iata") or "").upper().strip()
    depart = (args.get("depart_date") or "").strip()
    ret = (args.get("return_date") or "").strip() or None
    max_price = args.get("max_price")
    summary = (args.get("summary") or f"{origin}→{dest}").strip()[:256]
    if not (origin and dest and depart):
        return "erro: precisa de origin_iata, destination_iata e depart_date"
    params = {
        "origin_iata": origin, "destination_iata": dest, "depart_date": depart,
        "adults": int(args.get("adults") or 1),
        "travel_class": int(args.get("travel_class") or 1),
    }
    if ret:
        params["return_date"] = ret
    watch = TravelWatch(
        user_id=ctx.user.id, kind="flight", params=params,
        max_price=float(max_price) if max_price is not None else None,
        summary=summary,
    )
    ctx.session.add(watch)
    await ctx.session.commit()
    return f"ok: watch #{watch.id} criado pra {summary}"


async def _h_criar_watch_hotel(args: dict, ctx: ToolContext) -> str:
    location = (args.get("location") or "").strip()
    ci = (args.get("check_in") or "").strip()
    co = (args.get("check_out") or "").strip()
    max_price = args.get("max_price")
    summary = (args.get("summary") or location)[:256]
    if not (location and ci and co):
        return "erro: precisa de location, check_in e check_out"
    params = {
        "location": location, "check_in": ci, "check_out": co,
        "adults": int(args.get("adults") or 2),
    }
    watch = TravelWatch(
        user_id=ctx.user.id, kind="hotel", params=params,
        max_price=float(max_price) if max_price is not None else None,
        summary=summary,
    )
    ctx.session.add(watch)
    await ctx.session.commit()
    return f"ok: watch #{watch.id} criado pra {summary}"


async def _h_listar_watches_viagem(_args: dict, ctx: ToolContext) -> str:
    stmt = select(TravelWatch).where(
        TravelWatch.user_id == ctx.user.id,
        TravelWatch.status == "active",
    ).order_by(TravelWatch.id.desc())
    items = list((await ctx.session.scalars(stmt)).all())
    if not items:
        return "nenhum watch de viagem ativo"
    lines = []
    for w in items:
        icon = "✈️" if w.kind == "flight" else "🏨"
        teto = f" · teto R$ {w.max_price:.2f}" if w.max_price else ""
        ultimo = f" · último R$ {w.last_price:.2f}" if w.last_price else ""
        lines.append(f"#{w.id} {icon} {w.summary}{teto}{ultimo}")
    return "\n".join(lines)


async def _h_cancelar_watch_viagem(args: dict, ctx: ToolContext) -> str:
    wid = args.get("id")
    if wid is None:
        return "erro: parâmetro 'id' ausente"
    watch = await ctx.session.get(TravelWatch, int(wid))
    if watch is None or watch.user_id != ctx.user.id:
        return f"erro: watch #{wid} não encontrado"
    watch.status = "cancelled"
    await ctx.session.commit()
    return f"ok: watch #{wid} cancelado"
