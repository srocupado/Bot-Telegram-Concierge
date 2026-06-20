"""Sessões de cinema (horários) via API da Cinemark (bff-api).

Descoberto por engenharia reversa do site (a página é SPA e não expõe os
horários no HTML; eles vêm desta API client-side). Fluxo:

  1. resolve o cinema: diretório estático nome/cidade → theaterId
     (bot/data/cinemark_theaters.json, levantado da própria API).
  2. lista filmes em cartaz no cinema (/v1/movies/onDisplayByTheater) e casa
     o título pedido → movieId (evita um endpoint de busca à parte e já
     garante que o filme está em cartaz ali).
  3. busca as sessões do dia (/v1/ticketTypes/priceTable, que apesar do nome
     devolve as sessões com horário/áudio/tecnologia).

A API passa pelo WAF com headers de browser (Origin/Referer). JSON puro, sem
navegador headless. Só Cinemark BR. Falhas viram CinemaError (o handler traduz).
"""
from __future__ import annotations

import json
import logging
import unicodedata
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

_API = "https://br-www-frontend-ext-prod.cinemark.com.br/bff-api"
# Headers de browser: a API fica atrás de WAF e recusa requests "crus".
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 14) Chrome/126 Mobile Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.cinemark.com.br",
    "Referer": "https://www.cinemark.com.br/",
    "Accept-Language": "pt-BR,pt;q=0.9",
}
_TIMEOUT = 20.0

# Enums decodificados do bundle JS do site.
_FEATURES = {1: "D-BOX", 2: "XD", 3: "Imax", 4: "Ingresso Azul",
             5: "Prime", 6: "3D", 7: "2D", 9: "Cine Materna"}
_AUDIO = {10: "Original", 20: "Dublado", 30: "Legendado"}

_DIR_PATH = Path(__file__).resolve().parent.parent / "data" / "cinemark_theaters.json"


class CinemaError(Exception):
    pass


def _norm(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", (s or "").casefold())
        if not unicodedata.combining(c)
    )


@lru_cache(maxsize=1)
def _theaters() -> list[dict]:
    try:
        return json.loads(_DIR_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("cinema: falha lendo diretório de teatros")
        return []


# Palavras genéricas que não ajudam a desambiguar um cinema.
_STOP = {"cinemark", "shopping", "cinema", "em", "de", "do", "da", "no", "na",
         "center", "centro", "mall", "plaza"}


def resolver_cinema(texto: str) -> list[dict]:
    """Casa o texto do usuário (nome/cidade) com o diretório. Retorna a lista
    de candidatos ordenada por score (melhor primeiro); vazia se nada bate."""
    q = {t for t in _norm(texto).split() if t and t not in _STOP}
    if not q:
        return []
    scored: list[tuple[int, dict]] = []
    for th in _theaters():
        hay = set(_norm(f"{th['name']} {th['city']} {th['state']}").split())
        score = len(q & hay)
        if score:
            scored.append((score, th))
    scored.sort(key=lambda x: (-x[0], x[1]["name"]))
    best = scored[0][0] if scored else 0
    # mantém só os de score máximo (desambiguação real fica pro usuário)
    return [th for s, th in scored if s == best]


async def _get(client: httpx.AsyncClient, path: str, params: dict) -> object:
    try:
        r = await client.get(f"{_API}{path}", params=params, headers=_HEADERS)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise CinemaError(f"falha na API da Cinemark: {e}") from e
    try:
        data = r.json()
    except ValueError as e:
        raise CinemaError("resposta inesperada da Cinemark") from e
    if isinstance(data, dict) and data.get("success") is False:
        raise CinemaError(data.get("messageError") or "erro da API da Cinemark")
    return (data or {}).get("dataResult") if isinstance(data, dict) else data


def _fmt_hhmm(t: str) -> str:
    return (t or "")[:5].replace(":", "h")


# Palavras que não ajudam a casar o TÍTULO do filme (conectores + ruído de
# pedido tipo 'horário da sessão do filme ...').
_MOVIE_STOP = _STOP | {
    "o", "a", "os", "as", "dos", "das", "e", "um", "uma", "nos", "nas",
    "filme", "sessao", "sessoes", "horario", "horarios", "que", "horas",
    "passa", "passando", "programacao", "hoje", "amanha", "qual", "ver",
    "ingresso", "ingressos", "para", "pra", "tem",
}


def _match_movie(em_cartaz: list[dict], texto: str) -> dict | None:
    """Melhor filme em cartaz que casa com o texto (match por tokens, ignorando
    conectores/ruído). None se nada bate."""
    q = {t for t in _norm(texto).split() if t and t not in _MOVIE_STOP}
    melhor, melhor_score = None, 0
    for m in em_cartaz:
        mt = {t for t in _norm(m.get("name") or "").split() if t not in _MOVIE_STOP}
        sc = len(q & mt)
        if sc > melhor_score:
            melhor, melhor_score = m, sc
    return melhor


def _pick_theater(texto: str):
    """Teatro resolvido a partir do texto, ou string de erro/desambiguação."""
    cands = resolver_cinema(texto)
    if not cands:
        return ("Não achei o cinema no diretório Cinemark. "
                "Tente pelo nome do shopping + cidade (ex: 'Iguatemi Brasília').")
    if len(cands) > 1:
        nomes = "; ".join(f"{c['name']} ({c['city']})" for c in cands[:6])
        return f"Tem mais de um cinema parecido — qual? {nomes}"
    return cands[0]


async def _sessions_for(th: dict, filme_text: str, data_iso: str | None, tz: str) -> str:
    """Casa o filme (de `filme_text`) em cartaz no cinema `th`, busca as sessões
    da data e formata."""
    try:
        d = date.fromisoformat(data_iso) if data_iso else datetime.now(ZoneInfo(tz)).date()
    except ValueError:
        return f"erro: data inválida ({data_iso!r}). Use AAAA-MM-DD."
    hoje = datetime.now(ZoneInfo(tz)).date()

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        # 1) filmes em cartaz no cinema → casa o título → movieId
        em_cartaz = await _get(client, "/v1/movies/onDisplayByTheater",
                               {"theaterId": th["id"], "pageNumber": 1, "pageSize": 40})
        em_cartaz = em_cartaz if isinstance(em_cartaz, list) else []
        if not em_cartaz:
            return f"Não consegui a programação do {th['name']} agora. Tente mais tarde."
        melhor = _match_movie(em_cartaz, filme_text)
        if not melhor:
            lista = ", ".join(m.get("name", "?") for m in em_cartaz[:10])
            return f"Não identifiquei o filme no {th['name']}. Em cartaz: {lista}."

        # 2) sessões do dia (priceTable devolve os horários)
        res = await _get(client, "/v1/ticketTypes/priceTable",
                         {"movieId": melhor["id"], "theaterId": th["id"],
                          "sessionDate": d.isoformat()})

    res = res if isinstance(res, dict) else {}
    sessoes = res.get("sessions") or []
    nome_filme = res.get("movieName") or melhor.get("name") or filme_text
    quando = "hoje" if d == hoje else d.strftime("%d/%m")
    cab = (f"🎬 {nome_filme} — Cinemark {th['name']} ({th['city']}/{_uf(th['state'])})\n"
           f"📅 {quando}, {d.strftime('%d/%m')}")

    if not sessoes:
        return cab + "\n\n(sem sessões nessa data)"

    # agrupa por (tecnologia, áudio)
    grupos: dict[tuple[str, str], list[str]] = {}
    for s in sessoes:
        tech = " ".join(_FEATURES.get(f, str(f)) for f in (s.get("features") or [])) or "—"
        aud = _AUDIO.get(s.get("audio"), str(s.get("audio")))
        grupos.setdefault((tech, aud), []).append(_fmt_hhmm(s.get("time") or ""))

    linhas = [cab, ""]
    for (tech, aud), horas in sorted(grupos.items()):
        horas = sorted(h for h in horas if h)
        linhas.append(f"• {tech} {aud}: {', '.join(horas)}")
    return "\n".join(linhas)


async def consultar_sessoes(
    filme: str, cinema: str, data_iso: str | None, tz: str = "America/Sao_Paulo",
) -> str:
    """Horários de um filme num cinema (filme e cinema já separados — usado pela
    tool do agente). Texto pronto pra repassar no chat."""
    filme = (filme or "").strip()
    cinema = (cinema or "").strip()
    if not filme or not cinema:
        return "erro: informe o filme e o cinema (ex: 'Mestres do Universo no Iguatemi Brasília')"
    th = _pick_theater(cinema)
    if isinstance(th, str):
        return th
    return await _sessions_for(th, filme, data_iso, tz)


async def consultar_sessoes_texto(
    query: str, data_iso: str | None = None, tz: str = "America/Sao_Paulo",
) -> str:
    """Igual à consultar_sessoes, mas extrai cinema E filme do texto cru por
    tokens, sem LLM — pro /buscar e voz (ex: 'horário da sessão do filme Mestres
    do Universo no Cinemark do Iguatemi Shopping em Brasília')."""
    query = (query or "").strip()
    if not query:
        return "erro: pedido vazio"
    th = _pick_theater(query)
    if isinstance(th, str):
        return th
    return await _sessions_for(th, query, data_iso, tz)


# UF a partir do nome do estado (pro cabeçalho ficar curto).
_UF = {
    "acre": "AC", "alagoas": "AL", "amapa": "AP", "amazonas": "AM", "bahia": "BA",
    "ceara": "CE", "distrito federal": "DF", "espirito santo": "ES", "goias": "GO",
    "maranhao": "MA", "mato grosso": "MT", "mato grosso do sul": "MS",
    "minas gerais": "MG", "para": "PA", "paraiba": "PB", "parana": "PR",
    "pernambuco": "PE", "piaui": "PI", "rio de janeiro": "RJ",
    "rio grande do norte": "RN", "rio grande de norte": "RN",
    "rio grande do sul": "RS", "rondonia": "RO", "roraima": "RR",
    "santa catarina": "SC", "sao paulo": "SP", "sergipe": "SE", "tocantins": "TO",
}


def _uf(estado: str) -> str:
    return _UF.get(_norm(estado), estado)
