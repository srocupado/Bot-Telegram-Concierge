"""Pauta de comissões da Câmara dos Deputados via API de Dados Abertos.

Grátis, sem chave (https://dadosabertos.camara.leg.br/api/v2). Fluxo:
  1. resolve a comissão pelo texto (sigla/nome) — diretório das comissões
     permanentes, cacheado;
  2. reuniões (eventos) da comissão na data;
  3. pauta de cada reunião → proposições;
  4. autor + partido de cada proposição (mapa deputado→partido cacheado),
     com filtro opcional por partido e/ou deputado.

Tudo dado oficial — o agente não inventa: se não houver pauta/reunião, diz isso.
"""
from __future__ import annotations

import asyncio
import re
import unicodedata
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

_API = "https://dadosabertos.camara.leg.br/api/v2"
_HEADERS = {"Accept": "application/json", "User-Agent": "Bot-Telegram-Concierge/1.0"}
_TIMEOUT = 20.0
_CONCURRENCY = 8


class CamaraError(Exception):
    pass


def _norm(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", (s or "").casefold())
        if not unicodedata.combining(c)
    )


async def _get(client: httpx.AsyncClient, path: str, params: dict | None = None) -> list:
    try:
        r = await client.get(f"{_API}{path}", params=params or {}, headers=_HEADERS)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise CamaraError(f"API da Câmara falhou: {e}") from e
    return (r.json() or {}).get("dados") or []


# ── Cache: comissões permanentes + mapa deputado→partido ─────────────────────
_COMISSOES: list[dict] = []
_COMISSOES_AT: datetime | None = None
_DEPUTADOS: dict[int, dict] = {}
_DEPUTADOS_AT: datetime | None = None
_TTL = timedelta(hours=12)
_LOCK = asyncio.Lock()

# Conectores que não ajudam a casar o nome da comissão.
_STOP = {"comissao", "de", "do", "da", "dos", "das", "e", "a", "o", "as", "os",
         "camara", "deputados", "na", "no", "em", "pauta", "reuniao", "reuniões"}


async def _ensure_comissoes(client: httpx.AsyncClient) -> list[dict]:
    global _COMISSOES, _COMISSOES_AT
    now = datetime.now()
    if _COMISSOES and _COMISSOES_AT and now - _COMISSOES_AT < _TTL:
        return _COMISSOES
    async with _LOCK:
        if _COMISSOES and _COMISSOES_AT and datetime.now() - _COMISSOES_AT < _TTL:
            return _COMISSOES
        # codTipoOrgao=2 = Comissão Permanente (as ~30 comissões temáticas).
        dados = await _get(client, "/orgaos", {"codTipoOrgao": 2, "itens": 100})
        out = []
        for o in dados:
            nome = o.get("nome") or ""
            sigla = o.get("sigla") or ""
            toks = {t for t in _norm(f"{sigla} {nome}").split() if t and t not in _STOP}
            out.append({"id": o["id"], "sigla": sigla, "nome": nome, "tokens": toks})
        if out:
            _COMISSOES, _COMISSOES_AT = out, datetime.now()
        return _COMISSOES


async def _ensure_deputados(client: httpx.AsyncClient) -> dict[int, dict]:
    global _DEPUTADOS, _DEPUTADOS_AT
    now = datetime.now()
    if _DEPUTADOS and _DEPUTADOS_AT and now - _DEPUTADOS_AT < _TTL:
        return _DEPUTADOS
    async with _LOCK:
        if _DEPUTADOS and _DEPUTADOS_AT and datetime.now() - _DEPUTADOS_AT < _TTL:
            return _DEPUTADOS
        mapa: dict[int, dict] = {}
        pagina = 1
        while pagina <= 5:  # ~513 deputados / 200 por página = 3 páginas
            dados = await _get(client, "/deputados", {"itens": 200, "pagina": pagina})
            if not dados:
                break
            for d in dados:
                mapa[d["id"]] = {
                    "nome": d.get("nome") or "",
                    "partido": d.get("siglaPartido") or "",
                    "uf": d.get("siglaUf") or "",
                }
            if len(dados) < 200:
                break
            pagina += 1
        if mapa:
            _DEPUTADOS, _DEPUTADOS_AT = mapa, datetime.now()
        return _DEPUTADOS


def resolver_comissao(texto: str, comissoes: list[dict]) -> list[dict]:
    """Casa o texto (sigla/nome) com as comissões. Sigla exata ganha; senão,
    score por tokens do nome. Vazio se nada; >1 = ambiguidade."""
    n = _norm(texto)
    toks_texto = set(n.split())
    # 1) sigla exata no texto (ex.: 'CSAUDE')
    exatas = [c for c in comissoes if _norm(c["sigla"]) in toks_texto]
    if len(exatas) == 1:
        return exatas
    # 2) sigla por prefixo, p/ abreviações comuns ('CCJ' → 'CCJC'); token ≥ 3
    pref = [c for c in comissoes
            if any(len(t) >= 3 and _norm(c["sigla"]).startswith(t) for t in toks_texto)]
    if len(pref) == 1:
        return pref
    # 3) tokens do nome
    q = {t for t in n.split() if t and t not in _STOP}
    if not q:
        return exatas or pref
    scored = []
    for c in comissoes:
        score = len(q & c["tokens"])
        if score:
            scored.append((score, c))
    scored.sort(key=lambda x: (-x[0], x[1]["nome"]))
    best = scored[0][0] if scored else 0
    return [c for s, c in scored if s == best]


# ── Partido: aliases comuns → como vem em siglaPartido ───────────────────────
_PARTIDO_ALIAS = {
    "podemos": "PODE", "pode": "PODE",
    "republicanos": "REPUBLICANOS", "republic": "REPUBLICANOS",
    "uniao": "UNIÃO", "uniaobrasil": "UNIÃO", "uniao brasil": "UNIÃO",
    "solidariedade": "SOLIDARIEDADE", "solid": "SOLIDARIEDADE",
    "cidadania": "CIDADANIA", "patriota": "PATRIOTA", "avante": "AVANTE",
    "novo": "NOVO", "rede": "REDE", "pcdob": "PCdoB", "pc do b": "PCdoB",
}


def _partido_casa(query: str, sigla_dep: str) -> bool:
    if not query or not sigla_dep:
        return False
    q = _norm(query)
    alvo = _PARTIDO_ALIAS.get(q, query)
    a, s = _norm(alvo), _norm(sigla_dep)
    return a == s or a in s or s in a


_MESES = {
    "janeiro": 1, "fevereiro": 2, "marco": 3, "março": 3, "abril": 4, "maio": 5,
    "junho": 6, "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10,
    "novembro": 11, "dezembro": 12,
}


def parse_data(texto: str, hoje: date) -> date | None:
    """Extrai data do texto: AAAA-MM-DD, DD/MM[/AAAA], 'hoje'/'amanhã',
    'N de <mês>' ('1º de julho'). None se não achar."""
    if not texto:
        return None
    n = _norm(texto).strip()
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", n)        # ISO
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            pass
    m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", n)  # DD/MM
    if m:
        d, mo = int(m[1]), int(m[2])
        yr = int(m[3]) if m[3] else hoje.year
        if yr < 100:
            yr += 2000
        try:
            cand = date(yr, mo, d)
            if not m[3] and cand < hoje:
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
    m = re.search(r"\b(\d{1,2})\s*o?\s+de\s+([a-z]+)\b", n)   # 1º/1 de julho (º→'o' no _norm)
    if m and m[2] in _MESES:
        d, mo = int(m[1]), _MESES[m[2]]
        yr = hoje.year
        try:
            cand = date(yr, mo, d)
            if cand < hoje:
                cand = date(yr + 1, mo, d)
            return cand
        except ValueError:
            pass
    return None


def _br(d: date) -> str:
    return d.strftime("%d/%m/%Y")


async def _autores_partidos(client: httpx.AsyncClient, prop_id: int,
                            deps: dict[int, dict]) -> list[tuple[str, str]]:
    """[(nome, partido)] dos autores de uma proposição. Partido vem do mapa de
    deputados (autor não-deputado fica com partido vazio)."""
    try:
        autores = await _get(client, f"/proposicoes/{prop_id}/autores")
    except CamaraError:
        return []
    out = []
    for a in autores:
        nome = a.get("nome") or "?"
        uri = a.get("uri") or ""
        partido = ""
        m = re.search(r"/deputados/(\d+)", uri)
        if m:
            partido = (deps.get(int(m[1])) or {}).get("partido", "")
        out.append((nome, partido))
    return out


async def consultar_pauta(
    comissao_texto: str, data_texto: str, *,
    partido: str | None = None, deputado: str | None = None,
    tz: str = "America/Sao_Paulo",
) -> str:
    """Pauta de uma comissão da Câmara numa data, com filtro opcional por
    partido/deputado. Texto pronto pro chat (dado oficial; nunca inventa)."""
    comissao_texto = (comissao_texto or "").strip()
    if not comissao_texto:
        return "erro: informe a comissão (ex: 'Comissão de Saúde', 'CCJ')."
    hoje = datetime.now(ZoneInfo(tz)).date()
    d = parse_data(data_texto, hoje)
    if not d:
        return "erro: não entendi a data (use DD/MM, AAAA-MM-DD, 'amanhã' ou '1º de julho')."

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        comissoes = await _ensure_comissoes(client)
        cands = resolver_comissao(comissao_texto, comissoes)
        if not cands:
            return ("Não achei essa comissão entre as permanentes da Câmara. "
                    "Tente o nome (ex: 'Saúde', 'Constituição e Justiça') ou a sigla.")
        if len(cands) > 1:
            nomes = "; ".join(f"{c['sigla']} ({c['nome']})" for c in cands[:6])
            return f"Tem mais de uma comissão parecida — qual? {nomes}"
        org = cands[0]

        eventos = await _get(client, f"/orgaos/{org['id']}/eventos",
                             {"dataInicio": d.isoformat(), "dataFim": d.isoformat(), "itens": 50})
        if not eventos:
            return f"🏛️ {org['sigla']} — sem reunião agendada em {_br(d)}."

        deps = await _ensure_deputados(client)
        partes = [f"🏛️ {org['nome']} ({org['sigla']}) — {_br(d)}"]
        sem = asyncio.Semaphore(_CONCURRENCY)

        for ev in eventos:
            hora = (ev.get("dataHoraInicio") or "")[11:16]
            tipo = ev.get("descricaoTipo") or "Reunião"
            pauta = await _get(client, f"/eventos/{ev['id']}/pauta")
            # proposições distintas da pauta
            props = {}
            for item in pauta:
                p = item.get("proposicao_") or {}
                pid = p.get("id")
                if pid and pid not in props:
                    props[pid] = p

            async def _resolve(pid, p):
                async with sem:
                    autores = await _autores_partidos(client, pid, deps)
                return (p, autores)

            resolved = await asyncio.gather(*(_resolve(pid, p) for pid, p in props.items()))

            # filtro por partido/deputado
            def _match(autores):
                if not partido and not deputado:
                    return True
                for nome, part in autores:
                    if partido and _partido_casa(partido, part):
                        return True
                    if deputado and _norm(deputado) in _norm(nome):
                        return True
                return False

            filtrados = [(p, a) for (p, a) in resolved if _match(a)]
            cab = f"\n📋 {tipo} {hora} — {len(props)} item(ns)"
            if (partido or deputado) and not filtrados:
                alvo = partido or deputado
                partes.append(cab + f"\n   • Nada de {alvo} nesta pauta.")
                continue
            partes.append(cab + (f" · {len(filtrados)} de {partido or deputado}:" if (partido or deputado) else ":"))
            for p, autores in sorted(filtrados, key=lambda x: (x[0].get("siglaTipo") or "", x[0].get("numero") or 0)):
                tag = f"{p.get('siglaTipo')} {p.get('numero')}/{p.get('ano')}"
                aut = ", ".join(f"{n} ({pt})" if pt else n for n, pt in autores[:3]) or "—"
                ementa = (p.get("ementa") or "").strip().replace("\n", " ")
                partes.append(f"   • {tag} — {aut}\n     {ementa[:160]}")

    return "\n".join(partes)
