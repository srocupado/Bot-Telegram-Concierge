"""Sessões de cinema (horários) via API da Cinemark (bff-api).

Descoberto por engenharia reversa do site (a página é SPA e não expõe os
horários no HTML; eles vêm desta API client-side). Fluxo:

  1. resolve o cinema: diretório de TODA a rede, montado da própria API em
     runtime (states → cities?stateId → theaters?cityId) e cacheado em memória.
     Sem arquivo em disco (o antigo cinemark_theaters.json caía no .gitignore e
     nunca ia pro deploy — a tool resolvia vazio e caía sempre pra web).
  2. lista filmes em cartaz no cinema (/v1/movies/onDisplayByTheater) e casa
     o título pedido → movieId (já garante que o filme está em cartaz ali).
  3. busca as sessões do dia (/v1/ticketTypes/priceTable, que apesar do nome
     devolve as sessões com horário/áudio/tecnologia).

A API passa pelo WAF com headers de browser (Origin/Referer). JSON puro, sem
navegador headless. Só Cinemark BR. Falhas viram CinemaError (o handler traduz).
"""
from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
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


class CinemaError(Exception):
    pass


def _norm(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", (s or "").casefold())
        if not unicodedata.combining(c)
    )


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


# ── Diretório on-demand ──────────────────────────────────────────────────────
# A API da Cinemark é estritamente state → city → theater: não há endpoint de
# 'todos os teatros' (geo/bulk não funcionam) e cities?stateId devolve TODOS os
# ~8200 municípios, não só os com Cinemark. Varrer tudo é inviável. Mas listar
# as CIDADES custa só ~25 chamadas (1 por estado, <1s). Então: cacheio as
# cidades (baratas), acho a cidade citada no texto, e busco os teatros SÓ
# daquela(s) cidade(s) sob demanda (cache por cidade). Cobre a rede inteira sem
# arquivo em disco e sem varredura cara.
_CITIES_CACHE: list[dict] = []
_CITIES_AT: datetime | None = None
_CITIES_TTL = timedelta(hours=12)
_CITIES_LOCK = asyncio.Lock()
_THEATERS_BY_CITY: dict[int, list[dict]] = {}
_DIR_CONCURRENCY = 6
_CITY_CONNECTORS = {"de", "do", "da", "dos", "das", "e", "d"}
_CIDADE_PADRAO = "Brasília"  # bot é de Brasília; sem cidade citada, assume aqui

# Palavras genéricas que não ajudam a desambiguar um cinema.
_STOP = {"cinemark", "shopping", "cinema", "em", "de", "do", "da", "no", "na",
         "center", "centro", "mall", "plaza"}


async def _ensure_cities(client: httpx.AsyncClient) -> list[dict]:
    """Lista de cidades de toda a rede (1 chamada/estado), cacheada. Cada item:
    {id, name, tokens, state, uf}. Barato (~25 chamadas)."""
    global _CITIES_CACHE, _CITIES_AT
    now = datetime.now(timezone.utc)
    if _CITIES_CACHE and _CITIES_AT and now - _CITIES_AT < _CITIES_TTL:
        return _CITIES_CACHE
    async with _CITIES_LOCK:
        now = datetime.now(timezone.utc)
        if _CITIES_CACHE and _CITIES_AT and now - _CITIES_AT < _CITIES_TTL:
            return _CITIES_CACHE
        states = await _get(client, "/v1/states", {})
        states = states if isinstance(states, list) else []
        sem = asyncio.Semaphore(_DIR_CONCURRENCY)

        async def _cities(state: dict) -> list[dict]:
            async with sem:
                try:
                    cs = await _get(client, "/v1/cities", {"stateId": state["id"]})
                except CinemaError:
                    return []
            out = []
            for c in (cs if isinstance(cs, list) else []):
                toks = frozenset(
                    t for t in _norm(c.get("name") or "").split()
                    if t and t not in _CITY_CONNECTORS
                )
                if not toks or c.get("id") is None:
                    continue
                out.append({"id": c["id"], "name": c.get("name") or "", "tokens": toks,
                            "state": state.get("name") or "", "uf": state.get("code") or ""})
            return out

        nested = await asyncio.gather(*(_cities(s) for s in states))
        cities = [c for cl in nested for c in cl]
        if cities:
            _CITIES_CACHE, _CITIES_AT = cities, datetime.now(timezone.utc)
        return _CITIES_CACHE


def _candidate_cities(texto: str, cities: list[dict]) -> list[dict]:
    """Cidades cujo nome está INTEIRO no texto (todos os tokens do nome
    presentes) — evita casar 'são' sozinho com centenas de municípios. Mais
    específicas primeiro; no máx. 8 (limita chamadas de teatro)."""
    q = frozenset(t for t in _norm(texto).split() if t and t not in _STOP)
    if not q:
        return []
    matches = [c for c in cities if c["tokens"] <= q]
    matches.sort(key=lambda c: (-len(c["tokens"]), c["name"]))
    return matches[:8]


async def _theaters_for_cities(client: httpx.AsyncClient, cities: list[dict]) -> list[dict]:
    """Teatros das cidades candidatas (cache por cityId), agrupados e dedup."""
    sem = asyncio.Semaphore(_DIR_CONCURRENCY)

    async def _fetch(city: dict) -> list[dict]:
        cid = city["id"]
        if cid in _THEATERS_BY_CITY:
            return _THEATERS_BY_CITY[cid]
        async with sem:
            try:
                ts = await _get(client, "/v1/theaters", {"cityId": cid})
            except CinemaError:
                return []
        out = []
        for t in (ts if isinstance(ts, list) else []):
            tid = t.get("code")
            if tid is None:
                continue
            out.append({"id": tid, "name": t.get("name") or "",
                        "city": t.get("city") or "", "state": t.get("state") or ""})
        _THEATERS_BY_CITY[cid] = out
        return out

    nested = await asyncio.gather(*(_fetch(c) for c in cities))
    seen: dict[int, dict] = {}
    for tl in nested:
        for t in tl:
            seen[t["id"]] = t
    return list(seen.values())


async def _resolve_theaters(client: httpx.AsyncClient, texto: str) -> list[dict]:
    """Texto → candidatos de cinema. Regra: o padrão é SEMPRE Brasília; só sai
    de Brasília se outra cidade COM Cinemark for explicitamente nomeada no texto
    (ex.: 'Eldorado São Paulo'). Assim 'no Cinemark do Iguatemi Shopping' (sem
    cidade) resolve em Brasília — mesmo 'Iguatemi' sendo também um município
    (Iguatemi/MS, sem Cinemark)."""
    cities = await _ensure_cities(client)
    # cidade(s) explicitamente citada(s) que TÊM Cinemark têm prioridade
    cand = _candidate_cities(texto, cities)
    if cand:
        theaters = await _theaters_for_cities(client, cand)
        if theaters:
            return resolver_cinema(texto, theaters)
    # padrão: Brasília
    bsb = _candidate_cities(_CIDADE_PADRAO, cities)
    theaters = await _theaters_for_cities(client, bsb) if bsb else []
    return resolver_cinema(texto, theaters)


def resolver_cinema(texto: str, theaters: list[dict]) -> list[dict]:
    """Casa o texto do usuário (nome/cidade) com os teatros dados. Retorna os
    candidatos de score máximo (melhor primeiro); vazio se nada bate. Mais de um
    = ambiguidade real (ex.: 'Iguatemi' em mais de uma cidade citada) → pergunta."""
    q = {t for t in _norm(texto).split() if t and t not in _STOP}
    if not q:
        return []
    scored: list[tuple[int, dict]] = []
    for th in theaters:
        hay = set(_norm(f"{th['name']} {th['city']} {th['state']}").split())
        score = len(q & hay)
        if score:
            scored.append((score, th))
    scored.sort(key=lambda x: (-x[0], x[1]["name"]))
    best = scored[0][0] if scored else 0
    return [th for s, th in scored if s == best]


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

# Dias da semana normalizados (sem acento) → weekday() (segunda=0).
_WEEKDAYS = {
    "segunda": 0, "terca": 1, "quarta": 2, "quinta": 3,
    "sexta": 4, "sabado": 5, "domingo": 6,
}
_DIAS_SEMANA = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]


def _parse_data_pt(query: str, hoje: date) -> date:
    """Extrai uma data do texto cru: 'hoje', 'amanhã', 'depois de amanhã', dia
    da semana ('terça'), 'dia 23', '23/06'. Default: hoje. Não confunde o filme
    'Dia D' com data ('dia N' exige número)."""
    n = _norm(query)
    m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", n)  # DD/MM[/AAAA]
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        yr = int(m.group(3)) if m.group(3) else hoje.year
        if yr < 100:
            yr += 2000
        try:
            cand = date(yr, mo, d)
            if not m.group(3) and cand < hoje:  # sem ano e já passou → próximo ano
                cand = date(yr + 1, mo, d)
            return cand
        except ValueError:
            pass
    if "depois de amanha" in n:
        return hoje + timedelta(days=2)
    if "amanha" in n:
        return hoje + timedelta(days=1)
    if "hoje" in n:
        return hoje
    m = re.search(r"\bdia (\d{1,2})\b", n)
    if m:
        d = int(m.group(1))
        for bump in (0, 1):  # neste mês; se já passou, no mês seguinte
            y, mo = hoje.year, hoje.month
            if bump:
                y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
            try:
                cand = date(y, mo, d)
            except ValueError:
                break
            if cand >= hoje:
                return cand
    for name, wd in _WEEKDAYS.items():
        if re.search(rf"\b{name}\b", n):
            return hoje + timedelta(days=(wd - hoje.weekday()) % 7)
    return hoje


def _match_movie(em_cartaz: list[dict], texto: str, extra_stop=frozenset()) -> dict | None:
    """Melhor filme em cartaz que casa com o texto (match por tokens, ignorando
    conectores/ruído e os tokens do próprio cinema em `extra_stop`). Retorna None
    se nada casa OU se há empate no topo (ambíguo) — evita escolher o filme
    errado por causa de 1 token solto."""
    stop = _MOVIE_STOP | extra_stop
    q = {t for t in _norm(texto).split() if t and t not in stop}
    melhor = None
    best = second = 0
    for m in em_cartaz:
        mt = {t for t in _norm(m.get("name") or "").split() if t not in stop}
        sc = len(q & mt)
        if sc > best:
            best, second, melhor = sc, best, m
        elif sc > second:
            second = sc
    if best == 0 or best == second:
        return None
    return melhor


def _quando_label(d: date, hoje: date) -> str:
    """'hoje, 21/06' / 'amanhã, 22/06' / 'terça, 23/06' / '30/06' (datas longe)."""
    delta = (d - hoje).days
    if delta == 0:
        quando = "hoje"
    elif delta == 1:
        quando = "amanhã"
    elif 2 <= delta <= 6:
        quando = _DIAS_SEMANA[d.weekday()]
    else:
        quando = None
    data_br = d.strftime("%d/%m")
    return f"{quando}, {data_br}" if quando else data_br


def _group_sessions(sessoes: list[dict]) -> list[str]:
    """Linhas '• {tech} {aud}: h, h' agrupadas por tecnologia/áudio. [] se vazio."""
    grupos: dict[tuple[str, str], list[str]] = {}
    for s in sessoes:
        tech = " ".join(_FEATURES.get(f, str(f)) for f in (s.get("features") or [])) or "—"
        aud = _AUDIO.get(s.get("audio"), str(s.get("audio")))
        grupos.setdefault((tech, aud), []).append(_fmt_hhmm(s.get("time") or ""))
    linhas = []
    for (tech, aud), horas in sorted(grupos.items()):
        horas = sorted(h for h in horas if h)
        if horas:
            linhas.append(f"• {tech} {aud}: {', '.join(horas)}")
    return linhas


async def _programacao(
    client: httpx.AsyncClient, th: dict, em_cartaz: list[dict], d: date, hoje: date,
) -> str:
    """Programação completa do cinema na data: TODOS os filmes em cartaz com as
    sessões do dia (1 priceTable por filme, concorrente)."""
    sem = asyncio.Semaphore(_DIR_CONCURRENCY)

    async def _sess(m: dict) -> tuple[str, list[dict]]:
        async with sem:
            try:
                res = await _get(client, "/v1/ticketTypes/priceTable",
                                 {"movieId": m["id"], "theaterId": th["id"],
                                  "sessionDate": d.isoformat()})
            except CinemaError:
                return (m.get("name") or "?", [])
        res = res if isinstance(res, dict) else {}
        return (m.get("name") or "?", res.get("sessions") or [])

    results = await asyncio.gather(*(_sess(m) for m in em_cartaz))
    linhas = [f"🎬 Programação — Cinemark {th['name']} ({th['city']}/{_uf(th['state'])})",
              f"📅 {_quando_label(d, hoje)}", ""]
    sem_sessao, teve = [], False
    for nome, sessoes in results:
        grupos = _group_sessions(sessoes)
        if not grupos:
            sem_sessao.append(nome)
            continue
        teve = True
        linhas.append(nome)
        linhas.extend(grupos)
        linhas.append("")
    if not teve:
        linhas.append("(nenhum filme com sessão nessa data)")
    elif sem_sessao:
        linhas.append("Sem sessões nessa data: " + ", ".join(sem_sessao))
    return "\n".join(linhas).strip()


async def _sessions_for(
    client: httpx.AsyncClient, th: dict, filme_text: str, data_iso: str | None, tz: str,
) -> str:
    """Sessões da data no cinema `th`. Com filme casado em `filme_text` → sessões
    dele; sem filme identificado (ex.: pediu a 'programação', ou o filme citado
    não está em cartaz) → TODOS os filmes em cartaz com horários. Levanta
    CinemaError se a API não devolver a programação (o caller decide o fallback)."""
    try:
        d = date.fromisoformat(data_iso) if data_iso else datetime.now(ZoneInfo(tz)).date()
    except ValueError:
        return f"erro: data inválida ({data_iso!r}). Use AAAA-MM-DD."
    hoje = datetime.now(ZoneInfo(tz)).date()
    # tokens do próprio cinema (nome/cidade/UF) não podem casar como filme
    th_stop = frozenset(t for t in _norm(f"{th['name']} {th['city']} {th['state']}").split() if t)

    em_cartaz = await _get(client, "/v1/movies/onDisplayByTheater",
                           {"theaterId": th["id"], "pageNumber": 1, "pageSize": 40})
    em_cartaz = em_cartaz if isinstance(em_cartaz, list) else []
    if not em_cartaz:
        raise CinemaError(f"sem programação retornada para {th['name']}")
    melhor = _match_movie(em_cartaz, filme_text, extra_stop=th_stop)
    if not melhor:
        return await _programacao(client, th, em_cartaz, d, hoje)

    res = await _get(client, "/v1/ticketTypes/priceTable",
                     {"movieId": melhor["id"], "theaterId": th["id"],
                      "sessionDate": d.isoformat()})
    res = res if isinstance(res, dict) else {}
    nome_filme = res.get("movieName") or melhor.get("name") or filme_text
    cab = (f"🎬 {nome_filme} — Cinemark {th['name']} ({th['city']}/{_uf(th['state'])})\n"
           f"📅 {_quando_label(d, hoje)}")
    grupos = _group_sessions(res.get("sessions") or [])
    if not grupos:
        return cab + "\n\n(sem sessões nessa data)"
    return "\n".join([cab, ""] + grupos)


async def consultar_sessoes(
    filme: str, cinema: str, data_iso: str | None, tz: str = "America/Sao_Paulo",
) -> str:
    """Horários no cinema (usado pela tool do agente). Com `filme` → sessões
    dele; `filme` vazio → programação completa do cinema. Texto pronto pro chat."""
    filme = (filme or "").strip()
    cinema = (cinema or "").strip()
    if not cinema:
        return "erro: informe o cinema (ex: 'Iguatemi Brasília')"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        cands = await _resolve_theaters(client, cinema)
        if not cands:
            return ("Não achei esse cinema na rede Cinemark. "
                    "Tente pelo nome do shopping + cidade (ex: 'Iguatemi Brasília').")
        if len(cands) > 1:
            nomes = "; ".join(f"{c['name']} ({c['city']})" for c in cands[:6])
            return f"Tem mais de um cinema parecido — qual? {nomes}"
        return await _sessions_for(client, cands[0], filme, data_iso, tz)


async def consultar_sessoes_texto(
    query: str, data_iso: str | None = None, tz: str = "America/Sao_Paulo",
) -> str | None:
    """Igual à consultar_sessoes, mas extrai cinema E filme do texto cru por
    tokens, sem LLM — pro /buscar e voz (ex: 'horário da sessão do filme Mestres
    do Universo no Cinemark do Iguatemi Shopping em Brasília').

    Retorna None quando NÃO dá pra atender pela Cinemark — cinema fora da rede,
    ou a API falhou — sinalizando ao caller pra cair na busca web. Retorna texto
    quando há resposta Cinemark útil (sessões, desambiguação de cinema, ou filme
    não identificado no cinema)."""
    query = (query or "").strip()
    if not query:
        return None
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            cands = await _resolve_theaters(client, query)
            if not cands:
                return None  # sem cidade reconhecível ou sem match → deixa a web tentar
            if len(cands) > 1:
                nomes = "; ".join(f"{c['name']} ({c['city']})" for c in cands[:6])
                return f"Tem mais de um cinema parecido — qual? {nomes}"
            if data_iso is None:  # sem LLM: extrai 'amanhã'/dia/data do próprio texto
                hoje = datetime.now(ZoneInfo(tz)).date()
                data_iso = _parse_data_pt(query, hoje).isoformat()
            return await _sessions_for(client, cands[0], query, data_iso, tz)
    except CinemaError:
        return None  # API da Cinemark falhou → a web pode resolver


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
