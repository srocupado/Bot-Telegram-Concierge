"""Fallback público de detecção de MP: portal www.in.gov.br.

Homologado contra o site real (06/08/2026, sondas do container E do Orange
Pi — MP 1.381 encontrada de ponta a ponta): o endpoint /consulta/-/buscar/dou
responde SEM login com a lista de matérias em JSON embutido no HTML (bloco
<script id="..._params">), incluindo artType "Medida Provisória", seção,
data de publicação e o link da matéria — e a página da matéria entrega o
título oficial e a ementa. Exige disfarce leve (User-Agent de navegador +
HTTP/1.1): com UA de curl o WAF corta a conexão.

PAPEL (INVERTIDO em 11/08/2026, decisão do dono): fonte de VERIFICAÇÃO com
direito a baixa quando a evidência é positiva — MPs encontradas, ou edição
confirmada sem MP, ou (com dia-CONTROLE) ausência conclusiva de edição. O
Inlabs vira secundário: enriquecimento (XML) e confirmação quando estiver
de bom humor. Motivos da inversão, todos medidos: o Inlabs acumulou
vaga-lume de listagem, manutenções, WAF barrando login frio e o caso
decisivo — a MP 1.382/2026, publicada na edição EXTRA retroativa de
01/08 (pasta criada no Inlabs só em 10/08), que APENAS o portal detectou,
com texto íntegro. A busca de MP do portal cobre inclusive edições extras
(medido: achou a 1.382); a sonda de edição (portaria/despacho do1) NÃO
enxerga dia só-extra — por isso o veredito operante da baixa é sempre
"sem MP indexada", nunca só "sem edição".

É scraping de HTML sem contrato: qualquer mudança de layout estoura
PortalError ALTO (bloco ausente, título não parseável) — nunca lista vazia
silenciosa, que aqui seria falso negativo de MP.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta

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
    ano: int          # int como nos dicts do Inlabs — str quebrava o dedup
    titulo: str       # contra DouSeenMP.ano (int) em silêncio
    ementa: str | None
    url: str
    # Texto INTEGRAL do ato, montado da página da matéria (identifica +
    # parágrafos + assinatura). None quando o corpo não passou na régua de
    # sanidade — nota técnica com texto suspeito é pior que esperar o Inlabs.
    texto: str | None = None
    data_publicacao: str | None = None   # ISO; None = usar o dia consultado
    # "Extra" | "Normal", do pubName do índice (ex.: DO1_EXTRA_C). Sem isso a
    # MP 1.382 (extra de sábado 01/08/2026) saiu rotulada "Edição Normal" no
    # prompt da nota — o default silencioso mentiu pro LLM (dono, 11/08/2026).
    edicao: str = "Normal"


@dataclass(frozen=True)
class PortalDia:
    mps: list
    # True = houve edição indexada na data (com ou sem MP). False = índice
    # vazio pra data — NÃO afirma "sem edição": pode ser atraso do índice.
    edicao_confirmada: bool
    # True = evidência POSITIVA de dia sem DO1 regular E sem MP indexada:
    # busca de MP e sondas vazias pra data, com a MESMA sonda devolvendo
    # matérias no dia de CONTROLE (índice vivo cobrindo o período — a mesma
    # estrutura de prova do "raiz sem a pasta" no Inlabs). Nunca True sem
    # controle vivo. Combinado com dia fechado, autoriza baixa.
    sem_edicao: bool = False


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
_IDENTIFICA_RE = re.compile(r'<p class="identifica"[^>]*>(.*?)</p>', re.DOTALL)
# Corpo do ato na página da matéria (medido na MP 1.381: 112 parágrafos).
# ATENÇÃO ao [^>]*: o <p> vem com atributos além da class — o match exato
# '<p class="dou-paragraph">' devolvia 0 e quase condenou a fonte na sondagem.
_PARAGRAFO_RE = re.compile(r'<p class="dou-paragraph"[^>]*>(.*?)</p>', re.DOTALL)
_ASSINA_RE = re.compile(r'<p class="assina(?:Pr)?"[^>]*>(.*?)</p>', re.DOTALL)

# Régua de sanidade do texto: MP real tem no mínimo preâmbulo + artigos.
# Reprovou → texto=None e a nota espera o Inlabs (análise jurídica em cima
# de texto truncado é pior que atraso).
_MIN_PARAGRAFOS = 3
_MIN_CHARS_TEXTO = 200


def _fmt_data(d: date) -> str:
    return d.strftime("%d-%m-%Y")


def dia_controle(d: date) -> date:
    """Último dia ÚTIL estritamente anterior a `d` — o dia com máxima chance
    de ter DO1 regular no índice. Se ele for feriado (controle vazio), o
    veredito segue inconclusivo — lado seguro."""
    c = d - timedelta(days=1)
    while c.weekday() >= 5:
        c -= timedelta(days=1)
    return c


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


def _limpo(trecho: str) -> str:
    import html as _html
    return _html.unescape(_TAGS_RE.sub("", trecho)).strip()


async def _materia(client: httpx.AsyncClient, url_title: str) -> tuple[str | None, str | None]:
    """(ementa, texto_integral) da página da matéria — best-effort: falha aqui
    não bloqueia a DETECÇÃO (o aviso sai com o título; a nota espera o
    Inlabs). O texto só é devolvido quando passa na régua de sanidade."""
    if not url_title:
        return None, None
    try:
        resp = await client.get(MATERIA_URL.format(url_title=url_title))
        resp.raise_for_status()
    except httpx.HTTPError:
        logger.warning("portal: página da matéria indisponível (%s)", url_title)
        return None, None
    html = resp.text
    m = _EMENTA_RE.search(html)
    ementa = _limpo(m.group(1)) if m else None

    ident = _IDENTIFICA_RE.search(html)
    pars = [p for p in (_limpo(x) for x in _PARAGRAFO_RE.findall(html)) if p]
    assinaturas = [a for a in (_limpo(x) for x in _ASSINA_RE.findall(html)) if a]
    texto = None
    if ident and len(pars) >= _MIN_PARAGRAFOS and sum(len(p) for p in pars) >= _MIN_CHARS_TEXTO:
        blocos = [_limpo(ident.group(1))]
        if ementa:
            blocos.append(ementa)
        blocos.append("\n".join(pars))
        if assinaturas:
            blocos.append("\n".join(assinaturas))
        texto = "\n\n".join(blocos)
    else:
        logger.warning(
            "portal: corpo da matéria reprovado na sanidade (%s: identifica=%s "
            "parágrafos=%d) — nota vai esperar o Inlabs",
            url_title, bool(ident), len(pars),
        )
    return ementa, texto


def _data_iso(pub_date_br: str | None) -> str | None:
    """'31/07/2026' → '2026-07-31'; lixo → None (caller usa o dia consultado)."""
    try:
        from datetime import datetime as _dt
        return _dt.strptime((pub_date_br or "").strip(), "%d/%m/%Y").date().isoformat()
    except ValueError:
        return None


def mp_dict_para_nota(mp: PortalMP, d: date) -> dict | None:
    """Converte a matéria do portal pro MESMO formato de MP que o pipeline da
    nota consome do Inlabs (generate_nota_tecnica/build_docx/dedup). None
    quando o texto não passou na sanidade — o caller mantém a fila."""
    if not mp.texto:
        return None
    from bot.services.dou_monitor import PLANALTO_BASE, _planalto_period
    period = _planalto_period(mp.ano)
    return {
        "numero": mp.numero,
        "ano": mp.ano,
        "ementa": mp.ementa or mp.titulo,
        "data_publicacao": mp.data_publicacao or d.isoformat(),
        "url_planalto": f"{PLANALTO_BASE}/ccivil_03/_ato{period}/{mp.ano}/mpv/mpv{mp.numero}.htm",
        "texto_integral": mp.texto,
        "edicao": mp.edicao,
    }


async def checar_dia_portal(d: date, *, controle: date | None = None) -> PortalDia:
    """Responde 'houve MP publicada no DOU de `d`?' pelo portal público.

    Duas consultas: a de MPs e — quando vem vazia — uma sonda genérica que
    prova que a EDIÇÃO do dia está no índice (portaria/despacho existem em
    praticamente toda edição do DO1). Edição confirmada + 0 MPs = evidência
    positiva de "sem MP até agora"; índice vazio = inconclusivo (o caller
    mantém a pendência — na dúvida, é pendência).

    `controle`: dia útil FECHADO que deveria ter edição (o caller escolhe —
    tipicamente o último dia útil antes de `d`). Quando o índice vem vazio
    pra `d` mas a sonda devolve matérias no controle, a ausência vira
    evidência POSITIVA (sem_edicao=True): o índice está vivo e cobre o
    período — se houvesse matéria em `d`, apareceria. Controle vazio também
    → segue inconclusivo (lado seguro)."""
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
            # Número CANÔNICO (sem o ponto de milhar do título): é a mesma
            # forma que o Inlabs e a Câmara usam. Divergir disso quebrou a
            # conferência em 15/08/2026 — ver dou_monitor.numero_canonico.
            numero, ano = m.group(1).replace(".", ""), int(m.group(2))
            if (numero, ano) in vistos:
                continue
            vistos.add((numero, ano))
            ementa = texto = None
            if len(mps) < _MAX_EMENTAS:
                ementa, texto = await _materia(client, it.get("urlTitle") or "")
            mps.append(PortalMP(
                numero=numero, ano=ano, titulo=titulo, ementa=ementa,
                url=MATERIA_URL.format(url_title=it.get("urlTitle") or ""),
                texto=texto,
                data_publicacao=_data_iso(it.get("pubDate")),
                edicao=("Extra" if "EXTRA" in (it.get("pubName") or "").upper()
                        else "Normal"),
            ))
        if mps:
            logger.info("portal DOU: %d MP(s) em %s", len(mps), d.isoformat())
            return PortalDia(mps, True)
        # 0 MPs: edição existe no índice? (separa "sem MP" de "índice fora")
        for sonda in ("portaria", "despacho"):
            if await _buscar(client, sonda, d, secao="do1"):
                return PortalDia([], True)
        # Índice vazio pra `d`. Com um dia de CONTROLE respondendo, a
        # ausência é positiva: não houve DO1 regular nem MP indexada em `d`.
        if controle is not None:
            for sonda in ("portaria", "despacho"):
                if await _buscar(client, sonda, controle, secao="do1"):
                    logger.info(
                        "portal DOU: %s sem matérias com controle %s vivo — "
                        "sem edição/MP (conclusivo)", d.isoformat(), controle,
                    )
                    return PortalDia([], False, sem_edicao=True)
            logger.info("portal DOU: controle %s também vazio — inconclusivo",
                        controle)
        return PortalDia([], False)
