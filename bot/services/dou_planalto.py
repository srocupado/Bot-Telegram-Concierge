"""Planalto como rede de captura de MP — independente do índice do DOU.

Nasceu da perda da MP 1.391 (publicada 11/09/2026 em edição EXTRA, detectada
pelo dono em 12/09). O portal do in.gov.br indexou o DESPACHO que encaminha a
MP ao Congresso, mas não a MP: busca por `artType="Medida Provisória"` em
11/09 devolvia ZERO. Como o Inlabs saiu do ar do projeto, o portal virou fonte
única — e respondeu "houve DOU e NENHUMA MP" com cara de certeza.

O Planalto resolve isso por uma propriedade que o índice não tem: **a URL é
determinística e a numeração é sequencial**.

    mpv1389.htm → 200      mpv1392.htm → 404
    mpv1390.htm → 200      mpv1393.htm → 404
    mpv1391.htm → 200      mpv1400.htm → 404

Então "existe MP que eu não entreguei?" deixa de ser inferência sobre um
índice incompleto e vira **evidência positiva**: pega o último número
entregue e sonda os seguintes. Não depende de data, de edição (normal ou
extra), nem da fila de dias — recupera MP mesmo de dia que já recebeu baixa.

Devolve `PortalMP` (de dou_portal) de propósito: assim a MP achada aqui
atravessa o pipeline inteiro — card, botão e geração da nota técnica — sem
nenhum caminho paralelo.
"""
from __future__ import annotations

import logging
import re
from datetime import date

import httpx

from bot.services.dou_monitor import PLANALTO_BASE, _planalto_period

logger = logging.getLogger(__name__)

# Planalto pendura a conexão por dezenas de segundos quando está caindo, e
# esta sonda roda dentro da janela proativa. Falhar rápido é melhor que
# segurar o tick — quem falha vira pendência, não silêncio.
_TIMEOUT = httpx.Timeout(8.0, connect=5.0)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}

# Teto por sonda. A numeração é sequencial, então na prática são 1-2 MPs
# novas por vez; o teto existe pro caso de o bot ter ficado dias fora.
_MAX_NOVAS = 12
# 404s seguidos que encerram a varredura. Dois, e não um: se o Planalto
# demorar a publicar UMA página no meio, parar no primeiro 404 esconderia as
# seguintes — e esconder MP é exatamente o que este módulo existe pra evitar.
_MAX_404_SEGUIDOS = 2

_MESES = {
    "janeiro": 1, "fevereiro": 2, "marco": 3, "março": 3, "abril": 4,
    "maio": 5, "junho": 6, "julho": 7, "agosto": 8, "setembro": 9,
    "outubro": 10, "novembro": 11, "dezembro": 12,
}

# Título dentro da página: "MEDIDA PROVISÓRIA Nº 1.391, DE 11 DE SETEMBRO DE
# 2026". É daqui que saem número, ano E data — regra do projeto: a identidade
# da MP vem da MP, nunca da requisição (MP assinada em 31/12 sai no DOU de
# 01/01 e o ano errado quebra dedup, Planalto e conferência).
_TITULO_RE = re.compile(
    r"MEDIDA\s+PROVIS[ÓO]RIA\s+N[ºo°\.\s]*(?P<num>[\d\.]+)\s*,?\s*DE\s+"
    r"(?P<dia>\d{1,2})\s+DE\s+(?P<mes>[A-Za-zÇçÃãÕõÁáÉéÍíÓóÚúÂâÊêÔô]+)\s+DE\s+"
    r"(?P<ano>\d{4})",
    re.IGNORECASE,
)
_INICIO_ATO_RE = re.compile(r"^(O\s+PRESIDENTE|A\s+PRESIDENTA|O\s+VICE)", re.IGNORECASE)


def url_mp(numero: str | int, ano: int) -> str:
    return (f"{PLANALTO_BASE}/ccivil_03/_ato{_planalto_period(ano)}/{ano}"
            f"/mpv/mpv{numero}.htm")


def _data_do_titulo(m: re.Match) -> date | None:
    mes = _MESES.get(m.group("mes").strip().lower())
    if not mes:
        return None
    try:
        return date(int(m.group("ano")), mes, int(m.group("dia")))
    except ValueError:
        return None


def _parse(conteudo: bytes) -> tuple[re.Match, str, str] | None:
    """(match do título, ementa, texto). None = página não é uma MP.

    Recebe BYTES, não str, e decodifica aqui: a página do Planalto NÃO declara
    charset nenhum (verificado em 12/09/2026), então tanto o httpx quanto o
    autodetect do BeautifulSoup erram — o httpx assume utf-8 e produz
    "PROVIS�RIA"; o bs4 chutou cp1250 e produziu "Nş 1.391". Nos dois casos a
    regex do título não casa e a MP fica invisível. utf-8 primeiro (se um dia
    mudarem) e iso-8859-1 depois, que é o que a página é hoje.

    O título vem QUEBRADO na página ("MEDIDA PROVISÓRIA Nº 1.391, DE" / "11" /
    "DE SETEMBRO DE 2026"), então a regex roda sobre o texto inteiro com
    espaços normalizados, nunca linha a linha.

    O Planalto às vezes responde 200 com página de erro; sem o título casando
    NÃO inventamos MP — melhor não achar do que anunciar uma que não existe.
    """
    from bs4 import BeautifulSoup

    try:
        html = conteudo.decode("utf-8")
    except UnicodeDecodeError:
        html = conteudo.decode("iso-8859-1", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    linhas = [ln.strip() for ln in soup.get_text("\n", strip=True).splitlines()
              if ln.strip()]
    plano = re.sub(r"\s+", " ", " ".join(linhas[:400])).strip()
    m = _TITULO_RE.search(plano)
    if not m:
        return None
    # Ementa = o que vem depois do título e antes do "O PRESIDENTE…" que abre
    # o dispositivo. Mesmo recorte que _ementa_do_excerpt usa no dou_monitor.
    depois = plano[m.end():].lstrip(" .,;")
    fim = re.search(r"\s(O\s+PRESIDENTE|A\s+PRESIDENTA|O\s+VICE)\b", depois,
                    re.IGNORECASE)
    ementa = (depois[:fim.start()] if fim else depois[:1200]).strip(" .,;")
    return m, ementa, "\n".join(linhas[:500])


async def buscar_mp(
    numero: str | int, ano: int, *, client: httpx.AsyncClient | None = None,
):
    """MP pelo número, direto do Planalto. None = não existe (404) ou a
    página não é uma MP. Exceção de rede sobe — o caller decide, porque
    "não consegui checar" ≠ "não existe"."""
    from bot.services.dou_portal import PortalMP

    url = url_mp(numero, ano)
    fechar = client is None
    client = client or httpx.AsyncClient(
        timeout=_TIMEOUT, headers=_HEADERS, follow_redirects=True,
    )
    try:
        resp = await client.get(url)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        parsed = _parse(resp.content)
    finally:
        if fechar:
            await client.aclose()

    if parsed is None:
        logger.info("planalto: %s respondeu 200 mas não parece MP", url)
        return None
    m, ementa, texto = parsed
    num_canon = m.group("num").replace(".", "")
    ano_real = int(m.group("ano"))
    pub = _data_do_titulo(m)
    if num_canon != str(numero).replace(".", ""):
        # Página do número X servindo a MP Y: não dá pra confiar em nada dela.
        logger.warning("planalto: %s trouxe MP %s — ignorando", url, num_canon)
        return None
    return PortalMP(
        numero=num_canon,
        ano=ano_real,
        titulo=m.group(0).strip(),
        ementa=ementa or None,
        url=url,
        texto=texto or None,
        data_publicacao=pub.isoformat() if pub else None,
        # A edição real (normal/extra) não está na página do Planalto. Fica
        # "Normal" só quando o portal não disser outra coisa — quem sabe a
        # edição é o índice do DOU, e ele enriquece depois.
        edicao="Normal",
    )


async def sondar_novas(ultimo_numero: int, anos: list[int]) -> list:
    """MPs com número ACIMA de `ultimo_numero` que existem no Planalto.

    O coração da rede: não pergunta "saiu MP no dia D?" (que depende do
    índice cobrir D), pergunta "existe MP depois da última que entreguei?".

    `anos` é lista porque a numeração NÃO reinicia na virada do ano, mas a
    pasta do Planalto sim: a MP 1.400 assinada em janeiro/2027 mora em
    /2027/mpv/, enquanto a 1.399 de dezembro/2026 mora em /2026/mpv/. Sondar
    só o ano corrente perderia MP toda primeira semana de janeiro.

    Exceção de rede sobe pro caller — Planalto fora é "não sei", nunca
    "não há MP nova"."""
    achadas: list = []
    faltas = 0
    anos = list(dict.fromkeys(anos))  # únicos, preservando a ordem
    async with httpx.AsyncClient(
        timeout=_TIMEOUT, headers=_HEADERS, follow_redirects=True,
    ) as client:
        n = ultimo_numero
        while len(achadas) < _MAX_NOVAS and faltas < _MAX_404_SEGUIDOS:
            n += 1
            mp = None
            for ano in anos:
                mp = await buscar_mp(n, ano, client=client)
                if mp is not None:
                    break
            if mp is None:
                faltas += 1
                continue
            faltas = 0
            achadas.append(mp)
    if achadas:
        logger.info("planalto: %d MP(s) além da %d — %s", len(achadas),
                    ultimo_numero, [f"{m.numero}/{m.ano}" for m in achadas])
    return achadas


async def ultimo_numero_entregue(session, user_id: int) -> tuple[int, int] | None:
    """(número, ano) da MP mais alta já ENTREGUE a este usuário, ou None.

    É a régua da sonda sequencial. None = sem régua (usuário novo): aí não há
    o que sondar e nenhuma conclusão pode ser tirada."""
    from sqlalchemy import select

    from bot.db.models import DouSeenMP
    from bot.services.dou_monitor import numero_canonico

    rows = await session.scalars(
        select(DouSeenMP).where(DouSeenMP.user_id == user_id)
    )
    vistos: list[tuple[int, int]] = []
    for r in rows:
        try:
            vistos.append((int(numero_canonico(r.numero)), r.ano))
        except (TypeError, ValueError):
            continue
    return max(vistos) if vistos else None


async def confirma_sem_mp_nova(session, user_id: int, hoje_ano: int) -> bool:
    """A sonda RODOU e não existe MP acima da última entregue?

    True é evidência POSITIVA de ausência, vinda de fonte independente do
    índice do DOU — é o que autoriza dar baixa num dia com edição extra e
    zero MP no índice. Falha de rede ou ausência de régua devolvem False:
    "não sei" nunca vira "não há"."""
    regua = await ultimo_numero_entregue(session, user_id)
    if regua is None:
        return False
    ultimo, ano_ultimo = regua
    try:
        return not await sondar_novas(ultimo, [hoje_ano, ano_ultimo])
    except Exception as exc:
        logger.warning("planalto: sonda de confirmação falhou (%s)", exc)
        return False
