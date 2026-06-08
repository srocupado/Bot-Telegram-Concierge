"""Travels: busca de voos e hotéis via SerpAPI.

Portado de https://github.com/srocupado/Telegram-Travels (mesma stack
arquitetural). Diferenças vs origem:
- `SerpAPIClient.__init__` recebe a key direto, não o objeto `Settings`.
- `compose_alert_message` foi removido — alertas usam template fixo
  (evita acoplar custo de LLM ao tick do scheduler).
"""
from __future__ import annotations

import html
import logging
from datetime import date, timedelta
from typing import Any

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://serpapi.com/search.json"
MAX_FLEX_ITERATIONS = 14
MAX_FLIGHT_FLEX_SAMPLES = 5


class SerpAPIError(Exception):
    pass


class SerpAPIClient:
    def __init__(self, api_key: str) -> None:
        self._key = api_key
        self._client = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "SerpAPIClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        params = {**params, "api_key": self._key}
        resp = await self._client.get(BASE_URL, params=params)
        if resp.status_code != 200:
            raise SerpAPIError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        data: dict[str, Any] = resp.json()
        if "error" in data:
            raise SerpAPIError(str(data["error"]))
        return data

    async def search_flights(
        self,
        origin_iata: str,
        destination_iata: str,
        depart_date: str,
        return_date: str | None = None,
        adults: int = 1,
        currency: str = "BRL",
        travel_class: int = 1,
        departure_token: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "engine": "google_flights",
            "departure_id": origin_iata,
            "arrival_id": destination_iata,
            "outbound_date": depart_date,
            "adults": adults,
            "currency": currency,
            "hl": "pt-br",
            "gl": "br",
            "type": "2" if return_date is None else "1",
        }
        if return_date:
            params["return_date"] = return_date
        if travel_class and travel_class != 1:
            params["travel_class"] = travel_class
        if departure_token:
            params["departure_token"] = departure_token
        return await self._get(params)

    async def search_hotels(
        self,
        location: str,
        check_in: str,
        check_out: str,
        adults: int = 2,
        currency: str = "BRL",
    ) -> dict[str, Any]:
        params = {
            "engine": "google_hotels",
            "q": location,
            "check_in_date": check_in,
            "check_out_date": check_out,
            "adults": adults,
            "currency": currency,
            "hl": "pt-br",
            "gl": "br",
        }
        return await self._get(params)


def extract_price_insights(raw: dict[str, Any]) -> dict[str, Any] | None:
    pi = raw.get("price_insights")
    return pi if isinstance(pi, dict) else None


def extract_best_flight(raw: dict[str, Any]) -> tuple[float, dict[str, Any]] | None:
    candidates: list[dict[str, Any]] = []
    candidates.extend(raw.get("best_flights") or [])
    candidates.extend(raw.get("other_flights") or [])
    if not candidates:
        return None
    priced = [c for c in candidates if isinstance(c.get("price"), (int, float))]
    if not priced:
        return None
    best = min(priced, key=lambda c: c["price"])
    return float(best["price"]), best


async def attach_return_leg(
    serpapi: "SerpAPIClient",
    payload: dict[str, Any],
    origin_iata: str,
    destination_iata: str,
    depart_date: str,
    return_date: str,
    adults: int,
    currency: str,
    travel_class: int,
) -> None:
    """Em round-trip, busca o trecho de volta via `departure_token` e
    embute o melhor resultado em `_return_flights` / `_return_layovers`.
    Best-effort: falhas são logadas e silenciosamente ignoradas."""
    token = payload.get("departure_token")
    if not token:
        return
    try:
        raw = await serpapi.search_flights(
            origin_iata=origin_iata,
            destination_iata=destination_iata,
            depart_date=depart_date,
            return_date=return_date,
            adults=adults,
            currency=currency,
            travel_class=travel_class,
            departure_token=token,
        )
    except Exception:
        logger.exception(
            "return leg fetch failed for %s→%s; continuing without volta",
            origin_iata,
            destination_iata,
        )
        return
    leg = extract_best_flight(raw)
    if leg is None:
        return
    _, ret_payload = leg
    payload["_return_flights"] = ret_payload.get("flights") or []
    payload["_return_layovers"] = ret_payload.get("layovers") or []
    payload["_return_total_duration"] = ret_payload.get("total_duration")


async def find_best_flight_in_window(
    serpapi: "SerpAPIClient",
    origin_iata: str,
    destination_iatas: list[str],
    window_start: str,
    window_end: str,
    stay_nights: int,
    adults: int = 1,
    currency: str = "BRL",
    travel_class: int = 1,
    max_samples: int = MAX_FLIGHT_FLEX_SAMPLES,
) -> tuple[float, dict[str, Any], str, str, str, dict[str, Any] | None] | None:
    try:
        start = date.fromisoformat(window_start)
        end = date.fromisoformat(window_end)
    except ValueError:
        return None
    last_depart = end - timedelta(days=stay_nights)
    if last_depart < start or stay_nights <= 0 or not destination_iatas:
        return None
    span = (last_depart - start).days + 1
    n = min(span, max_samples)
    offsets = [0] if n == 1 else [int(i * (span - 1) / (n - 1)) for i in range(n)]

    best: tuple[float, dict[str, Any], str, str, str, dict[str, Any] | None] | None = None
    for off in offsets:
        depart = (start + timedelta(days=off)).isoformat()
        ret = (start + timedelta(days=off + stay_nights)).isoformat()
        for dest in destination_iatas:
            try:
                raw = await serpapi.search_flights(
                    origin_iata, dest, depart, ret, adults, currency, travel_class
                )
            except SerpAPIError as e:
                logger.warning(
                    "flex flight leg %s→%s %s/%s failed: %s",
                    origin_iata, dest, depart, ret, e,
                )
                continue
            leg = extract_best_flight(raw)
            if leg is None:
                continue
            price, payload = leg
            if best is None or price < best[0]:
                best = (price, payload, depart, ret, dest, extract_price_insights(raw))
    return best


def _extract_rate(rate_obj: Any) -> float | None:
    if not isinstance(rate_obj, dict):
        return None
    val = rate_obj.get("extracted_lowest") or rate_obj.get("lowest")
    if isinstance(val, str):
        try:
            return float(
                val.replace("R$", "")
                .replace("$", "")
                .replace(".", "")
                .replace(",", ".")
                .strip()
            )
        except ValueError:
            return None
    if isinstance(val, (int, float)):
        return float(val)
    return None


def _property_price(p: dict[str, Any]) -> float | None:
    direct = _extract_rate(p.get("rate_per_night")) or _extract_rate(p.get("total_rate"))
    if direct:
        return direct
    sources: list[Any] = []
    sources.extend(p.get("featured_prices") or [])
    sources.extend(p.get("prices") or [])
    for source in sources:
        if not isinstance(source, dict):
            continue
        r = _extract_rate(source.get("rate_per_night")) or _extract_rate(source.get("total_rate"))
        if r:
            return r
    return None


def extract_best_hotel(raw: dict[str, Any]) -> tuple[float, dict[str, Any]] | None:
    candidates: list[dict[str, Any]] = []
    candidates.extend(raw.get("properties") or [])
    candidates.extend(raw.get("ads") or [])
    candidates.extend(raw.get("featured_results") or [])

    # Resposta de hotel único (q = nome específico) tem campos na raiz.
    if not candidates and (
        "rate_per_night" in raw
        or "total_rate" in raw
        or "prices" in raw
        or "featured_prices" in raw
    ):
        candidates.append(raw)

    if not candidates:
        logger.warning(
            "hotel response had no candidates: top-level keys=%s",
            list(raw.keys()),
        )
        return None
    priced: list[tuple[float, dict[str, Any]]] = []
    for p in candidates:
        price = _property_price(p)
        if price is not None:
            priced.append((price, p))
    if not priced:
        logger.warning(
            "hotel response had %d candidates but no extractable prices",
            len(candidates),
        )
        return None
    return min(priced, key=lambda t: t[0])


async def find_best_hotel_in_window(
    serpapi: "SerpAPIClient",
    location: str,
    window_start: str,
    window_end: str,
    nights: int,
    adults: int = 2,
    currency: str = "BRL",
) -> tuple[float, dict[str, Any], str, str] | None:
    try:
        start = date.fromisoformat(window_start)
        end = date.fromisoformat(window_end)
    except ValueError:
        return None
    last_checkin = end - timedelta(days=nights)
    if last_checkin < start or nights <= 0:
        return None
    span = (last_checkin - start).days + 1
    iterations = min(span, MAX_FLEX_ITERATIONS)
    best: tuple[float, dict[str, Any], str, str] | None = None
    for i in range(iterations):
        ci = (start + timedelta(days=i)).isoformat()
        co = (start + timedelta(days=i + nights)).isoformat()
        try:
            raw = await serpapi.search_hotels(location, ci, co, adults, currency)
        except SerpAPIError as e:
            logger.warning("flex hotel leg %s→%s failed: %s", ci, co, e)
            continue
        leg = extract_best_hotel(raw)
        if leg is None:
            continue
        price, payload = leg
        if best is None or price < best[0]:
            best = (price, payload, ci, co)
    return best


def _fmt_duration(mins: Any) -> str:
    if not isinstance(mins, (int, float)) or mins <= 0:
        return "?"
    h, m = divmod(int(mins), 60)
    if h and m:
        return f"{h}h{m:02d}"
    if h:
        return f"{h}h"
    return f"{m}min"


def _fmt_time(s: Any) -> str:
    if not isinstance(s, str) or not s:
        return "?"
    parts = s.split(" ")
    return parts[1] if len(parts) > 1 else s


_PRICE_LEVEL_LABEL = {
    "low": "🟢 Preço baixo",
    "typical": "🟡 Preço normal",
    "high": "🔴 Preço alto",
}


def _h(value: Any) -> str:
    return html.escape(str(value)) if value is not None else ""


def format_flight(
    price: float,
    payload: dict[str, Any],
    chosen_depart: str | None = None,
    chosen_return: str | None = None,
    price_insights: dict[str, Any] | None = None,
) -> str:
    lines: list[str] = [f"💰 <b>R$ {price:.2f}</b>"]
    if price_insights:
        level = price_insights.get("price_level")
        label = _PRICE_LEVEL_LABEL.get(level) if isinstance(level, str) else None
        typical = price_insights.get("typical_price_range")
        if label:
            extra = ""
            if isinstance(typical, list) and len(typical) == 2:
                try:
                    extra = f" (faixa típica R$ {float(typical[0]):.0f}–{float(typical[1]):.0f})"
                except (TypeError, ValueError):
                    extra = ""
            lines.append(f"{label}{extra}")
    if chosen_depart:
        try:
            d1 = date.fromisoformat(chosen_depart)
            label = f"📅 Ida {d1.strftime('%d/%m')}"
            if chosen_return:
                d2 = date.fromisoformat(chosen_return)
                label += f" → Volta {d2.strftime('%d/%m')} ({(d2 - d1).days} dias)"
            lines.append(label)
        except ValueError:
            pass

    total_duration = payload.get("total_duration")
    if total_duration:
        lines.append(f"⏱ Duração total: {_fmt_duration(total_duration)}")

    trip_type = payload.get("type")
    if trip_type:
        lines.append(f"🔁 {_h(trip_type)}")

    outbound_flights = payload.get("flights") or []
    outbound_layovers = payload.get("layovers") or []
    return_flights = payload.get("_return_flights") or []
    return_layovers = payload.get("_return_layovers") or []

    sections: list[tuple[str | None, list[dict[str, Any]], list[dict[str, Any]]]] = []
    if return_flights:
        sections.append(("✈️ <b>Ida</b>", outbound_flights, outbound_layovers))
        sections.append(("🔙 <b>Volta</b>", return_flights, return_layovers))
    else:
        sections.append((None, outbound_flights, outbound_layovers))

    for header_label, flights, layovers in sections:
        lines.append("")
        if header_label:
            lines.append(header_label)
        for i, flight in enumerate(flights):
            airline = flight.get("airline", "?")
            flight_number = flight.get("flight_number", "")
            airplane = flight.get("airplane") or ""
            travel_class = flight.get("travel_class") or ""

            dep = flight.get("departure_airport") or {}
            arr = flight.get("arrival_airport") or {}

            header = f"<b>{_h(airline)} {_h(flight_number)}</b>".rstrip()
            details: list[str] = []
            if travel_class:
                details.append(_h(travel_class))
            if airplane:
                details.append(_h(airplane))
            if details:
                header += f" — {' / '.join(details)}"
            lines.append(header)

            dep_name = dep.get("name") or "?"
            dep_id = dep.get("id") or ""
            arr_name = arr.get("name") or "?"
            arr_id = arr.get("id") or ""
            lines.append(f"  🛫 {_h(_fmt_time(dep.get('time')))} {_h(dep_name)} ({_h(dep_id)})")
            lines.append(f"  🛬 {_h(_fmt_time(arr.get('time')))} {_h(arr_name)} ({_h(arr_id)})")

            leg_dur = flight.get("duration")
            if leg_dur:
                lines.append(f"  ⏱ {_h(_fmt_duration(leg_dur))}")

            if flight.get("overnight"):
                lines.append("  🌙 voo noturno")

            if i < len(layovers):
                lay = layovers[i]
                lay_name = lay.get("name") or lay.get("id") or "?"
                lines.append(
                    f"  ⤷ Conexão em {_h(lay_name)}: {_h(_fmt_duration(lay.get('duration')))}"
                )

    extensions = payload.get("extensions") or []
    if extensions:
        lines.append("")
        for ext in extensions[:4]:
            lines.append(f"• {_h(ext)}")

    return "\n".join(lines)


def format_hotel(
    price: float,
    payload: dict[str, Any],
    chosen_check_in: str | None = None,
    chosen_check_out: str | None = None,
) -> str:
    name = payload.get("name") or "Hotel"
    hotel_class = payload.get("hotel_class") or ""
    rating = payload.get("overall_rating")
    reviews = payload.get("reviews")
    check_in = payload.get("check_in_time")
    check_out = payload.get("check_out_time")
    amenities = payload.get("amenities") or []
    description = payload.get("description")
    nearby = payload.get("nearby_places") or []
    link = payload.get("link")

    lines: list[str] = [f"💰 <b>R$ {price:.2f}</b> / diária"]
    if chosen_check_in and chosen_check_out:
        try:
            ci = date.fromisoformat(chosen_check_in)
            co = date.fromisoformat(chosen_check_out)
            nights = (co - ci).days
            lines.append(
                f"📅 {ci.strftime('%d/%m')} → {co.strftime('%d/%m')} ({nights} noite{'s' if nights != 1 else ''})"
            )
        except ValueError:
            lines.append(f"📅 {chosen_check_in} → {chosen_check_out}")
    lines.append(f"🏨 <b>{_h(name)}</b>")
    if hotel_class:
        lines.append(f"  {_h(hotel_class)}")
    if rating:
        rating_line = f"  ⭐ {_h(rating)}"
        if reviews:
            rating_line += f" ({_h(reviews)} avaliações)"
        lines.append(rating_line)
    if check_in or check_out:
        lines.append(
            f"  🕐 Check-in {_h(check_in or '?')} · Check-out {_h(check_out or '?')}"
        )
    if amenities:
        top = amenities[:6]
        lines.append(f"  ✨ {_h(', '.join(str(a) for a in top))}")
    if nearby:
        first = nearby[0]
        if isinstance(first, dict) and first.get("name"):
            transp = first.get("transportations") or []
            extra = ""
            if transp and isinstance(transp[0], dict):
                extra = f" ({transp[0].get('duration', '')} {transp[0].get('type', '')})".strip()
            lines.append(f"  📍 Perto de: {_h(first['name'])}{(' ' + _h(extra)) if extra else ''}")
    if description:
        lines.append("")
        text = str(description)
        lines.append(_h(text[:300] + ("…" if len(text) > 300 else "")))
    if link:
        lines.append("")
        lines.append(f'🔗 <a href="{_h(link)}">Ver no Google Hotels</a>')

    return "\n".join(lines)
