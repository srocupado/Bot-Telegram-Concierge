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


# A API de Dados Abertos da Câmara é instável: 502/503/504 e timeouts são
# comuns e TRANSITÓRIOS (passam na tentativa seguinte). Retry com backoff evita
# falhar a pauta inteira por um 504 passageiro. 4xx não repete (erro do pedido).
_RETRY_STATUS = {500, 502, 503, 504}
_RETRIES = 3


async def _get(client: httpx.AsyncClient, path: str, params: dict | None = None) -> list:
    url = f"{_API}{path}"
    last: Exception | None = None
    for attempt in range(1, _RETRIES + 1):
        try:
            r = await client.get(url, params=params or {}, headers=_HEADERS)
            r.raise_for_status()
            return (r.json() or {}).get("dados") or []
        except httpx.HTTPStatusError as e:
            last = e
            if e.response.status_code not in _RETRY_STATUS:
                raise CamaraError(f"API da Câmara falhou: {e}") from e
        except httpx.HTTPError as e:  # timeout, erro de conexão
            last = e
        if attempt < _RETRIES:
            await asyncio.sleep(attempt)  # 1s, 2s
    raise CamaraError(
        f"API da Câmara instável (504/timeout) após {_RETRIES} tentativas"
    ) from last


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


def _partido_de_nome(nome: str, deps: dict[int, dict]) -> str:
    """Partido atual de um deputado pelo NOME (o campo `relator` da pauta é só
    nome, sem id). Igualdade primeiro; senão contém."""
    if not nome:
        return ""
    nn = _norm(nome)
    for d in deps.values():
        if _norm(d["nome"]) == nn:
            return d["partido"]
    for d in deps.values():
        dn = _norm(d["nome"])
        if nn in dn or dn in nn:
            return d["partido"]
    return ""


def _match_pessoa(partido: str | None, deputado: str | None, nome: str, part: str) -> bool:
    if partido and _partido_casa(partido, part):
        return True
    if deputado and _norm(deputado) in _norm(nome):
        return True
    return False


def _extrair_voto(parecer: dict) -> str:
    """Teor do voto do relator, da ementa do parecer (PRL): 'pela aprovação, com
    substitutivo', 'pela rejeição' etc. '' se o item não for um parecer (ex.:
    requerimento, ou PL ainda sem parecer)."""
    if (parecer.get("siglaTipo") or "").upper() != "PRL":
        return ""
    e = re.sub(r"\s+", " ", parecer.get("ementa") or "").strip()
    m = re.search(r"\bpel[ao]\s+.+", e, re.IGNORECASE)   # 'pela aprovação...'
    return m.group(0).rstrip(". ")[:170] if m else ""


async def _secao_comissao(client, org: dict, d: date, partido, deputado, deps) -> str:
    """Seção de UMA comissão: header + reuniões/pauta na data, com filtro."""
    eventos = await _get(client, f"/orgaos/{org['id']}/eventos",
                         {"dataInicio": d.isoformat(), "dataFim": d.isoformat(), "itens": 50})
    if not eventos:
        return f"🏛️ {org['nome']} ({org['sigla']}) — sem reunião agendada em {_br(d)}."

    alvo = partido or deputado
    partes = [f"🏛️ {org['nome']} ({org['sigla']}) — {_br(d)}"]
    sem = asyncio.Semaphore(_CONCURRENCY)
    if True:
        for ev in eventos:
            hora = (ev.get("dataHoraInicio") or "")[11:16]
            tipo = ev.get("descricaoTipo") or "Reunião"
            pauta = await _get(client, f"/eventos/{ev['id']}/pauta")

            # Cada item → o PROJETO (PL relatada num parecer, ou a própria
            # proposição) + o nome do relator. Assim cobrimos AUTORIA (autor do
            # projeto de fundo) E RELATORIA (campo relator) separadamente.
            itens = []
            for item in pauta:
                prop = item.get("proposicao_") or {}
                rel_prop = item.get("proposicaoRelacionada_") or {}
                projeto = rel_prop if rel_prop.get("id") else prop
                relator = item.get("relator")
                if isinstance(relator, dict):
                    relator = relator.get("nome")
                if projeto.get("id"):
                    itens.append((projeto, relator if isinstance(relator, str) else None, prop))

            ids = list({p["id"] for p, _, _ in itens})

            async def _fa(pid):
                async with sem:
                    return pid, await _autores_partidos(client, pid, deps)

            amap = dict(await asyncio.gather(*(_fa(pid) for pid in ids)))

            def _ordem(t):
                # `numero` às vezes vem string, às vezes int — coage p/ não
                # quebrar o sort (TypeError str vs int).
                try:
                    num = int(t[0].get("numero") or 0)
                except (TypeError, ValueError):
                    num = 0
                return (str(t[0].get("siglaTipo") or ""), num)

            linhas = []
            for projeto, relator, parecer in sorted(itens, key=_ordem):
                autores = amap.get(projeto["id"], [])
                rel_partido = _partido_de_nome(relator, deps) if relator else ""
                autor_hit = any(_match_pessoa(partido, deputado, n, p) for n, p in autores)
                relator_hit = bool(relator) and _match_pessoa(partido, deputado, relator, rel_partido)
                if alvo and not (autor_hit or relator_hit):
                    continue
                papeis = []
                if autor_hit:
                    papeis.append("autoria")
                if relator_hit:
                    papeis.append("relatoria")
                # \x02..\x03 marcam o que vira <b>..</b> no handler (depois do
                # escape HTML), pra negritar o número da proposição.
                tag = f"\x02{projeto.get('siglaTipo')} {projeto.get('numero')}/{projeto.get('ano')}\x03"
                papel_txt = f" [{' + '.join(papeis)}]" if papeis else ""
                aut = ", ".join(f"{n} ({p})" if p else n for n, p in autores[:2]) or "—"
                rel_txt = f"{relator} ({rel_partido})" if relator else "—"
                voto = _extrair_voto(parecer)
                ementa = (projeto.get("ementa") or "").strip().replace("\n", " ")
                linha = f"   • {tag}{papel_txt}\n     autor: {aut} · relator: {rel_txt}"
                if voto:
                    linha += f"\n     🗳️ voto do relator: {voto}"
                else:
                    linha += "\n     🗳️ voto do relator: (ainda sem parecer na pauta)"
                linha += f"\n     {ementa[:140]}"
                linhas.append(linha)

            cab = f"\n📋 {tipo} {hora} — {len(itens)} projeto(s)"
            if alvo and not linhas:
                partes.append(cab + f"\n   • Nada de {alvo} — nem autoria nem relatoria.")
            else:
                partes.append(cab + (f" · {len(linhas)} de {alvo}:" if alvo else ":"))
                partes.extend(linhas)

    return "\n".join(partes)


async def consultar_pauta(
    comissoes, data_texto: str, *,
    partido: str | None = None, deputado: str | None = None,
    tz: str = "America/Sao_Paulo",
) -> str:
    """Pauta de UMA OU MAIS comissões da Câmara numa data, com filtro opcional
    por partido/deputado. `comissoes` aceita str (uma) ou lista. Texto pronto
    pro chat (dado oficial; nunca inventa)."""
    if isinstance(comissoes, str):
        comissoes = [comissoes]
    comissoes = [str(c).strip() for c in (comissoes or []) if str(c).strip()]
    if not comissoes:
        return "erro: informe a comissão (ex: 'Comissão de Saúde', 'CCJ')."
    hoje = datetime.now(ZoneInfo(tz)).date()
    d = parse_data(data_texto, hoje)
    if not d:
        return "erro: não entendi a data (use DD/MM, AAAA-MM-DD, 'amanhã' ou '1º de julho')."

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        catalogo = await _ensure_comissoes(client)
        deps = await _ensure_deputados(client)
        secoes = []
        for texto in comissoes:
            cands = resolver_comissao(texto, catalogo)
            if not cands:
                secoes.append(f"🏛️ “{texto}” — não achei essa comissão permanente da Câmara.")
            elif len(cands) > 1:
                nomes = "; ".join(f"{c['sigla']} ({c['nome']})" for c in cands[:6])
                secoes.append(f"🏛️ “{texto}” — qual delas? {nomes}")
            else:
                secoes.append(await _secao_comissao(client, cands[0], d, partido, deputado, deps))
        return "\n\n".join(secoes)
