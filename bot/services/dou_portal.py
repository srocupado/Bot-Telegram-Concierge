"""Fallback público de detecção de MP: portal www.in.gov.br.

Homologado contra o site real (06/08/2026, sondas do container E do Orange
Pi — MP 1.381 encontrada de ponta a ponta): o endpoint /consulta/-/buscar/dou
responde SEM login com a lista de matérias em JSON embutido no HTML (bloco
<script id="..._params">), incluindo artType "Medida Provisória", seção,
data de publicação e o link da matéria — e a página da matéria entrega o
título oficial e a ementa. Exige disfarce leve (User-Agent de navegador +
HTTP/1.1): com UA de curl o WAF corta a conexão.

PAPEL: fonte SECUNDÁRIA de detecção quando o Inlabs pisca ("vaga-lume",
diagnóstico do dono em 06/08/2026). NUNCA dá baixa no dia — confirmação
final e nota técnica continuam com o Inlabs. O que o portal resolve é o
pior modo de falha da janela (ficar CEGO): MP publicada é avisada na hora
mesmo com o Inlabs caído, e "sem MP" vira afirmação com evidência (edição
confirmada no índice) em vez de "não consegui checar".

É scraping de HTML sem contrato: qualquer mudança de layout estoura
PortalError ALTO (bloco ausente, título não parseável) — nunca lista vazia
silenciosa, que aqui seria falso negativo de MP.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date

import httpx

logger = logging.getLogger(__name__)

BUSCA_URL = "https://www.in.gov.br/consulta/-/buscar/dou"
MATERIA_URL = "https://www.in.gov.br/web/dou/-/{url_title}"

# WAF do portal (medido 06/08/2026): UA de curl → conexão cortada; HTTP/2
# via proxy → PROTOCOL_ERROR. Navegador + HTTP/1.1 (default do httpx) passa.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9",
}
_TIMEOUT = 20.0
# Teto de itens por página da busca. Página CHEIA = pode haver mais matérias
# além do corte → não dá pra afirmar nada (vira PortalError, dia pendente).
_DELTA = 60
# Ementas buscadas por dia (1 GET por MP). Dia normal tem 0-3 MPs; o teto só
# protege contra resposta absurda do índice.
_MAX_EMENTAS = 5


class PortalError(Exception):
    pass


@dataclass(frozen=True)
class PortalMP:
    numero: str
    ano: str
    titulo: str
    ementa: str | None
    url: str


@dataclass(frozen=True)
class PortalDia:
    mps: list
    # True = houve edição indexada na data (com ou sem MP). False = índice
    # vazio pra data — NÃO afirma "sem edição": pode ser atraso do índice.
    edicao_confirmada: bool


_PARAMS_RE = re.compile(
    r'_params"\s+type="application/json">\s*(\{.*?\})\s*</script>', re.DOTALL,
)
_TAGS_RE = re.compile(r"<[^>]+>")
# Identidade da MP vem do TÍTULO dela (regra do projeto): número e ANO DO
# TÍTULO — MP assinada em 31/12 sai no DOU de 01/01 e o ano errado quebra
# dedup, Planalto e conferência.
_MP_TITULO_RE = re.compile(
    r"MEDIDA\s+PROVIS[ÓO]RIA\s+N[ºO°]\s*([\d.]+)\s*,?\s*DE\b.*?\bDE\s*(\d{4})",
    re.IGNORECASE | re.DOTALL,
)
_EMENTA_RE = re.compile(r'<p class="ementa"[^>]*>(.*?)</p>', re.DOTALL)


def _fmt_data(d: date) -> str:
    return d.strftime("%d-%m-%Y")


async def _buscar(client: httpx.AsyncClient, q: str, d: date, secao: str = "todos") -> list[dict]:
    params = {
        "q": q,
        "s": secao,
        "exactDate": "personalizado",
        "publishFrom": _fmt_data(d),
        "publishTo": _fmt_data(d),
        "sortType": "0",
        "delta": str(_DELTA),
    }
    try:
        resp = await client.get(BUSCA_URL, params=params)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise PortalError(f"busca no portal falhou: {e}") from e
    m = _PARAMS_RE.search(resp.text)
    if not m:
        # Layout mudou (ou WAF serviu outra página): erro ALTO, nunca
        # "0 resultados" — que aqui viraria falso "sem MP".
        raise PortalError("portal sem o bloco de resultados (layout mudou?)")
    try:
        return json.loads(m.group(1)).get("jsonArray") or []
    except ValueError as e:
        raise PortalError(f"JSON do portal ilegível: {e}") from e


async def _ementa_da_materia(client: httpx.AsyncClient, url_title: str) -> str | None:
    """Best-effort: ementa ausente não bloqueia a detecção (o aviso sai com o
    título oficial no lugar)."""
    if not url_title:
        return None
    try:
        resp = await client.get(MATERIA_URL.format(url_title=url_title))
        resp.raise_for_status()
    except httpx.HTTPError:
        logger.warning("portal: página da matéria indisponível (%s)", url_title)
        return None
    m = _EMENTA_RE.search(resp.text)
    return _TAGS_RE.sub("", m.group(1)).strip() if m else None


async def checar_dia_portal(d: date) -> PortalDia:
    """Responde 'houve MP publicada no DOU de `d`?' pelo portal público.

    Duas consultas: a de MPs e — quando vem vazia — uma sonda genérica que
    prova que a EDIÇÃO do dia está no índice (portaria/despacho existem em
    praticamente toda edição do DO1). Edição confirmada + 0 MPs = evidência
    positiva de "sem MP até agora"; índice vazio = inconclusivo (o caller
    mantém a pendência — na dúvida, é pendência)."""
    async with httpx.AsyncClient(
        timeout=_TIMEOUT, headers=_HEADERS, follow_redirects=True,
    ) as client:
        itens = await _buscar(client, '"MEDIDA PROVISÓRIA"', d)
        if len(itens) >= _DELTA:
            raise PortalError(
                f"busca no limite de paginação ({len(itens)} itens) — "
                "resultado pode estar cortado"
            )
        mps: list[PortalMP] = []
        vistos: set[tuple[str, str]] = set()
        for it in itens:
            if (it.get("artType") or "").strip().lower() != "medida provisória":
                continue
            titulo = _TAGS_RE.sub("", it.get("title") or "").strip()
            m = _MP_TITULO_RE.search(titulo)
            if not m:
                # MP detectada sem identidade parseável não pode sumir calada.
                raise PortalError(f"título de MP não parseável: {titulo!r}")
            numero, ano = m.group(1), m.group(2)
            if (numero, ano) in vistos:
                continue
            vistos.add((numero, ano))
            ementa = None
            if len(mps) < _MAX_EMENTAS:
                ementa = await _ementa_da_materia(client, it.get("urlTitle") or "")
            mps.append(PortalMP(
                numero=numero, ano=ano, titulo=titulo, ementa=ementa,
                url=MATERIA_URL.format(url_title=it.get("urlTitle") or ""),
            ))
        if mps:
            logger.info("portal DOU: %d MP(s) em %s", len(mps), d.isoformat())
            return PortalDia(mps, True)
        # 0 MPs: edição existe no índice? (separa "sem MP" de "índice fora")
        for sonda in ("portaria", "despacho"):
            if await _buscar(client, sonda, d, secao="do1"):
                return PortalDia([], True)
        return PortalDia([], False)
