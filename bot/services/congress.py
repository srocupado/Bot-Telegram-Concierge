from __future__ import annotations

import asyncio
import html
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

AGENDA_DAY_URL_TEMPLATE = (
    "https://www.congressonacional.leg.br/sessoes/"
    "agenda-do-congresso-senado-e-camara/-/agenda/{date}"
)
USER_AGENT = (
    "Mozilla/5.0 (compatible; TelegramTravelsBot/0.2; "
    "+https://github.com/srocupado/telegram-travels)"
)

_WEEKDAY_PT = {0: "seg", 1: "ter", 2: "qua", 3: "qui", 4: "sex", 5: "sáb", 6: "dom"}

_MP_REGEX = re.compile(r"\b(?:mpv?|cmmpv)\b")
_TIME_REGEX = re.compile(r"\b(\d{1,2})\s*h\s*(\d{2})\b")
_CANCELED_REGEX = re.compile(r"\bcancelad[oa]s?\b")


class CongressScrapeError(Exception):
    pass


@dataclass(frozen=True)
class MPItem:
    date: date
    hora: str | None
    descricao: str
    link: str | None


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()


def _is_mp(text: str) -> bool:
    norm = _normalize(text)
    if "medida provisoria" in norm:
        return True
    return bool(_MP_REGEX.search(norm))


def _week_bounds(today: date) -> tuple[date, date]:
    # Seg-sex da semana "ativa": no fim de semana (sáb=5, dom=6), pula pra
    # próxima segunda, pra não mostrar a agenda já encerrada.
    if today.weekday() >= 5:
        monday = today + timedelta(days=7 - today.weekday())
    else:
        monday = today - timedelta(days=today.weekday())
    friday = monday + timedelta(days=4)
    return monday, friday


def _extract_time(text: str) -> str | None:
    m = _TIME_REGEX.search(text)
    if not m:
        return None
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _parse_day(html_text: str, day: date, base_url: str) -> list[MPItem]:
    """Extract MP items from a single day's agenda page."""
    soup = BeautifulSoup(html_text, "html.parser")
    rows = soup.find_all("div", class_="cn-agenda-casas-tabela-linha")
    items: list[MPItem] = []
    seen: set[str] = set()
    for row in rows:
        # Filtro de casa: só eventos do Congresso Nacional (sessões conjuntas
        # e comissões mistas, incluindo CMMPV). Câmara (CD) e Senado (SF)
        # isolados ficam de fora.
        if row.get("data-casa") != "CN":
            continue
        full_text = " ".join(row.get_text(" ", strip=True).split())
        if not full_text or not _is_mp(full_text):
            continue
        if _CANCELED_REGEX.search(_normalize(full_text)):
            logger.info("congress: pulando evento cancelado em %s", day)
            continue
        orgao_block = row.find("div", class_="cn-agenda-casas-orgao")
        orgao = (
            " ".join(orgao_block.get_text(" ", strip=True).split())
            if orgao_block
            else None
        )
        # Título/link da sessão fica num <a> da célula principal, não no
        # <a> do órgão (link da comissão) nem no clone visible-phone.
        link_tag = None
        for a in row.find_all("a", href=True):
            ancestors_classes = {
                cls
                for parent in a.parents
                for cls in (parent.get("class") or [])
            }
            if "cn-agenda-casas-orgao" in ancestors_classes:
                continue
            if "visible-phone" in ancestors_classes:
                continue
            link_tag = a
            break
        titulo = (
            " ".join(link_tag.get_text(" ", strip=True).split()) or None
            if link_tag is not None
            else None
        )
        desc_block = row.find("blockquote", class_="cn-agenda-casas-descricao")
        desc_bq = (
            " ".join(desc_block.get_text(" ", strip=True).split())
            if desc_block
            else None
        )
        parts = [p for p in (orgao, titulo, desc_bq) if p]
        descricao = " — ".join(parts) if parts else full_text
        hora = _extract_time(full_text)
        link = urljoin(base_url, link_tag["href"]) if link_tag else None
        sig = descricao[:200]
        if sig in seen:
            continue
        seen.add(sig)
        items.append(MPItem(date=day, hora=hora, descricao=descricao, link=link))
    return items


async def _fetch_day(
    client: httpx.AsyncClient, day: date
) -> list[MPItem]:
    url = AGENDA_DAY_URL_TEMPLATE.format(date=day.isoformat())
    try:
        resp = await client.get(
            url, headers={"Accept-Language": "pt-BR,pt;q=0.9"}
        )
    except httpx.HTTPError as e:
        raise CongressScrapeError(f"http error for {day}: {e}") from e
    if resp.status_code != 200:
        raise CongressScrapeError(
            f"HTTP {resp.status_code} for {day}: {resp.text[:200]}"
        )
    try:
        return _parse_day(resp.text, day, url)
    except Exception as e:
        raise CongressScrapeError(f"parse error for {day}: {e}") from e


async def fetch_week_mps(client: httpx.AsyncClient, today: date) -> list[MPItem]:
    """Fetch MP items from the Congresso Nacional agenda for Mon-Fri of `today`'s week.

    Fetches one page per weekday in parallel. Raises CongressScrapeError if any day fails.
    """
    monday, _ = _week_bounds(today)
    days = [monday + timedelta(days=i) for i in range(5)]
    results = await asyncio.gather(
        *(_fetch_day(client, d) for d in days), return_exceptions=True
    )
    items: list[MPItem] = []
    errors: list[str] = []
    for res in results:
        if isinstance(res, CongressScrapeError):
            errors.append(str(res))
        elif isinstance(res, BaseException):
            errors.append(repr(res))
        else:
            items.extend(res)
    if errors and not items:
        raise CongressScrapeError("; ".join(errors))
    if errors:
        logger.warning("partial congress scrape: %s", "; ".join(errors))
    items.sort(key=lambda i: (i.date, i.hora or ""))
    return items


def format_week_message(items: list[MPItem], today: date) -> str:
    monday, friday = _week_bounds(today)
    header = (
        f"🏛️ <b>Agenda do Congresso — semana de "
        f"{monday.strftime('%d/%m')} a {friday.strftime('%d/%m')}</b>"
    )
    if not items:
        return f"{header}\n\nSem MP esta semana."
    lines = [header, ""]
    for item in items:
        wd = _WEEKDAY_PT[item.date.weekday()]
        when = f"{wd} {item.date.strftime('%d/%m')}"
        if item.hora:
            when = f"{when} {item.hora}"
        descricao = item.descricao
        if len(descricao) > 280:
            descricao = descricao[:277] + "..."
        descricao_html = html.escape(descricao)
        if item.link:
            link_esc = html.escape(item.link)
            lines.append(
                f'• <b>{when}</b> — {descricao_html} (<a href="{link_esc}">link</a>)'
            )
        else:
            lines.append(f"• <b>{when}</b> — {descricao_html}")
    return "\n".join(lines)
