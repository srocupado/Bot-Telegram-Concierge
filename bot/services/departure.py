"""Melhor horário pra sair.

A Google Directions aceita `departure_time` no FUTURO e devolve o
duration_in_traffic *previsto* pelo modelo histórico do Google — então não há
modelo próprio aqui: varremos horários de partida candidatos e a API prevê.

A LÓGICA DE ESCOLHA (candidate_times, choose_best) é pura e separada da rede,
pra ser testável sem gastar chamada de API.
"""
from __future__ import annotations

import html
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx

from bot.services.traffic import TrafficError, fetch_traffic

logger = logging.getLogger(__name__)

# "9h", "9:30", "09h30", "9 30", "18h" → hora/min. Também ISO no parser abaixo.
_HORA_RE = re.compile(r"^\s*(\d{1,2})\s*(?:[:h]\s*(\d{2}))?\s*h?\s*$")

HORIZON_MIN = 60   # janela de varredura no modo "sair em breve"
STEP_MIN = 15      # passo entre sondagens
MAX_PROBES = 8     # teto de chamadas pagas à Directions por consulta


@dataclass(frozen=True)
class DepartureOption:
    depart_at: datetime           # tz-aware, no fuso do usuário
    travel_minutes: int
    # Só pra exibição — fora da comparação/igualdade (facilita os testes puros).
    distance_km: float = field(default=0.0, compare=False)
    summary: str = field(default="", compare=False)
    maps_url: str = field(default="", compare=False)

    @property
    def arrive_at(self) -> datetime:
        return self.depart_at + timedelta(minutes=self.travel_minutes)


def candidate_times(
    start: datetime,
    horizon_min: int,
    step_min: int,
    *,
    max_probes: int = MAX_PROBES,
    end: datetime | None = None,
) -> list[datetime]:
    """Horários de partida candidatos (tz-aware), no máx. `max_probes`.

    - Sem `end` (modo 'sair em breve'): de `start` até `start+horizon`, passo
      `step_min`.
    - Com `end` (modo 'chegar até'): de `start` até `end`, com o passo
      esticado pra caber em `max_probes` (não estoura o orçamento de chamadas).
    """
    if end is not None:
        window = (end - start).total_seconds() / 60.0
        if window <= 0:
            return []
        step = max(step_min, math.ceil(window / max_probes))
        horizon_min = window
    else:
        step = step_min

    out: list[datetime] = []
    k = 0
    while len(out) < max_probes:
        t = start + timedelta(minutes=k * step)
        if (t - start).total_seconds() / 60.0 > horizon_min + 1e-6:
            break
        out.append(t)
        k += 1
    return out


def choose_best(
    options: list[DepartureOption], arrive_by: datetime | None = None,
) -> tuple[DepartureOption | None, bool]:
    """(melhor_opção, viável).

    - Sem `arrive_by`: menor tempo de viagem (empate → partida mais cedo).
    - Com `arrive_by`: a partida MAIS TARDE que ainda chega no prazo. Se
      nenhuma chega a tempo, devolve a de chegada mais cedo com viável=False.
    """
    if not options:
        return None, False
    if arrive_by is None:
        best = min(options, key=lambda o: (o.travel_minutes, o.depart_at))
        return best, True
    on_time = [o for o in options if o.arrive_at <= arrive_by]
    if on_time:
        return max(on_time, key=lambda o: o.depart_at), True
    return min(options, key=lambda o: o.arrive_at), False


def parse_arrive_by(text: str, now: datetime) -> datetime | None:
    """Interpreta o horário-alvo. Aceita ISO local ('2026-07-05T09:00') ou
    hora do dia ('9h', '09:30', '9'). Só hora → hoje; se já passou, amanhã.
    `now` é tz-aware; o retorno herda o mesmo fuso. None se não entender."""
    raw = (text or "").strip()
    if not raw:
        return None
    # ISO com data (deixa o fuso do `now` — assume horário local do usuário).
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=now.tzinfo)
        return dt
    except ValueError:
        pass
    m = _HORA_RE.match(raw)
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2) or 0)
    if hh > 23 or mm > 59:
        return None
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def _fmt_hm(dt: datetime) -> str:
    return dt.strftime("%Hh%M") if dt.minute else dt.strftime("%Hh")


def format_departure_message(
    origin_label: str,
    dest_label: str,
    best: DepartureOption,
    options: list[DepartureOption],
    *,
    arrive_by: datetime | None,
    feasible: bool,
) -> str:
    """Mensagem HTML: recomendação + a curva de horários sondados."""
    o = html.escape(origin_label)
    d = html.escape(dest_label)
    lines = [f"🕒 <b>Melhor horário — {o} → {d}</b>", ""]

    if arrive_by is not None:
        alvo = _fmt_hm(arrive_by)
        if feasible:
            lines.append(
                f"Pra chegar até <b>{alvo}</b>, saia até "
                f"<b>{_fmt_hm(best.depart_at)}</b> "
                f"(~{best.travel_minutes} min, chega ~{_fmt_hm(best.arrive_at)})."
            )
        else:
            lines.append(
                f"⚠️ Nenhuma saída na janela chega até <b>{alvo}</b>. "
                f"O melhor é sair <b>{_fmt_hm(best.depart_at)}</b> "
                f"(~{best.travel_minutes} min, chega ~{_fmt_hm(best.arrive_at)})."
            )
    else:
        lines.append(
            f"Melhor sair <b>{_fmt_hm(best.depart_at)}</b> — "
            f"~{best.travel_minutes} min (chega ~{_fmt_hm(best.arrive_at)})."
        )

    lines.append("")
    for o_ in options:
        star = " ⭐" if o_.depart_at == best.depart_at else ""
        lines.append(f"• {_fmt_hm(o_.depart_at)} → ~{o_.travel_minutes} min{star}")

    via = f" via {html.escape(best.summary)}" if best.summary else ""
    if best.distance_km:
        lines.append(f"\n📏 {best.distance_km} km{via}")
    if best.maps_url:
        lines.append(
            f'🗺️ <a href="{html.escape(best.maps_url, quote=True)}">abrir no Maps</a>'
        )
    return "\n".join(lines)


async def plan_departure(
    client: httpx.AsyncClient,
    api_key: str,
    origin: str,
    destination: str,
    *,
    now: datetime,
    arrive_by: datetime | None,
    origin_label: str,
    dest_label: str,
    waypoints: list[str] | None = None,
) -> str | None:
    """Varredura + escolha + formatação, dados origem/destino já resolvidos.
    Devolve a mensagem HTML pronta, ou None se não houve sondagem válida (o
    caller trata a mensagem de erro). Reusado pela tool (origem nomeada) e pelo
    on_location (origem = GPS do usuário)."""
    candidates = candidate_times(now, HORIZON_MIN, STEP_MIN, end=arrive_by)
    if not candidates:
        return None
    options = await probe_departures(
        client, api_key, origin, destination,
        candidates=candidates, waypoints=waypoints or [],
    )
    best, feasible = choose_best(options, arrive_by)
    if best is None:
        return None
    return format_departure_message(
        origin_label, dest_label, best, options,
        arrive_by=arrive_by, feasible=feasible,
    )


async def probe_departures(
    client: httpx.AsyncClient,
    api_key: str,
    origin: str,
    destination: str,
    *,
    candidates: list[datetime],
    waypoints: list[str] | None = None,
) -> list[DepartureOption]:
    """Sonda cada horário candidato na Directions (1 chamada paga cada) e
    devolve as opções previstas. Sondagens que falham são puladas."""
    waypoints = waypoints or []
    options: list[DepartureOption] = []
    for t in candidates:
        ts = str(int(t.timestamp()))
        try:
            infos = await fetch_traffic(
                client, api_key, origin, destination, waypoints,
                departure_time=ts,
            )
        except TrafficError:
            logger.warning("departure: sondagem falhou p/ %s", t.isoformat(), exc_info=True)
            continue
        info = infos[0]
        options.append(DepartureOption(
            depart_at=t,
            travel_minutes=info.duration_minutes,
            distance_km=info.distance_km,
            summary=info.summary,
            maps_url=info.maps_url,
        ))
    return options
