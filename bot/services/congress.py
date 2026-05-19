"""Scraper da agenda de Medidas Provisórias do Congresso Nacional.

Usa a página pública `congressonacional.leg.br/.../mpv/em-tramitacao`, sem
APIs de Dados Abertos. Para evitar bloqueio 403, envia headers completos
de browser e visita a home antes para coletar cookies.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

CONGRESS_HOME = "https://www.congressonacional.leg.br/"
CONGRESS_URL = "https://www.congressonacional.leg.br/materias/medidas-provisorias/-/mpv/em-tramitacao"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


@dataclass
class MPItem:
    title: str
    summary: str
    status: str
    url: str | None


async def fetch_mps(limit: int = 10) -> list[MPItem]:
    """Faz scraping da página em tramitação do Congresso Nacional."""
    try:
        async with httpx.AsyncClient(
            timeout=20.0, follow_redirects=True, headers=BROWSER_HEADERS, http2=False
        ) as client:
            # Visita a home primeiro para receber cookies de sessão (necessário em
            # alguns gateways do portal).
            try:
                await client.get(CONGRESS_HOME)
            except Exception:
                logger.debug("falha ao visitar home (seguindo)")
            r = await client.get(CONGRESS_URL, headers={"Referer": CONGRESS_HOME})
            r.raise_for_status()
            html = r.text
    except Exception:
        logger.exception("congresso request failed")
        return []

    return _parse(html, limit)


def _parse(html: str, limit: int) -> list[MPItem]:
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    items: list[MPItem] = []

    # A página lista cada MP em um bloco com link contendo o número. Procuramos
    # todos os <a> cujo texto bate com o padrão MPV NNNN/AAAA.
    for a in soup.select("a"):
        text = a.get_text(" ", strip=True)
        m = re.search(r"\b(MPV?\s?\d{3,4}/\d{4})\b", text, flags=re.IGNORECASE)
        if not m:
            continue
        title = re.sub(r"\s+", " ", m.group(1).upper()).replace("MP ", "MPV ")
        if title in seen:
            continue
        seen.add(title)

        # Pega o bloco-pai para extrair ementa e situação.
        container = a.find_parent(["article", "li", "tr", "div"]) or a
        block_text = container.get_text(" ", strip=True)
        summary = _extract_summary(block_text, title)
        status = _extract_status(block_text)
        href = a.get("href") or ""
        url = href if href.startswith("http") else None
        items.append(MPItem(title=title, summary=summary, status=status, url=url))
        if len(items) >= limit:
            break

    return items


def _extract_summary(block_text: str, title: str) -> str:
    idx = block_text.upper().find(title)
    tail = block_text[idx + len(title) :] if idx >= 0 else block_text

    # Prefere o trecho que vem após "Ementa" (descrição oficial), caindo para
    # "Título" caso a ementa não exista.
    m = re.search(r"Ementa\b[:\s]+(.+?)(?:\s+(?:Dia de tramita|Situa|Última|$))", tail, flags=re.IGNORECASE | re.DOTALL)
    if m:
        text = m.group(1)
    else:
        m_t = re.search(r"T[ií]tulo\b[:\s]+(.+?)(?:\s+(?:Ementa|Dia de tramita|Situa|Última|$))", tail, flags=re.IGNORECASE | re.DOTALL)
        text = m_t.group(1) if m_t else tail

    text = re.sub(r"\s+", " ", text).strip(" -–—:.;")
    if len(text) > 240:
        text = text[:237].rstrip() + "..."
    return text or "(sem ementa disponível)"


def _extract_status(block_text: str) -> str:
    for kw in (
        "Aguardando designação",
        "Aguardando análise",
        "Aguardando",
        "Em tramitação",
        "Em análise",
        "Aprovada",
        "Rejeitada",
        "Vencida",
        "Prorrogada",
        "Devolvida",
    ):
        if kw.lower() in block_text.lower():
            return kw
    return "—"


def format_mps_message(items: list[MPItem]) -> str:
    if not items:
        return "📜 *Medidas Provisórias*\n\nNão foi possível obter a lista no momento."
    lines = ["📜 *Medidas Provisórias em tramitação*\n"]
    for it in items:
        lines.append(f"• *{it.title}* — _{it.status}_\n  {it.summary}")
    return "\n\n".join(lines)
