"""Monitor de Medidas Provisórias no Diário Oficial da União (Inlabs/DOU).

Autônomo — não compartilha código nem estado com o Monitor-de-MP externo.
Fluxo: autentica no Inlabs → LISTA a pasta do dia → baixa o que existir de
Seção 1 (zip XML DO1/DO1E; ou o PDF da extra quando o dia sai só em PDF —
sábado/feriado) → extrai MPs publicadas → gera nota técnica via Claude →
entrega no Telegram (mensagem + DOCX). Dedup por (usuário, número, ano) na
tabela dou_seen_mps.

Credenciais: INLABS_EMAIL / INLABS_PASSWORD (cadastro gratuito em
inlabs.in.gov.br). Reusa ANTHROPIC_API_KEY do bot pra nota técnica.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import threading
import time
import zipfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import DouSeenMP, ProactiveNotice
from bot.services.llm.gemini_impl import gerar

logger = logging.getLogger(__name__)

PLANALTO_BASE = "https://www.planalto.gov.br"
INLABS_BASE = "https://inlabs.in.gov.br"
DOU_SECTIONS = ["DO1E", "DO1"]

BRT = ZoneInfo("America/Sao_Paulo")

# A partir de que hora do dia SEGUINTE um 404 do Inlabs vira definitivo.
# Antes disso, 404 é "ainda não saiu" — a edição extra (DO1E), onde sai
# crédito extraordinário, pode ser publicada a qualquer hora do dia, inclusive
# à noite. 6h dá margem e ainda deixa o briefing das 7h fechar o dia anterior.
_HORA_FECHAMENTO = 6

# UMA nota por vez em todo o processo. A geração é web search + LLM + DOCX, e
# nada mais impede duas simultâneas: job da fila + /mp_dou_agora, DOIS USUÁRIOS
# da casa pedindo ao mesmo tempo, ou o teto de notas por janela acima de 1.
#
# Por que importa mesmo com plano pago: a chave e o plano são de quem usa. Se
# qualquer usuário estiver num provedor de RPM apertado (free tier), duas
# gerações concorrentes viram 429 — e 429 na redação NÃO derruba a entrega:
# `_gen_nota_*` captura e devolve None, então o DOCX sai com o texto base e a
# legenda "sem análise da IA". Perde-se a ANÁLISE, não a nota, e com aviso.
# Ainda assim vale evitar: a análise é o produto. O semáforo protege
# independente do plano, em vez de depender de todo chamador se comportar.
# Custo: a segunda nota espera ~1min; ela roda em background.
_SEM_NOTA = asyncio.Semaphore(1)


def _dia_encerrado(d: date, agora: datetime | None = None) -> bool:
    """True quando a data já não pode mais receber edição do DOU.

    É o que separa 404 legítimo ("não publicado", dia fechado) de 404 espúrio
    ou prematuro ("ainda não saiu" / Inlabs instável). Sem essa régua, um 404
    dava baixa imediata no dia e a MP publicada depois — ou não servida por
    instabilidade — sumia em silêncio, sem pendência e sem aviso.
    """
    agora = agora or datetime.now(BRT)
    limite = datetime.combine(
        d + timedelta(days=1), datetime.min.time(), tzinfo=BRT,
    ) + timedelta(hours=_HORA_FECHAMENTO)
    return agora >= limite

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}

_TITLE_RE = re.compile(
    r"^\s*MEDIDA PROVIS[ÓO]RIA\s+N[ºo°\.°]?\s*([\d\.]+)", re.IGNORECASE
)


class DouError(Exception):
    pass


# ──────────────────────── fetch (sync; rodar em thread) ────────────────────────

def _planalto_period(year: int) -> str:
    start = ((year - 1991) // 4) * 4 + 1991
    return f"{start}-{start + 3}"


def _fetch_mp_page(client: httpx.Client, url: str) -> tuple[str, str]:
    """Best-effort: busca ementa + texto limpo na página do Planalto.

    Timeout curto: planalto.gov.br é instável e segura o request por dezenas
    de segundos quando está caindo. Como temos try/except e caímos no excerpt
    do DOU como fallback, falhar rápido é melhor que pendurar o tick inteiro.
    """
    try:
        resp = client.get(url, timeout=5.0)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        lines = [ln.strip() for ln in soup.get_text("\n", strip=True).splitlines() if ln.strip()]
        ementa = ""
        found_title = False
        for ln in lines[:120]:
            if re.search(r"MEDIDA\s+PROVIS[ÓO]RIA\s+N", ln, re.I):
                found_title = True
                continue
            if found_title and len(ln) > 20:
                if not re.match(r"(A PRESIDENTA|O PRESIDENTE|O VICE)", ln, re.I):
                    ementa = ln
                    break
        # Sem fallback p/ lines[0]: a 1ª linha da página costuma ser o nome do
        # arquivo ('mpv1365') ou navegação — lixo como ementa. Devolvendo ""
        # aqui, _build_mp_dict cai na ementa derivada do excerpt do DOU.
        return ementa, "\n".join(lines[:500])
    except Exception as exc:
        logger.warning("dou: erro ao buscar página planalto %s: %s", url, exc)
        return "", ""


def _parse_pubdate(s: str | None) -> date | None:
    """Converte a data de publicação do DOU (vários formatos) em date."""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


# Título da MP dentro do corpo: "MEDIDA PROVISÓRIA Nº 1.381, DE 30 DE JULHO
# DE 2026". Serve pra duas coisas: achar onde a ementa REALMENTE começa e ler
# o ANO da própria MP.
_TITULO_NO_CORPO_RE = re.compile(
    r"MEDIDA\s+PROVIS[ÓO]RIA\s+N[ºo°\.\s]*(?P<num>[\d\.]+)\s*,?\s*DE\s+\d{1,2}\s+DE\s+"
    r"[A-ZÇÃÕÁÉÍÓÚÂÊÔa-zçãõáéíóúâêô]+\s+DE\s+(?P<ano>\d{4})",
    re.IGNORECASE,
)
_FIM_EMENTA_RE = re.compile(r"\s+(O\s+PRESIDENTE|A\s+PRESIDENTA|O\s+VICE)", re.IGNORECASE)


def _corta_em_palavra(texto: str, limite: int = 300) -> str:
    """Trunca sem partir palavra e sem deixar pontuação solta.

    O corte cego (`[:300]`) produzia coisas como "…para os fi" na mensagem que
    o dono recebe — visível em toda MP cuja página do Planalto não respondeu.
    """
    texto = texto.strip()
    if len(texto) <= limite:
        return texto
    corte = texto[:limite]
    # Preferir terminar numa frase; se não houver ponto, no último espaço.
    ponto = max(corte.rfind(". "), corte.rfind("; "))
    if ponto >= limite // 2:
        return corte[:ponto + 1].strip()
    espaco = corte.rfind(" ")
    return (corte[:espaco] if espaco > 0 else corte).rstrip(" ,;:-") + "…"


def _ementa_do_excerpt(text_excerpt: str) -> str:
    """Ementa a partir do trecho do DOU, quando o Planalto não respondeu.

    O excerpt costuma trazer ementa + título + ementa de novo. Antes o fallback
    pegava do início e cortava em 300 chars, então a ementa saía DUPLICADA e
    truncada no meio da palavra. Agora começa DEPOIS do título (que é onde a
    ementa oficial de fato começa) e termina antes do "O PRESIDENTE…".
    """
    limpo = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text_excerpt or "")).strip()
    if not limpo:
        return ""
    m = _TITULO_NO_CORPO_RE.search(limpo)
    corpo = limpo[m.end():].strip() if m else limpo
    fim = _FIM_EMENTA_RE.search(corpo)
    if fim:
        corpo = corpo[:fim.start()].strip()
    # Sobrou só o título/nada aproveitável: melhor devolver o trecho inteiro
    # cortado direito do que uma string vazia (o dono fica sem ementa nenhuma).
    return _corta_em_palavra(corpo or limpo)


def ano_da_mp(text_excerpt: str, padrao: int, numero: str | None = None) -> int:
    """Ano da PRÓPRIA MP, lido do título; `padrao` (ano da data consultada) só
    quando o título não trouxer.

    Importa na virada de ano: MP assinada em 31/12 sai no DOU de 01/01, e usar
    o ano da consulta gravaria 1400/2027 pra uma MP que é 1400/2026 — quebrando
    o dedup, a URL do Planalto e o cruzamento com a Câmara (que devolve o ano
    correto), justamente onde a checagem serve pra não perder MP.

    `numero`: ANCORA a extração à MP certa. Sem ele, um "Revoga a MP nº 1.200,
    de 2025" ANTES do título próprio faria pegar o ano da MP CITADA (bug #5 da
    auditoria). Com ele, escolhe o título cujo número casa o da MP processada.
    """
    limpo = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text_excerpt or ""))
    alvo = numero.replace(".", "") if numero else None
    escolhido = None
    for m in _TITULO_NO_CORPO_RE.finditer(limpo):
        if alvo is None or m.group("num").replace(".", "") == alvo:
            escolhido = m
            break
    if escolhido is None:
        # Nenhum título casa o número (ou não há número): cai no primeiro título
        # como antes — melhor um palpite ancorado no padrão do que nada.
        escolhido = _TITULO_NO_CORPO_RE.search(limpo)
    if not escolhido:
        return padrao
    ano = int(escolhido.group("ano"))
    # Guarda contra lixo de OCR/parse: só aceita ano vizinho do consultado.
    return ano if padrao - 1 <= ano <= padrao + 1 else padrao


def _build_mp_dict(
    client: httpx.Client, numero: str, year: int, text_excerpt: str,
    target_date: date, pub_date: date | None = None,
) -> dict:
    period = _planalto_period(year)
    planalto_url = f"{PLANALTO_BASE}/ccivil_03/_ato{period}/{year}/mpv/mpv{numero}.htm"
    ementa_page, texto_planalto = _fetch_mp_page(client, planalto_url)
    # Ementa válida tem espaços e é razoavelmente longa; rejeita lixo como
    # 'mpv1365' (nome do arquivo) que às vezes vaza da extração da página.
    if ementa_page and " " in ementa_page and len(ementa_page) > 20:
        ementa = ementa_page
    else:
        ementa = _ementa_do_excerpt(text_excerpt)
    return {
        "numero": numero,
        "ano": year,
        "ementa": ementa,
        # Data REAL de publicação no DOU (do XML); cai no dia consultado só
        # se o XML não trouxer. É a base dos prazos regimentais.
        "data_publicacao": (pub_date or target_date).isoformat(),
        "url_planalto": planalto_url,
        "texto_integral": texto_planalto or text_excerpt[:20000],
    }


_PUBDATE_ATTRS = ("pubDate", "pubdate", "PubDate", "dataPublicacao", "data", "Data")


def _parse_dou_xml(client: httpx.Client, xml_content: str, target_date: date) -> list[dict]:
    year = target_date.year
    results: list[dict] = []
    seen: set[str] = set()
    parent_map: dict = {}

    def _pubdate_for(elem) -> date | None:
        """Sobe pelos ancestrais procurando um atributo de data de publicação."""
        cur, hops = elem, 0
        while cur is not None and hops < 12:
            if hasattr(cur, "get"):
                for attr in _PUBDATE_ATTRS:
                    d = _parse_pubdate(cur.get(attr))
                    if d:
                        return d
            cur = parent_map.get(cur)
            hops += 1
        return None

    def _try(title_text: str, body_text: str, elem=None) -> None:
        m = _TITLE_RE.match(title_text.strip().upper())
        if not m:
            return
        numero = m.group(1).replace(".", "")
        if numero in seen:
            return
        seen.add(numero)
        pub = _pubdate_for(elem) if elem is not None else None
        # Ano da PRÓPRIA MP (título casando o número), não da data consultada
        # nem de MP citada antes do título — ver ano_da_mp.
        ano = ano_da_mp(body_text or title_text, year, numero)
        if ano != year:
            logger.info("dou: MP %s — ano do título=%s difere do ano consultado=%s; "
                        "usando o do título", numero, ano, year)
        logger.info(
            "dou: MP %s — pubDate do XML=%s | target=%s | base usada=%s",
            numero, pub.isoformat() if pub else "NÃO ENCONTRADO",
            target_date.isoformat(), (pub or target_date).isoformat(),
        )
        results.append(
            _build_mp_dict(client, numero, ano, body_text or title_text, target_date, pub_date=pub)
        )

    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        soup = BeautifulSoup(xml_content, "html.parser")
        for tag in soup.find_all(True):
            txt = tag.get_text(" ", strip=True)
            if txt:
                _try(txt, txt)
        return results

    parent_map = {child: parent for parent in root.iter() for child in parent}
    for elem in root.iter():
        text = (elem.text or "").strip()
        if not text or len(text) > 2000:
            continue
        parent = parent_map.get(elem, elem)
        body_text = ET.tostring(parent, encoding="unicode", method="text")
        _try(text, body_text, elem)
    for elem in root.iter():
        attr_title = elem.get("title", "").strip()
        if attr_title:
            _try(attr_title, ET.tostring(elem, encoding="unicode", method="text"), elem)

    if not results:
        for raw_line in xml_content.splitlines():
            line = re.sub(r"<[^>]+>", " ", raw_line).strip()
            if 20 <= len(line) <= 400:
                _try(line, xml_content)
    return results


# Título da MP em CAIXA ALTA no INÍCIO da linha — é como o DOU imprime o
# CABEÇALHO da MP publicada ("MEDIDA PROVISÓRIA Nº 1.382, DE 1º DE AGOSTO…").
# Uma CITAÇÃO a outra MP vem em caixa de título ("altera a Medida Provisória nº
# 1.373") e no MEIO da frase; as duas âncoras (CAIXA ALTA + início de linha,
# logo SEM re.IGNORECASE) impedem que citação vire MP fantasma. Medido no PDF
# real de 01/08/2026: extra_C traz a 1.382 no cabeçalho e cita 1.373/1.355/…;
# extra_D só cita → corretamente 0 MP.
_TITULO_PDF_RE = re.compile(r"^\s*MEDIDA PROVIS[ÓO]RIA\s+N[º°\.\s]*([\d\.]+)")


def _extrair_texto_mupdf(content: bytes) -> str | None:
    """Extração via PyMuPDF (motor em C). ~20x mais rápido que o pypdf no ARM
    do Orange Pi — MEDIDO no PDF real de 01/08/2026: 0,5s vs 9,9s (extra_C),
    0,2s vs 8,2s (extra_D), com o MESMO texto e o MESMO cabeçalho em caixa
    alta. Devolve None (não "") quando a lib falta, pra o caller distinguir
    "não tem PyMuPDF" de "PyMuPDF leu e veio vazio"."""
    try:
        import pymupdf            # PyMuPDF >= 1.24.3
    except ImportError:
        try:
            import fitz as pymupdf   # nome antigo do mesmo pacote
        except ImportError:
            return None
    doc = pymupdf.open(stream=content, filetype="pdf")
    try:
        return "\n".join(p.get_text() for p in doc)
    finally:
        doc.close()


def _extrair_texto_pdf(content: bytes) -> str:
    """Texto de um PDF do DOU. Prefere o PyMuPDF (rápido) e cai no pypdf se ele
    faltar ou falhar — os dois entregam o mesmo cabeçalho, então o parser não
    muda. Imports TARDIOS: se AMBOS faltarem, degrada com aviso em vez de
    derrubar o import do módulo, e a seção vira FALHA (pendência), nunca 'sem
    MP'."""
    try:
        texto = _extrair_texto_mupdf(content)
        if texto is not None:
            return texto
    except Exception as exc:
        logger.warning("dou: PyMuPDF falhou (%s); tentando pypdf", exc)
    try:
        from pypdf import PdfReader
    except ImportError:
        logger.warning("dou: nem PyMuPDF nem pypdf disponíveis — não dá pra ler PDF")
        return ""
    try:
        reader = PdfReader(io.BytesIO(content))
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception as exc:
        logger.warning("dou: falha ao extrair texto do PDF: %s", exc)
        return ""


def _parse_dou_text(client: httpx.Client, texto: str, target_date: date) -> list[dict]:
    """Extrai MPs de TEXTO plano — o PDF da edição extra quando o Inlabs não
    gera o XML/zip (dia só com edição extra). Mesmo produto do _parse_dou_xml
    (número/ano/ementa + texto do Planalto), fonte diferente."""
    year = target_date.year
    results: list[dict] = []
    seen: set[str] = set()
    for raw in (texto or "").splitlines():
        m = _TITULO_PDF_RE.match(raw)
        if not m:
            continue
        numero = m.group(1).replace(".", "")
        if numero in seen:
            continue
        seen.add(numero)
        # Ano da PRÓPRIA MP (título casando o número), como no XML — ver ano_da_mp.
        ano = ano_da_mp(texto, year, numero)
        results.append(_build_mp_dict(client, numero, ano, texto, target_date))
    return results


# O Inlabs (login + download dos ZIPs) solta 502/503/504 e timeouts com
# frequência — transitórios. Retry com backoff evita perder a busca inteira por
# um Bad Gateway passageiro. Roda em thread (fetch_mps), então time.sleep é ok.
_INLABS_RETRY_STATUS = frozenset({500, 502, 503, 504})
# O Inlabs serve uma página "Sistema em Manutenção" (com status 502) quando o
# sistema está fora pra manutenção — não adianta retentar, e o usuário merece
# uma mensagem clara em vez de "HTTP 502".
_MAINT_RE = re.compile(r"manuten[çc][ãa]o", re.IGNORECASE)

# Tela de LOGIN (sessão recusada) × LISTAGEM de arquivos (logado, arquivo
# inexistente). O Inlabs nunca devolve 404: serve HTML com status 200 nos dois
# casos, e confundi-los custa caro nas duas direções — login lido como "não
# publicado" perde MP em silêncio; listagem lida como falha vira alarme falso
# todo fim de semana. Medido em 01/08/2026 contra o Inlabs real.
_E_LOGIN_RE = re.compile(r'type="password"|logar\.php', re.IGNORECASE)

# Marcadores do navegador de arquivos, MEDIDOS nos dois corpos reais
# (01/08/2026): presentes na listagem (37.549 chars) e ausentes na tela de
# login (6.032). O título "Imprensa Nacional - INLABS" NÃO serve — as duas
# páginas o têm, e depender só dele deixava a classificação de pé apenas pela
# ordem das checagens: bastava o Inlabs mudar o formulário de login pra sessão
# recusada virar "não publicado" e a MP sumir calada.
_MARCAS_LISTAGEM = ("sair", "tamanho", "modificado")


def _e_listagem(corpo: str) -> bool:
    """True quando o corpo é o navegador de arquivos com sessão ATIVA."""
    baixo = corpo.lower()
    return all(m in baixo for m in _MARCAS_LISTAGEM)


# Datas de PASTA (YYYY-MM-DD) referenciadas no corpo. É o que separa a listagem
# RAIZ (pastas de vários dias) da listagem DA PASTA de um dia (só arquivos
# daquele dia — os nomes carregam a própria data, nunca outra). A coluna
# "Modificado" usa DD-MM-YYYY e não casa aqui de propósito.
_DATA_PASTA_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


def _datas_no_corpo(corpo: str) -> set[str]:
    return set(_DATA_PASTA_RE.findall(corpo))


class InlabsMaintenanceError(DouError):
    pass


def _inlabs_call(do_request, *, tries: int = 3):
    """do_request() → httpx.Response. Retenta em 5xx transitório/timeout do
    Inlabs (backoff 2s, 4s) e devolve a Response (o caller checa status/404).
    Levanta InlabsMaintenanceError se cair na página de manutenção (sem
    retentar), ou o último erro se esgotar as tentativas."""
    last: Exception | None = None
    for attempt in range(1, tries + 1):
        try:
            r = do_request()
        except httpx.HTTPError as exc:      # timeout, erro de conexão
            last = exc
        else:
            if r.status_code not in _INLABS_RETRY_STATUS:
                return r
            # Página de manutenção: não é transitório, não retenta.
            if _MAINT_RE.search(r.text or ""):
                raise InlabsMaintenanceError(
                    "o Inlabs (sistema oficial do DOU) está em manutenção agora "
                    "— não dá pra checar as MPs. Tente mais tarde."
                )
            last = httpx.HTTPStatusError(
                f"HTTP {r.status_code}", request=r.request, response=r)
        if attempt < tries:
            time.sleep(attempt * 2)
    assert last is not None
    raise last


def chave_job_nota(user_id: int, target: date) -> str:
    """Chave de dedup do job da nota, COMPARTILHADA por quem pode dispará-lo:
    o comando/botão manual (`/mp_dou_agora`) e a re-tentativa da fila no
    proativo. Mesmo usuário + mesma data = um pipeline só; sem isso o tick
    poderia começar a mesma nota que o dono acabou de pedir no botão, e as
    duas competiriam pelo mesmo recurso caro (Inlabs + LLM)."""
    return f"nota:{user_id}:{target.isoformat()}"


@contextmanager
def _fase(nome: str, **extra):
    """Cronometra uma fase e loga quanto durou.

    O pipeline da nota tem 5 fases com custos MUITO diferentes (login, download
    dos ZIPs, unzip+parse do XML, pesquisa de contexto, redação). Sem medir,
    não dá pra saber qual delas segurou o bot — e o remédio de cada uma é
    diferente (thread, streaming pra disco, outro provider…)."""
    t0 = time.monotonic()
    try:
        yield
    finally:
        dt = time.monotonic() - t0
        rss = _rss_mb()
        logger.info(
            "dou[fase] %s: %.1fs%s%s", nome, dt,
            "".join(f" {k}={v}" for k, v in extra.items()),
            f" rss={rss:.0f}MB" if rss else "",
        )


def _rss_mb() -> float | None:
    try:
        with open("/proc/self/status") as fh:
            for linha in fh:
                if linha.startswith("VmRSS:"):
                    return int(linha.split()[1]) / 1024
    except Exception:
        pass
    return None


# A listagem do dia linka cada download como `...&dl=<arquivo>`; pegamos o alvo
# do dl= e, por garantia, também tokens soltos com cara de arquivo do DOU (caso
# o Inlabs mude o markup). Medido em 01/08/2026.
_DL_RE = re.compile(r"dl=([^\"'&<>\s]+)", re.IGNORECASE)
_ARQ_RE = re.compile(r"(\d{4}[-_]\d\d[-_]\d\d[0-9A-Za-z_\-]*\.(?:zip|pdf))", re.IGNORECASE)


def _arquivos_da_listagem(corpo: str) -> set[str]:
    """Nomes de arquivo que a listagem do dia oferece pra download."""
    return set(_DL_RE.findall(corpo)) | set(_ARQ_RE.findall(corpo))


def _fontes_secao1(nomes: set[str]) -> dict[str, dict[str, list[str]]]:
    """Separa os arquivos por slot da Seção 1 (DO1E extra / DO1 normal) e por
    tipo (zip preferido; pdf quando não há zip). Só a Seção 1 traz MP, então
    DO2/DO3 são ignorados de propósito. As classes são disjuntas: `-DO1.zip`
    não casa `-DO1E.zip`, e `_do1.pdf` não casa `_do1_extra_*.pdf`."""
    zips = [n for n in nomes if n.lower().endswith(".zip")]
    pdfs = [n for n in nomes if n.lower().endswith(".pdf")]
    return {
        "DO1E": {
            "zip": [n for n in zips if re.search(r"-DO1E\.zip$", n, re.I)],
            "pdf": [n for n in pdfs if re.search(r"_do1_extra", n, re.I)],
        },
        "DO1": {
            "zip": [n for n in zips if re.search(r"-DO1\.zip$", n, re.I)],
            "pdf": [n for n in pdfs if re.search(r"_do1\.pdf$", n, re.I)],
        },
    }


# ── Sessão do Inlabs COMPARTILHADA entre fetches ──
# O Inlabs LIMITA logins por minuto (medido 03/08/2026: rajada de 8 logins → TODOS
# recusados, e depois nem login solto passava por alguns minutos). O proativo
# checa vários dias por rodada e logava POR DIA (~12 logins) → derrubava a
# própria sessão; aí NADA passava, nem o /mp_dou_agora logo depois. A correção é
# LOGAR UMA VEZ e reusar o cookie (a sessão vai no header Cookie, então o mesmo
# cookie serve pra QUALQUER data) — corta de ~12 logins/rodada pra 1 — e NÃO
# re-tentar login em rajada (isso aprofundava o bloqueio).
_SESSION_LOCK = threading.Lock()
_SESSION: dict = {"cookie": None, "ts": 0.0}
_SESSION_TTL = 1800.0   # 30 min — cobre uma rodada inteira; renova sem esticar
# Tentativas de LISTAGEM por ciclo antes de desistir do cookie. A listagem é um
# GET (barato, NÃO conta no limite de LOGIN), então re-tentar o GET recupera o
# BLIP do Inlabs (serve tela de login numa requisição isolada mesmo logado —
# medido 03/08/2026: 02/08 falhou 1x e 03/08 funcionou nos GETs vizinhos) sem
# rajada de login (o que derrubava a sessão).
_LISTAGEM_TRIES = 3


def _invalidar_sessao() -> None:
    with _SESSION_LOCK:
        _SESSION["cookie"] = None


def _obter_cookie(email: str, password: str, *, force: bool = False) -> str:
    """Header Cookie COMPLETO da sessão do Inlabs (todos os cookies do login),
    REUSADO entre fetches. Loga só em cache-miss/expirado (ou force=True). UMA
    tentativa por chamada: re-tentar login em rajada aprofunda o rate-limit do
    Inlabs. Falhou? levanta DouError e o dia fica pendente pra próxima janela
    (~6h), com o limite já resetado. Serializado (lock): fetches concorrentes
    reusam o mesmo login, não logam em dobro.

    TODOS os cookies, não só o inlabs_session_cookie (medido 10/08/2026, probe
    no Pi): o login seta também PHPSESSID e dois TS* (WAF F5). Com só o cookie
    de sessão, pasta EXISTENTE responde normal — mas pasta INEXISTENTE cai na
    tela de login (a decisão "sem pasta → raiz ou login?" é pela PHPSESSID).
    Era por isso que fim de semana sem edição morria como "recusou a sessão"
    enquanto o dia corrente passava, DETERMINISTICAMENTE, não vaga-lume."""
    with _SESSION_LOCK:
        if (not force and _SESSION["cookie"]
                and time.monotonic() - _SESSION["ts"] < _SESSION_TTL):
            return _SESSION["cookie"]
        # cliente NOVO (conexão nova) — a recusa de sessão não passa na conexão
        # reusada (medido 01/08/2026).
        with httpx.Client(headers=_HEADERS, follow_redirects=True) as login:
            # AQUECIMENTO (11/08/2026): o WAF (F5) do Inlabs passou a barrar o
            # POST "frio" de login (5xx com página de manutenção FALSA)
            # enquanto o fluxo de navegador — GET da tela de login primeiro
            # (ganha os cookies TS) e POST com Referer/Origin — logava
            # normalmente na mesma janela. Imitamos o navegador; falha do
            # aquecimento não é fatal (quem decide é o POST).
            try:
                _inlabs_call(lambda: login.get(
                    f"{INLABS_BASE}/acessar.php", timeout=20.0), tries=1)
            except Exception:
                logger.warning("inlabs: aquecimento do login falhou; sigo pro POST")
            try:
                resp = _inlabs_call(lambda: login.post(
                    f"{INLABS_BASE}/logar.php",
                    data={"email": email, "password": password},
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Referer": f"{INLABS_BASE}/acessar.php",
                        "Origin": INLABS_BASE,
                    },
                    timeout=20.0,
                ))
                resp.raise_for_status()
            except DouError:
                raise  # InlabsMaintenanceError já traz mensagem clara
            except Exception as exc:
                raise DouError(f"falha ao autenticar no Inlabs: {exc}") from exc
            cookies = dict(login.cookies)
        if "inlabs_session_cookie" not in cookies:
            raise DouError(
                "o Inlabs recusou o login agora (não devolveu cookie) — costuma "
                "ser o limite de tentativas dele e passa em minutos. O dia fica "
                "pendente e é re-checado na próxima janela."
            )
        header = "; ".join(f"{k}={v}" for k, v in cookies.items())
        _SESSION["cookie"] = header
        _SESSION["ts"] = time.monotonic()
        return header


def _fetch_mps_sync(target_date: date) -> list[dict]:
    email = settings.inlabs_email
    password = settings.inlabs_password.get_secret_value() if settings.inlabs_password else None
    if not email or not password:
        raise DouError(
            "INLABS_EMAIL / INLABS_PASSWORD não configurados "
            "(cadastro gratuito em inlabs.in.gov.br)."
        )

    results: list[dict] = []
    seen_numeros: set[str] = set()
    with httpx.Client(headers=_HEADERS, follow_redirects=True) as client:
        date_str = target_date.strftime("%Y-%m-%d")

        def _listar(cookie: str) -> str:
            """Lista a pasta do dia com o cookie da sessão. A listagem servida é
            a PROVA de sessão viva — se veio, o cookie vale."""
            with _fase("listagem"):
                rl = _inlabs_call(lambda: client.get(
                    f"{INLABS_BASE}/index.php?p={date_str}",
                    headers={"Cookie": cookie}, timeout=60.0,
                ))
            rl.raise_for_status()
            return rl.text

        # 2 ciclos: (0) cookie do cache reusado; (1) UM login novo, só se o cookie
        # tiver mesmo vencido. Dentro do ciclo, re-tenta a LISTAGEM (GET barato)
        # pra absorver o blip do Inlabs. Recupera o caso 02/08 (blip isolado) SEM
        # rajada de login. Manutenção declarada tem precedência (pane, não blip).
        #
        # PASTA INEXISTENTE ≠ SESSÃO RECUSADA (medido 10/08/2026, probe no Pi):
        # pra `?p=<dia sem pasta>` o Inlabs serve a listagem RAIZ com HTTP 200 —
        # que também passa no _e_listagem. Sem distinguir raiz de pasta-do-dia,
        # o bug cortava dos DOIS lados: (a) fim de semana sem edição, com o
        # Inlabs instável, ficava preso na fila como "recusou a sessão" pra
        # sempre (08-09/08/2026); (b) pior, raiz servida por blip no lugar da
        # pasta de um dia QUE EXISTE seria lida como "sem edição" e o dia
        # fechado receberia baixa — MP perdida em silêncio. O desempate é pelas
        # datas de pasta no corpo: raiz referencia vários dias; a listagem da
        # pasta só carrega a própria data (nos nomes de arquivo).
        corpo: str | None = None
        raiz_sem_pasta = False
        raiz_com_pasta = False
        cookie = ""
        for ciclo in range(2):
            cookie = _obter_cookie(email, password, force=(ciclo == 1))
            for tentativa in range(_LISTAGEM_TRIES):
                body = _listar(cookie)
                if _MAINT_RE.search(body):
                    raise InlabsMaintenanceError(
                        "o Inlabs (sistema oficial do DOU) está em manutenção agora "
                        "— não dá pra checar as MPs. Tente mais tarde."
                    )
                if _e_listagem(body):
                    datas = _datas_no_corpo(body)
                    outras = datas - {date_str}
                    if not outras:
                        corpo = body   # listagem da PASTA do dia — segue o fluxo
                        break
                    # Veio a RAIZ (referencia outras datas), não a pasta do dia.
                    if date_str in datas:
                        # A pasta EXISTE na raiz e mesmo assim veio a raiz:
                        # blip — re-tenta; concluir "sem edição" aqui perderia
                        # o DOU do dia inteiro com baixa carimbada.
                        raiz_com_pasta = True
                    elif min(outras) < date_str:
                        # Raiz viva (sessão boa) cobrindo datas ANTERIORES ao
                        # alvo, sem a pasta dele: evidência positiva de que o
                        # dia não tem edição (domingo/feriado).
                        raiz_sem_pasta = True
                        break
                    # Raiz sem nenhuma data anterior ao alvo: não dá pra afirmar
                    # ausência (fora do range/paginação) — re-tenta e, se
                    # persistir, falha explícita.
                if tentativa < _LISTAGEM_TRIES - 1:
                    time.sleep(2)   # blip transitório — dá um beat e re-tenta o GET
            if corpo is not None or raiz_sem_pasta:
                break
            _invalidar_sessao()   # cookie deste ciclo não colou → login novo no próximo
        if raiz_sem_pasta:
            out = MPList()
            out.sem_edicao = True
            if not _dia_encerrado(target_date):
                # Dia aberto ainda pode ganhar a pasta (edição sai ao longo do
                # dia) — sem baixa por enquanto, mesma régua do 404 de seção.
                out.provisorio = True
                out.secoes_404 = tuple(DOU_SECTIONS)
            logger.info(
                "dou: %s sem pasta no Inlabs (raiz servida no lugar) — dia sem "
                "edição%s", date_str,
                "" if _dia_encerrado(target_date) else " ATÉ AGORA (dia aberto)",
            )
            return out
        if corpo is None:
            if raiz_com_pasta:
                # Não é sessão recusada: a sessão está viva (raiz veio), mas o
                # Inlabs insiste em servir a raiz no lugar da pasta que existe.
                raise DouError(
                    f"o Inlabs está servindo a listagem raiz no lugar da pasta "
                    f"do dia {date_str} (que existe lá) — não dá pra confirmar "
                    "se houve MP; o dia fica pendente e re-checo em seguida."
                )
            # Recusa mesmo com login novo e re-tentativas: FALHA explícita (dia
            # pendente, re-checado nas próximas janelas), NUNCA "não houve MP".
            raise DouError(
                "não consegui baixar o DOU — o Inlabs recusou a sessão "
                "(instabilidade/limite de login) — não dá pra confirmar se houve "
                "MP; tente de novo em instantes."
            )

        hdr = {"Cookie": cookie}

        def _baixar(nome: str) -> bytes:
            """Bytes de um arquivo QUE A LISTAGEM disse existir."""
            url = f"{INLABS_BASE}/index.php?p={date_str}&dl={nome}"
            r = _inlabs_call(lambda: client.get(url, headers=hdr, timeout=90.0))
            r.raise_for_status()
            return r.content

        nomes = _arquivos_da_listagem(corpo)
        fontes = _fontes_secao1(nomes)
        logger.info("dou: listagem %s — fontes Seção 1: %s", date_str,
                    {k: (v["zip"] or v["pdf"] or "—") for k, v in fontes.items()})

        failed_sections: list[str] = []
        sections_404: list[str] = []
        for section in DOU_SECTIONS:          # ["DO1E", "DO1"] — extra primeiro
            src = fontes[section]
            edicao = "Extra" if section == "DO1E" else "Normal"
            try:
                if src["zip"]:
                    # Caminho normal: XML no zip (texto mais limpo). Se houver
                    # zip, os PDFs gêmeos daquele slot são redundantes — não baixa.
                    for nome in src["zip"]:
                        content = _baixar(nome)
                        if len(content) < 100 or content[:2] != b"PK":
                            corpo_z = content.decode("utf-8", errors="replace")
                            if _MAINT_RE.search(corpo_z):
                                raise InlabsMaintenanceError(
                                    "Inlabs em manutenção (página servida com status 200)")
                            raise DouError(f"{nome}: conteúdo não-ZIP")
                        with _fase(f"unzip+parse {section}", zip_mb=round(len(content) / 1e6, 1)):
                            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                                for name in (n for n in zf.namelist() if n.lower().endswith(".xml")):
                                    xml_data = zf.read(name).decode("utf-8", errors="replace")
                                    if "MEDIDA PROVIS" not in xml_data.upper():
                                        continue
                                    for mp in _parse_dou_xml(client, xml_data, target_date):
                                        if mp["numero"] not in seen_numeros:
                                            seen_numeros.add(mp["numero"])
                                            mp["edicao"] = edicao
                                            results.append(mp)
                elif src["pdf"]:
                    # Sem zip, com PDF: a edição saiu SÓ em PDF (extra de sábado/
                    # feriado). Lê o texto do PDF e joga no MESMO produto do XML
                    # (número/ano/ementa; texto integral vem do Planalto).
                    for nome in src["pdf"]:
                        with _fase(f"download+pdf {section}"):
                            content = _baixar(nome)
                        if content[:4] != b"%PDF":
                            corpo_p = content.decode("utf-8", errors="replace")
                            if _MAINT_RE.search(corpo_p):
                                raise InlabsMaintenanceError(
                                    "Inlabs em manutenção (página servida com status 200)")
                            raise DouError(f"{nome}: conteúdo não-PDF")
                        texto = _extrair_texto_pdf(content)
                        if not texto:
                            # PDF ilegível é FALHA (pode esconder MP), não "vazio".
                            raise DouError(f"{nome}: PDF sem texto extraível")
                        if "MEDIDA PROVIS" not in texto.upper():
                            continue   # PDF legítimo sem MP (ex.: só Resolução/Ato)
                        for mp in _parse_dou_text(client, texto, target_date):
                            if mp["numero"] not in seen_numeros:
                                seen_numeros.add(mp["numero"])
                                mp["edicao"] = edicao
                                results.append(mp)
                else:
                    # Slot AUSENTE na listagem. É o "404" do modelo antigo: o dia
                    # pode ainda receber a edição (extra sai tarde). Quem decide se
                    # é ausência definitiva é `_dia_encerrado`, abaixo.
                    sections_404.append(section)
            except InlabsMaintenanceError:
                raise  # manutenção → mensagem clara, não é "seção incompleta"
            except zipfile.BadZipFile:
                logger.warning("dou: ZIP inválido em %s", section)
                failed_sections.append(section)
            except Exception as exc:  # 502/timeout após retries, conexão, PDF…
                logger.warning("dou: erro baixando/processando %s: %s", section, exc)
                failed_sections.append(section)

    # Falso negativo é o pior caso: se NÃO achamos nada MAS uma seção falhou de
    # verdade (≠ "não publicada"), não dá pra afirmar "sem MP" — levanta pro
    # caller dizer "não consegui checar" em vez de "nenhuma MP".
    if failed_sections and not results:
        raise DouError(
            f"não consegui baixar o DOU (seção(ões) {', '.join(failed_sections)} "
            "indisponíveis no Inlabs) — não dá pra confirmar se houve MP; "
            "tente de novo em instantes."
        )
    out = MPList(results)
    # "Não houve edição" (dia sem publicação — domingo/feriado) ≠ "houve DOU
    # sem MP nova". A distinção existe pro aviso não dizer "gerando a nota"/
    # "sem MP" num dia que nem teve Diário. É por FONTE de Seção 1 encontrada na
    # listagem, não por 0 MP (dia útil pode ter DOU e nenhuma MP).
    out.sem_edicao = not any(
        fontes[s]["zip"] or fontes[s]["pdf"] for s in DOU_SECTIONS
    )
    if sections_404 and not _dia_encerrado(target_date):
        # Dia ainda aberto: o 404 não prova ausência. NÃO é falha (nada de
        # aviso "não consegui checar" — seria alarme falso todo dia útil, já
        # que a DO1E só existe quando há edição extra); é só motivo pra não
        # dar baixa ainda. A retroativa re-checa e o dia fecha sozinho.
        out.provisorio = True
        out.secoes_404 = tuple(sections_404)
        logger.info("dou: %s — seção(ões) %s ausente(s) da listagem com o dia "
                    "ainda aberto; sem baixa (re-checa nas próximas janelas)",
                    target_date, sections_404)
    if failed_sections:
        out.incompleto = True
        out.secoes_falhas = tuple(failed_sections)
        logger.warning("dou: %s — seção(ões) %s falharam; lista INCOMPLETA "
                       "(dia segue pendente pra re-checagem)", target_date, failed_sections)
    return out


class MPList(list):
    """`list` de MPs com flag de COMPLETUDE.

    Uma seção do DOU pode falhar enquanto a outra responde: antes, a lista
    parcial voltava indistinguível de uma completa, o dia era dado como
    checado com sucesso e a pendência retroativa recebia baixa — MP publicada
    só na edição EXTRA daquele dia sumia em silêncio, pra sempre. Continua
    sendo uma list (nenhum consumidor quebra); quem precisa saber consulta
    `incompleto`.
    """

    incompleto: bool = False
    secoes_falhas: tuple[str, ...] = ()

    # 404 numa seção de um dia que AINDA PODE receber edição. Não é falha (não
    # vale aviso), mas também não é "não publicado" — é cedo demais pra saber.
    # Ver `_dia_encerrado`.
    provisorio: bool = False
    secoes_404: tuple[str, ...] = ()

    # Nenhuma fonte de Seção 1 (zip nem PDF) na listagem do dia: o Diário não
    # foi publicado (domingo/feriado). Distinto de "houve DOU, 0 MP". Combinado
    # com _dia_encerrado, é o que separa "não houve edição" (definitivo) de
    # "ainda não saiu" (provisorio).
    sem_edicao: bool = False


# ── cache single-flight do fetch ──
#
# Vários caminhos buscam o MESMO dia com minutos de diferença: o coletor da
# janela proativa, o job da nota (deliver_to_user re-busca), o /mp_dou_agora
# e cada usuário da casa separadamente. Cada busca re-baixa os ZIPs do dia
# (~100-200MB no Orange Pi) e dá ao Inlabs uma chance nova de recusar a
# sessão — foi assim que, em 03/08/2026, o proativo alarmou "não consegui
# checar o DOU" minutos depois de o comando manual ter checado com sucesso.
#
# Regras do cache (erram SEMPRE pro lado da premissa "não perder MP"):
# - Só resultado COMPLETO entra (incompleto=False). Falha/exceção NUNCA é
#   cacheada — re-tentar sempre vai à fonte.
# - TTL curto: janelas distam horas; o desperdício acontece em minutos.
# - Single-flight: chamadas concorrentes da mesma data esperam a primeira
#   em vez de baixar em paralelo (o caso coletor × job de nota).
# - O MPList cacheado é COMPARTILHADO entre chamadores — ninguém o muta
#   (deliver_to_user e collect_mp criam listas novas ao filtrar).
_FETCH_TTL_S = 10 * 60.0
_fetch_cache: dict[date, tuple[float, "MPList"]] = {}
_fetch_locks: dict[date, asyncio.Lock] = {}

# Última checagem COMPLETA de cada data: (quando em BRT, qtde de MPs vistas).
# Memória de processo, não banco: o que ela responde é "este processo checou
# esta data há pouco?" — exatamente o contexto que falta quando uma re-checagem
# falha minutos depois de uma OK. Restart zera, e tudo bem: sem checagem OK
# recente, o alarme forte é o comportamento certo mesmo.
_ultima_ok: dict[date, tuple[datetime, int]] = {}
_ULTIMA_OK_RETENCAO_DIAS = 30


def ultima_checagem_ok(d: date) -> tuple[datetime, int] | None:
    """(quando BRT, nº de MPs no DOU) da última checagem COMPLETA da data,
    ou None se este processo nunca a checou com sucesso."""
    return _ultima_ok.get(d)


def registrar_checagem_ok(d: date, n_mps: int) -> None:
    """Registra checagem COMPLETA da data vinda de QUALQUER fonte. Até
    11/08/2026 só o fetch do Inlabs alimentava esta memória — checagem
    conclusiva pelo PORTAL (a fonte primária) ficava invisível pro
    'já checado HH:MM' do /mp_fila e pro tom do aviso de re-checagem
    falhada (furo 4 da varredura do dono)."""
    _ultima_ok[d] = (datetime.now(BRT), n_mps)
    _podar_estado_fetch()


def _podar_estado_fetch() -> None:
    agora = time.monotonic()
    for d in [d for d, (t, _) in _fetch_cache.items() if agora - t >= _FETCH_TTL_S]:
        del _fetch_cache[d]
    corte = datetime.now(BRT).date() - timedelta(days=_ULTIMA_OK_RETENCAO_DIAS)
    for d in [d for d in _ultima_ok if d < corte]:
        del _ultima_ok[d]
    for d in [d for d, lk in _fetch_locks.items() if d < corte and not lk.locked()]:
        del _fetch_locks[d]


async def fetch_mps(target_date: date) -> list[dict]:
    """Busca MPs publicadas na data (Inlabs DO1E + DO1). Roda o I/O
    bloqueante numa thread pra não travar o event loop.

    Single-flight + cache por data (TTL de 10 min): resultado completo
    recente é reusado em vez de re-baixar os ZIPs — inclusive pelo
    /mp_dou_agora (dado ≤10min é "agora" pra efeito de DOU; falha nunca é
    cacheada, então re-checar depois de um erro sempre vai à fonte)."""
    lock = _fetch_locks.setdefault(target_date, asyncio.Lock())
    async with lock:
        hit = _fetch_cache.get(target_date)
        if hit is not None:
            idade = time.monotonic() - hit[0]
            if idade < _FETCH_TTL_S:
                logger.info("dou: fetch %s servido do cache (%.0fs de idade)",
                            target_date.isoformat(), idade)
                return hit[1]
        result = await asyncio.to_thread(_fetch_mps_sync, target_date)
        if not result.incompleto:
            _fetch_cache[target_date] = (time.monotonic(), result)
            registrar_checagem_ok(target_date, len(result))
        return result


# ──────────────────────── nota técnica (Claude) ────────────────────────

# Tabela da série de MPs 2026 — referência pro parágrafo de contexto cruzar
# conexões temáticas (replicado das diretrizes do assessor). Estático.
_SERIE_MPS_2026 = (
    "1.337 (06/03 Extra) Fundo Social — financiamento calamidade MG; "
    "1.338 (06/03 Extra) Apoio R$7.300/família Zona da Mata MG; "
    "1.339 (09/03) crédito extraordinário R$266,5mi defesa civil MG; "
    "1.340 (12/03) subvenção diesel + IE petróleo/diesel; "
    "1.341 (12/03) drawback cacau; "
    "1.342 (18/03) crédito extraordinário R$1,305bi SUAS+FAR+FGO+FS (MG); "
    "1.343 (19/03 Extra) piso mínimo frete — CIOT/RNTRC; "
    "1.344 (19/03 Extra) crédito extraordinário R$10bi ANP subvenção diesel; "
    "1.345 (25/03) Plano Brasil Soberano — FGE/FGCE + R$15bi BNDES; "
    "1.346 (27/03 Extra) crédito extraordinário R$20,4mi INCRA/PR; "
    "1.347 (27/03 Extra) crédito extraordinário R$285mi defesa civil; "
    "1.348 (06/04 Extra) FUNAPOL; "
    "1.349 (07/04 Extra) Regime Emergencial Combustíveis (R$31bi); "
    "1.350 (15/04 Extra) FGHab — MCMV/Reforma Casa Brasil; "
    "1.360 (19/05 Extra) Moto-frete — CTB + Lei 12.009/2009. "
    "Séries: Calamidade MG (1.337→1.338→1.339→1.342); "
    "Combustíveis (1.340→1.343→1.344→1.349); "
    "Defesa Civil (1.339→1.342→1.346→1.347)."
)

_NOTA_SYSTEM = (
    "Você é assessor legislativo sênior da Liderança do Podemos na Câmara dos "
    "Deputados, especializado em Medidas Provisórias do governo federal. "
    "Redige Notas Técnicas padronizadas, com rigor jurídico e linguagem "
    "técnico-legislativa densa e objetiva.\n\n"
    "REGRAS DE ESCRITA:\n"
    "- Tom técnico-legislativo, objetivo — sem opiniões pessoais nem floreios.\n"
    "- Leis pelo número e data completa: 'Lei nº 11.977, de 7 de julho de 2009'.\n"
    "- Dispositivos pelo número completo: \"art. 5º, § 1º-A, inciso II, alínea 'h'\".\n"
    "- Valores por extenso + algarismos: 'R$ 1.305.000.000,00 (um bilhão "
    "trezentos e cinco milhões de reais)'.\n"
    "- Use travessões (—) para explicações incidentais.\n"
    "- Cite MPs correlatas pelo número e ano quando aplicável.\n"
    "- NÃO mencione quem assinou ou referendou a MP (Presidente, ministros).\n"
    "- Baseie afirmações de contexto APENAS no dossiê de pesquisa e no texto "
    "fornecidos. NUNCA invente dados, valores ou falas.\n\n"
    "REGRA DE IMPACTO (a mais importante — não a viole):\n"
    "Para CADA dispositivo, NÃO basta dizer o que mudou no texto da lei. "
    "Você DEVE explicar o EFEITO PRÁTICO da mudança:\n"
    "- Quem é afetado (cidadãos, empresas, entes federativos, setor X) e como.\n"
    "- O que passa a ser permitido, exigido, vedado ou facilitado na prática.\n"
    "- Quais direitos, obrigações, sanções ou mecanismos novos são criados.\n"
    "- O histórico da lei alterada e de MPs/leis anteriores que já a "
    "modificaram, para situar a mudança.\n"
    "- As normas infralegais (decreto, portaria, resolução) ainda necessárias "
    "para operacionalizar o dispositivo.\n"
    "Descrição que apenas parafraseia 'acrescenta o inciso X ao art. Y' SEM "
    "explicar o que isso muda na vida real é INSUFICIENTE e deve ser evitada.\n\n"
    "TIPOLOGIAS (use a checklist da que se aplicar; podem coexistir numa "
    "mesma MP — cubra todas as que incidirem):\n"
    "A. ALTERAÇÃO DE LEI(S) — para cada dispositivo alterado:\n"
    "   (i) dispositivo (lei + artigo/§/inciso); (ii) o que muda no texto;\n"
    "   (iii) efeito prático; (iv) histórico da lei (MPs/leis anteriores que\n"
    "   já a modificaram); (v) direitos, obrigações, sanções ou mecanismos\n"
    "   criados; (vi) normas infralegais ainda necessárias.\n"
    "B. CRIAÇÃO DE REGIME/PROGRAMA:\n"
    "   (i) estrutura por capítulos/eixos; (ii) operador (quem executa);\n"
    "   (iii) fiscalizador (quem controla); (iv) prazos, limites, sanções;\n"
    "   (v) mecanismos inéditos vs. já existentes na ordem jurídica;\n"
    "   (vi) infralegais pendentes para operacionalizar.\n"
    "C. SUBVENÇÃO ECONÔMICA:\n"
    "   (i) valor por unidade (R$/litro, R$/tonelada, R$/kWh, etc.);\n"
    "   (ii) limite global; (iii) beneficiários elegíveis; (iv) operador\n"
    "   (ANP, BNDES, BB, Caixa, etc.); (v) vigência e regra de prorrogação;\n"
    "   (vi) mecanismo de apuração; (vii) condicionantes (repasse ao\n"
    "   consumidor, habilitação prévia, comprovação, prestação de contas).\n\n"
    "ESTRUTURA (5 parágrafos):\n"
    "1. CONTEXTO — por que a MP foi editada: evento motivador, dados "
    "quantitativos, atores políticos, MPs correlatas anteriores, conexão "
    "econômica/geopolítica.\n"
    "2. DISPOSITIVOS CENTRAIS — principais artigos: citação precisa + efeito "
    "jurídico E PRÁTICO (aplique a REGRA DE IMPACTO), valores/prazos/limites. "
    "Para crédito extraordinário, detalhe o Anexo (órgão/UO, programa, ação, "
    "GND, fonte, localização, estimativa física) e cite o art. 167, §3º, CF.\n"
    "3. CONTINUAÇÃO — dispositivos adicionais, disposições transitórias, "
    "alterações em outras leis (com efeito prático e histórico), normas "
    "infralegais necessárias.\n"
    "4. SÍNTESE — quadro-resumo: distribuição de recursos, eixos da MP, "
    "natureza da despesa (investimento vs. custeio), e o impacto consolidado.\n"
    "5. FECHAMENTO — contexto político, conexão com pacote normativo mais "
    "amplo, impacto fiscal, expectativas de regulamentação.\n\n"
    "Cada parágrafo de análise deve ser denso e substantivo (várias frases). "
    "Se a MP for curta, deixe parágrafos 3-5 vazios (string vazia) em vez de "
    "encher de linguiça. Se for extensa, adense.\n\n"
    f"SÉRIE DE MPs 2026 (para cruzar referências no contexto): {_SERIE_MPS_2026}"
)

_NOTA_TOOL = {
    "name": "nota_tecnica",
    "description": "Emite o conteúdo estruturado da Nota Técnica da Medida Provisória.",
    "input_schema": {
        "type": "object",
        "properties": {
            "ementa": {
                "type": "string",
                "description": "Ementa oficial da MP, limpa, sem aspas.",
            },
            "p1_contexto": {"type": "string", "description": "Parágrafo 1 — contexto."},
            "p2_dispositivos": {"type": "string", "description": "Parágrafo 2 — dispositivos centrais."},
            "p3_continuacao": {"type": "string", "description": "Parágrafo 3 — continuação (vazio se MP curta)."},
            "p4_sintese": {"type": "string", "description": "Parágrafo 4 — síntese (vazio se MP curta)."},
            "p5_fechamento": {"type": "string", "description": "Parágrafo 5 — fechamento (vazio se MP curta)."},
        },
        "required": ["ementa", "p1_contexto", "p2_dispositivos"],
    },
}

# Só web_search com teto BAIXO de usos. SEM web_fetch (baixava páginas inteiras
# → tokens explodiam). max_uses=2: prompt pesado + busca sem teto fazia o modelo
# pesquisar/processar >100s e estourar; 2 buscas + prompt curto fecham rápido.
_WEB_TOOLS = [
    {"type": "web_search_20260209", "name": "web_search", "max_uses": 2},
]


async def _pesquisar_contexto(client, mp: dict, *, model: str | None = None) -> str:
    """Fase 1: pesquisa contexto político/noticioso da MP via web search.
    Retorna um dossiê textual (ou string vazia se desligado/indisponível).
    Limitado no tempo pra não pendurar a entrega. `model` segue o override
    do /dou_provider (default ANTHROPIC_MODEL)."""
    if not settings.dou_mp_web_research:
        return ""
    model = model or settings.anthropic_model
    # Prompt CURTO e expectativa baixa: MP recém-publicada quase não tem
    # cobertura web, e pedir 'dossiê com 5 categorias, só fatos com fonte' fazia
    # o modelo pesquisar/processar indefinidamente (>100s, estourava o timeout).
    # Pouca coisa, rápido, e tudo bem se vier vazio.
    prompt = (
        f"Em no máximo 2 buscas rápidas, traga o contexto essencial da Medida "
        f"Provisória nº {mp['numero']}/{mp['ano']} (DOU {mp['data_publicacao']}). "
        f"Ementa: {mp['ementa']}\n"
        "Foco: por que foi editada e valores/impacto, se houver cobertura. "
        "Responda em 3-6 tópicos curtos com fonte. Se não achar nada relevante "
        "(MP nova costuma não ter), responda só 'sem cobertura web ainda'."
    )
    # create (não streaming): com max_uses=2 + prompt curto a requisição é curta
    # e não esbarra no guard de 'requisição longa' do SDK; o timeout aperta o
    # resto. Sem loop de pause_turn (raro com max_uses=2; se vier, o texto pode
    # estar vazio → fallback gracioso).
    bounded = client.with_options(timeout=50.0, max_retries=1)
    try:
        resp = await asyncio.wait_for(
            bounded.messages.create(
                model=model,
                max_tokens=1024,
                tools=_WEB_TOOLS,
                messages=[{"role": "user", "content": prompt}],
            ),
            timeout=55.0,
        )
        return "\n".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    except Exception as exc:
        logger.warning("dou: pesquisa web indisponível/lenta p/ MP %s (%s); seguindo sem dossiê",
                       mp["numero"], exc)
        return ""


def _nota_user_content(mp: dict, dossie: str) -> str:
    return (
        # Default HONESTO: 'Normal' silencioso rotulou a MP 1.382 (extra de
        # sábado) como edição normal no prompt — e o LLM repetiu na nota.
        f"MP nº {mp['numero']}/{mp['ano']} — Edição {mp.get('edicao') or 'não identificada'} "
        f"do DOU de {mp['data_publicacao']}\n"
        f"Ementa: {mp['ementa']}\n"
        f"URL: {mp.get('url_planalto', 'N/A')}\n\n"
        f"=== DOSSIÊ DE PESQUISA (contexto; use só o que tiver fonte) ===\n"
        f"{dossie or '(pesquisa web indisponível — baseie-se apenas no texto da MP)'}\n\n"
        f"=== TEXTO INTEGRAL DA MP ===\n"
        # Cap moderado (50k chars ≈ 12k tokens) — cobre a esmagadora maioria
        # das MPs sem inflar o prompt; megapacote (>50k chars de texto legal)
        # é o caso raro, fica truncado mas mantém prazos+ementa+inicio.
        f"{(mp.get('texto_integral') or 'Não disponível')[:50000]}\n\n"
        "Emita a nota técnica seguindo a estrutura de 5 parágrafos."
    )


async def generate_nota_tecnica(
    mp: dict, *, provider: str | None = None, model: str | None = None,
) -> dict | None:
    """Gera o conteúdo da nota técnica (pesquisa + redação estruturada).
    `provider`/`model` são overrides por usuário (/dou_provider); quando None,
    segue DOU_MP_PROVIDER + DOU_MP_GEMINI_MODEL/ANTHROPIC_MODEL do .env. O
    `model` é interpretado conforme o provider efetivo (id gemini-* ou
    claude-*). Retorna {ementa, p1_contexto, ...} ou None se falhar."""
    prov = (provider or settings.dou_mp_provider).lower()
    if prov == "gemini":
        return await _gen_nota_gemini(mp, model_override=model)
    return await _gen_nota_anthropic(mp, model_override=model)


# ── Anthropic (Claude + web_search) ──

async def _gen_nota_anthropic(mp: dict, *, model_override: str | None = None) -> dict | None:
    if not settings.anthropic_api_key:
        logger.warning("dou: ANTHROPIC_API_KEY ausente; nota pulada")
        return None
    model = model_override or settings.anthropic_model
    try:
        import anthropic
    except ImportError:
        return None
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    dossie = await _pesquisar_contexto(client, mp, model=model)
    user_content = _nota_user_content(mp, dossie)
    try:
        resp = await client.with_options(timeout=240.0, max_retries=1).messages.create(
            model=model,
            max_tokens=16384,
            system=[{"type": "text", "text": _NOTA_SYSTEM, "cache_control": {"type": "ephemeral"}}],
            tools=[_NOTA_TOOL],
            tool_choice={"type": "tool", "name": "nota_tecnica"},
            messages=[{"role": "user", "content": user_content + " Chame a ferramenta nota_tecnica."}],
        )
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use":
                return block.input
        return None
    except Exception:
        logger.exception("dou: falha (anthropic) na nota MP %s/%s", mp["numero"], mp["ano"])
        return None


# ── Gemini (Flash + Google Search grounding + JSON mode) ──

_GEMINI_SCHEMA_PROPS = {
    "ementa": "Ementa oficial da MP, limpa, sem aspas.",
    "p1_contexto": "Parágrafo 1 — contexto (por que a MP foi editada).",
    "p2_dispositivos": "Parágrafo 2 — dispositivos centrais.",
    "p3_continuacao": "Parágrafo 3 — continuação (vazio se MP curta).",
    "p4_sintese": "Parágrafo 4 — síntese (vazio se MP curta).",
    "p5_fechamento": "Parágrafo 5 — fechamento (vazio se MP curta).",
}


def _gemini_models(primary: str | None = None) -> list[str]:
    """Modelo principal + fallback (sem repetir). `primary` sobrepõe o
    DOU_MP_GEMINI_MODEL do .env (override por usuário via /dou_provider);
    o fallback continua sempre o do .env."""
    main = primary or settings.dou_mp_gemini_model
    models = [main]
    fb = settings.dou_mp_gemini_model_fallback
    if fb and fb != main:
        models.append(fb)
    return models


def _is_quota_error(exc: Exception) -> bool:
    s = str(exc).lower()
    return any(t in s for t in ("429", "resource_exhausted", "quota", "rate limit"))


def _is_transient_or_recoverable(exc: Exception) -> bool:
    """Erros que valem retry no próximo modelo: cota (429), sobrecarga (503),
    timeout, OU JSON truncado (resposta cortada pelo max_output_tokens —
    o thinking automático do 3.5-flash come o orçamento e a saída fica
    incompleta). Em todos esses casos vale a pena tentar o fallback."""
    if _is_quota_error(exc):
        return True
    s = str(exc).lower()
    if any(t in s for t in ("503", "unavailable", "overloaded", "high demand", "deadline", "timeout")):
        return True
    if isinstance(exc, (json.JSONDecodeError,)) or "jsondecodeerror" in s or "unterminated string" in s:
        return True
    return False


async def _pesquisar_contexto_gemini(client, mp: dict, *, model_override: str | None = None) -> str:
    if not settings.dou_mp_web_research:
        return ""
    from google.genai import types
    prompt = (
        f"Pesquise o contexto da Medida Provisória nº {mp['numero']}, de "
        f"{mp['ano']} (DOU de {mp['data_publicacao']}). Ementa: {mp['ementa']}. "
        "Busque evento motivador, atores políticos, valores/dados, MPs "
        "correlatas e reações. Responda um dossiê objetivo em tópicos, só "
        "fatos com fonte."
    )

    def _call(model: str) -> str:
        # budget=-1: nenhum thinking_config no corpo (é o comportamento atual
        # — a pesquisa se beneficia de raciocínio). Passa pelo `gerar` mesmo
        # assim pra manter a invariante "ninguém chama a API direto": é o que
        # garante o log do payload quando o Gemini recusar algo aqui.
        resp = gerar(
            client, model, prompt, "dou:pesquisa", budget=-1,
            tools=[types.Tool(google_search=types.GoogleSearch())],
            max_output_tokens=1500,
        )
        return (resp.text or "").strip()

    models_p = _gemini_models(model_override)
    for model in models_p:
        try:
            return await asyncio.wait_for(asyncio.to_thread(_call, model), timeout=120.0)
        except Exception as exc:
            if _is_transient_or_recoverable(exc) and model != models_p[-1]:
                logger.warning(
                    "dou: pesquisa gemini %s falhou (%s); tentando fallback",
                    model, type(exc).__name__,
                )
                continue
            logger.warning("dou: pesquisa web (gemini) indisponível p/ MP %s (%s)", mp["numero"], exc)
            return ""
    return ""


async def _gen_nota_gemini(mp: dict, *, model_override: str | None = None) -> dict | None:
    if not settings.gemini_api_key:
        logger.warning("dou: GEMINI_API_KEY ausente; nota pulada")
        return None
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return None
    client = genai.Client(api_key=settings.gemini_api_key)
    dossie = await _pesquisar_contexto_gemini(client, mp, model_override=model_override)
    user_content = _nota_user_content(mp, dossie)

    schema = types.Schema(
        type=types.Type.OBJECT,
        properties={
            k: types.Schema(type=types.Type.STRING, description=v)
            for k, v in _GEMINI_SCHEMA_PROPS.items()
        },
        required=["ementa", "p1_contexto", "p2_dispositivos"],
    )

    def _call(model: str) -> str:
        # max_output_tokens alto + thinking_budget=0: o thinking automático
        # do 3.5-flash/3.1 consome o orçamento de saída e o JSON estruturado
        # vem TRUNCADO (JSONDecodeError "Unterminated string"). Desligar o
        # thinking devolve todos os tokens pra resposta.
        # budget=0 pelo `gerar`: mantém o thinking desligado (motivo acima) MAS
        # com queda automática — modelo que recuse o 0 responde sem o ajuste em
        # vez de derrubar a nota inteira com 400 INVALID_ARGUMENT.
        resp = gerar(
            client, model, user_content, "dou:nota", budget=0,
            system_instruction=_NOTA_SYSTEM,
            response_mime_type="application/json",
            response_schema=schema,
            max_output_tokens=16384,
        )
        return (resp.text or "").strip()

    models = _gemini_models(model_override)
    last_exc: Exception | None = None
    for model in models:
        try:
            raw = await asyncio.wait_for(asyncio.to_thread(_call, model), timeout=120.0)
            data = json.loads(raw)
            if not data.get("p1_contexto") or not data.get("p2_dispositivos"):
                logger.warning("dou: nota gemini incompleta MP %s/%s (%s)", mp["numero"], mp["ano"], model)
            logger.info("dou: nota gerada MP %s/%s via %s", mp["numero"], mp["ano"], model)
            return data
        except Exception as exc:
            last_exc = exc
            if _is_transient_or_recoverable(exc) and model != models[-1]:
                logger.warning(
                    "dou: nota gemini %s falhou (%s) MP %s; tentando fallback",
                    model, type(exc).__name__, mp["numero"],
                )
                continue
            logger.exception("dou: falha (gemini/%s) na nota MP %s/%s", model, mp["numero"], mp["ano"])
            return None
    if last_exc is not None:
        logger.warning("dou: cadeia gemini esgotada na nota MP %s: %s", mp["numero"], last_exc)
    return None


# ──────────────────────── prazos + formatação ────────────────────────

# Recesso parlamentar (CF art. 57: sessão legislativa de 2/2 a 17/7 e de 1/8 a
# 22/12) → recesso de 18 a 31 de julho e de 23/dez a 1º/fev. Durante o recesso os
# prazos CONSTITUCIONAIS da MP ficam SUSPENSOS: a eficácia por força do art. 62,
# §4º, e o sobrestamento (§6º) pela prática do Congresso (as tabelas oficiais de
# prazo constitucional já descontam o recesso). O prazo de emendas (Res. 1/2002-
# CN) é regimental e corre em dias corridos.
def _em_recesso(d: date) -> bool:
    if d.month == 7:
        return d.day >= 18
    if d.month == 12:
        return d.day >= 23
    if d.month == 1:
        return True
    if d.month == 2:
        return d.day == 1
    return False


def _prazo_suspenso_recesso(pub: date, dias: int) -> date:
    """`pub` + `dias`, mas dias de recesso NÃO contam (prazo suspenso). Fora de
    qualquer recesso, equivale exatamente a `pub + timedelta(days=dias)`."""
    d = pub
    contados = 0
    while contados < dias:
        d += timedelta(days=1)
        if not _em_recesso(d):
            contados += 1
    return d


def compute_prazos(pub: date) -> dict:
    """Prazos regimentais a partir da publicação no DOU."""
    return {
        # Constitucionais: suspensos durante o recesso (art. 62, §4º).
        "eficacia_fim": _prazo_suspenso_recesso(pub, 59),      # 60 dias
        "sobrestamento": _prazo_suspenso_recesso(pub, 45),
        # 6 dias contados da publicação (Res. 1/2002-CN, art. 4º, §1º): o dia da
        # publicação não conta — ex.: DOU 23/06 → emendas até 29/06. Regimental,
        # em dias corridos (não sofre a suspensão do recesso).
        "emendas_fim": pub + timedelta(days=6),
    }


def _br(d: date) -> str:
    return d.strftime("%d/%m/%Y")

_MESES_PT = {
    1: "janeiro", 2: "fevereiro", 3: "março", 4: "abril", 5: "maio", 6: "junho",
    7: "julho", 8: "agosto", 9: "setembro", 10: "outubro", 11: "novembro", 12: "dezembro",
}


def _data_extenso(d: date) -> str:
    dia = "1º" if d.day == 1 else str(d.day)
    return f"{dia} de {_MESES_PT[d.month]} de {d.year}"


def _num_fmt(numero: str) -> str:
    """1361 -> '1.361' (milhar com ponto)."""
    try:
        return f"{int(numero):,}".replace(",", ".")
    except ValueError:
        return numero


def format_telegram_message(mp: dict, nota: dict | None) -> str:
    pub = date.fromisoformat(mp["data_publicacao"])
    prazos = compute_prazos(pub)
    titulo = f"Medida Provisória nº {_num_fmt(mp['numero'])}, de {mp['ano']}"
    resumo = (nota or {}).get("p1_contexto") or (nota or {}).get("ementa") or mp.get("ementa") or "(sem ementa)"
    lines = [
        f"📄 <b>{titulo}</b>",
        # Sem rótulo "Edição Extra/Normal": a distinção não tem relevância pro
        # dono (regra dele) — o que importa é que HÁ uma MP. `edicao` segue no
        # dict (a nota técnica .docx cita "Edição Extra" por convenção legal).
        f"<i>Diário Oficial de {_br(pub)}</i>",
        "",
        resumo,
        "",
        "⏱️ <b>Prazos</b>",
        f"• Emendas até <b>{_br(prazos['emendas_fim'])}</b> (23h59min)",
        f"• Sobrestamento de pauta a partir de {_br(prazos['sobrestamento'])}",
        f"• Eficácia até {_br(prazos['eficacia_fim'])} (prorrogável +60 dias)",
        "",
        f'<a href="{mp.get("url_planalto", "")}">texto no Planalto</a>',
    ]
    if nota:
        lines.append("\n📎 Nota técnica completa no anexo (.docx).")
    return "\n".join(lines)


_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "..", "assets", "nota_template.docx")


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def build_docx(mp: dict, nota: dict | None) -> bytes:
    """Preenche o template institucional do Podemos (placeholders {{...}})
    com os dados da MP + nota técnica. Retorna os bytes do .docx."""
    nota = nota or {}
    pub = date.fromisoformat(mp["data_publicacao"])
    prazos = compute_prazos(pub)
    num = _num_fmt(mp["numero"])
    edicao_txt = "Edição Extra" if mp.get("edicao") == "Extra" else "Edição"
    ementa = nota.get("ementa") or mp.get("ementa") or ""

    intro = (
        f"A {edicao_txt} do Diário Oficial da União de {_data_extenso(pub)} "
        f"publicou a Medida Provisória nº {num}/{mp['ano']}, que "
    )
    paragrafos = [
        nota.get("p1_contexto") or ementa,
        nota.get("p2_dispositivos") or "",
        nota.get("p3_continuacao") or " ",
        nota.get("p4_sintese") or " ",
        nota.get("p5_fechamento") or " ",
    ]

    repl = {
        "{{EFICACIA}}": f"{_br(pub)} a {_br(prazos['eficacia_fim'])}, prorrogável por mais 60 dias",
        "{{SOBRESTAMENTO}}": _br(prazos["sobrestamento"]),
        "{{EMENDAS_RANGE}}": f"{_br(pub)} a {_br(prazos['emendas_fim'])}",
        "{{EMENDAS_FIM}}": _br(prazos["emendas_fim"]),
        "{{INTRO}}": intro,
        "{{EMENTA}}": ementa,
        "{{NUM_FULL}}": num,
        "{{P1}}": paragrafos[0],
        "{{P2}}": paragrafos[1],
        "{{P3}}": paragrafos[2],
        "{{P4}}": paragrafos[3],
        "{{P5}}": paragrafos[4],
    }

    with zipfile.ZipFile(_TEMPLATE_PATH) as zin:
        names = zin.namelist()
        contents = {n: zin.read(n) for n in names}

    for target in ("word/document.xml", "word/header1.xml"):
        xml = contents[target].decode("utf-8")
        for token, value in repl.items():
            xml = xml.replace(token, _xml_escape(value))
        contents[target] = xml.encode("utf-8")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for n in names:
            zout.writestr(n, contents[n])
    return buf.getvalue()


def docx_filename(mp: dict) -> str:
    return f"NOTA_TÉCNICA_-_MPV_nº_{mp['numero']}_de_{mp['ano']}.docx"


# ──────────────────────── dedup ────────────────────────

async def filter_unseen(session: AsyncSession, user_id: int, mps: list[dict]) -> list[dict]:
    if not mps:
        return []
    rows = await session.scalars(
        select(DouSeenMP).where(DouSeenMP.user_id == user_id)
    )
    seen = {(r.numero, r.ano) for r in rows}
    return [mp for mp in mps if (mp["numero"], mp["ano"]) not in seen]


# Folga pra Câmara registrar a MP publicada. Sem ela, a MP de hoje apareceria
# como "faltando" só porque o registro lá ainda não entrou — alarme falso que
# treina o dono a ignorar o aviso.
_FOLGA_CAMARA_DIAS = 2
# Até onde olhar pra trás. Não é o histórico inteiro: MP anterior à assinatura
# do monitor nunca foi "perdida", e listar tudo viraria enxurrada na 1ª rodada.
_JANELA_CONFERENCIA_DIAS = 30


async def mps_nao_recebidas(
    session: AsyncSession, user_id: int, hoje: date,
) -> list[dict]:
    """MPs que a Câmara registrou e o bot NUNCA entregou a este usuário.

    Existe porque toda a detecção depende de uma fonte só (Inlabs), e o modo
    de falha mais perigoso dela é silencioso: 404 em arquivo que existe, ou ZIP
    válido porém truncado. Nesses casos o bot conclui "não houve MP" e não há
    nada no estado dele que denuncie o buraco — só a comparação com uma fonte
    independente.

    Levanta CamaraError se a API falhar; o caller reporta (nunca vira silêncio,
    senão a conferência que existe pra achar falso negativo criaria um).
    """
    from bot.services.camara import mpvs_do_ano

    inicio = hoje - timedelta(days=_JANELA_CONFERENCIA_DIAS)
    limite = hoje - timedelta(days=_FOLGA_CAMARA_DIAS)
    anos = {inicio.year, hoje.year}          # cobre a virada de ano
    candidatas: list[dict] = []
    for ano in sorted(anos):
        candidatas += [
            mp for mp in await mpvs_do_ano(ano)
            if inicio <= mp["data"] <= limite
        ]
    if not candidatas:
        return []

    rows = await session.scalars(
        select(DouSeenMP).where(DouSeenMP.user_id == user_id)
    )
    recebidas = {(r.numero, r.ano) for r in rows}
    # DouSeenMP só registra MP com NOTA entregue. A detecção normal do proativo
    # avisa a MP e grava ProactiveNotice(kind="mp") — sem isso, MP que o dono
    # leu no briefing e dispensou a nota ("Não" no botão) seria acusada de
    # perdida, com dia enfileirado e ZIPs re-baixados à toa. As duas fontes
    # juntas respondem "o dono FICOU SABENDO desta MP?", que é a pergunta.
    avisos = await session.scalars(
        select(ProactiveNotice).where(
            ProactiveNotice.user_id == user_id,
            ProactiveNotice.kind == "mp",
        )
    )
    for r in avisos:
        num, _, ano = (r.key or "").partition("/")
        try:
            recebidas.add((num, int(ano)))
        except ValueError:
            logger.warning("dou: chave de aviso de MP inesperada: %r", r.key)
    faltando = [
        mp for mp in candidatas if (mp["numero"], mp["ano"]) not in recebidas
    ]
    if faltando:
        logger.warning(
            "dou: conferência achou %d MP(s) na Câmara que o usuário %s não "
            "recebeu: %s", len(faltando), user_id,
            ", ".join(f"{mp['numero']}/{mp['ano']}" for mp in faltando),
        )
    return sorted(faltando, key=lambda mp: mp["data"])


async def mark_seen(session: AsyncSession, user_id: int, mp: dict) -> None:
    session.add(DouSeenMP(user_id=user_id, numero=mp["numero"], ano=mp["ano"]))
    await session.commit()


# ──────────────────────── entrega (mensagem + DOCX) ────────────────────────

async def _pendencias_da_data(session: AsyncSession, user_id: int, d: date) -> list[str]:
    """Chaves de nota_pendente já existentes para a data (key = "data:nums")."""
    rows = await session.scalars(
        select(ProactiveNotice).where(
            ProactiveNotice.user_id == user_id,
            ProactiveNotice.kind == "nota_pendente",
        )
    )
    return [r.key for r in rows if (r.key or "").startswith(f"{d.isoformat()}:")]


async def _abrir_outbox(
    session: AsyncSession, user_id: int, d: date, avisadas: list[dict],
) -> str | None:
    """Garante que existe pendência da data ANTES de gerar as notas.

    Devolve a chave criada aqui, ou None quando já havia uma (caso do job da
    fila, que tem a sua) — assim não aparecem duas linhas de status pro mesmo
    dia nem se desfaz a entrada de outro dono.
    """
    if not avisadas:
        return None
    from bot.services.proactive import mark_notified
    if await _pendencias_da_data(session, user_id, d):
        return None
    chave = f"{d.isoformat()}:{','.join(mp['numero'] for mp in avisadas)}"
    await mark_notified(session, user_id, "nota_pendente", chave)
    logger.info("dou: outbox aberto %s (%d nota[s] a gerar)", chave, len(avisadas))
    return chave


async def _fechar_outbox(
    session: AsyncSession, user_id: int, d: date, chave: str | None,
    falhas: list[str],
) -> None:
    """Fecha a pendência que ESTE deliver criou; converte falhas em fila.

    POSSE: só age quando `chave` não é None, ou seja, quando foi este deliver
    que ABRIU o outbox (caminho do handler /mp_dou_agora). Numa re-tentativa da
    fila a entrada é pré-existente (`chave` None) e quem manda é o chamador
    (`_entregar_nota_pendente`): ele mantém na falha e baixa no sucesso. Mexer
    aqui nesse caso brigaria com ele e causaria baixa indevida ou key drift.
    """
    if chave is None:
        return
    from bot.services.proactive import mark_notified, unmark_notified
    await unmark_notified(session, user_id, "nota_pendente", chave)
    if not falhas:
        return
    chave_falha = f"{d.isoformat()}:{','.join(falhas)}"
    if chave_falha not in await _pendencias_da_data(session, user_id, d):
        await mark_notified(session, user_id, "nota_pendente", chave_falha)
    logger.warning("dou: %d nota(s) falharam em %s — de volta pra fila (%s)",
                   len(falhas), d, chave_falha)


def texto_sem_mp(motivo: str | None, target: date) -> str:
    """Frase de desfecho quando a busca não trouxe MP — distingue 'não houve
    edição' (domingo/feriado) de 'houve DOU sem MP' de 'ainda pode sair'. Antes
    tudo isso virava um único "Nenhuma MP encontrada", que num domingo soava
    como se o bot não tivesse checado (ou pior, prometia nota que nunca vinha)."""
    dia = target.strftime("%d/%m/%Y")
    if motivo == "sem_edicao":
        return (f"📭 Não houve edição do Diário Oficial em {dia} "
                "(dia sem publicação — fim de semana/feriado).")
    if motivo == "provisorio":
        return (f"⏳ O Diário Oficial de {dia} ainda pode sair (dia em aberto). "
                "Re-checo sozinho e te aviso se vier MP.")
    if motivo == "incompleto":
        return (f"⚠️ Não consegui confirmar o DOU de {dia} agora (fonte "
                "incompleta) — deixei pra re-checar; te aviso se vier MP.")
    # sem_mp e sem_mp_extra (houve Diário, 0 MP — dia fechado OU aberto): mensagem
    # ENXUTA. Sem ✅ (confundia com "saiu MP") e sem framing de edição extra
    # (exceção, nem sempre vem — regra do dono). Se a extra vier com MP, a
    # checagem das 19h entrega na hora; não precisa prometer no texto.
    return f"Nenhuma MP nova no Diário Oficial de {dia}."


async def gerar_e_enviar_nota(bot, user, mp: dict, *, caption_extra: str | None = None) -> None:
    """Pipeline de UMA nota (pesquisa + redação + DOCX + envio). Levanta em
    falha — o caller decide fila/aviso. CHAME SOB O _SEM_NOTA (a função não o
    adquire de propósito: o deliver_to_user já o segura no laço dele, e
    semáforo de asyncio não é reentrante).

    `caption_extra` marca origem alternativa (ex.: texto vindo do portal
    público com o Inlabs fora) — origem dita, regra do projeto."""
    from aiogram.types import BufferedInputFile
    logger.info("dou: gerando nota técnica MP %s/%s…", mp["numero"], mp["ano"])
    with _fase(f"nota MP {mp['numero']} (pesquisa+redação)"):
        nota = await generate_nota_tecnica(
            mp,
            provider=getattr(user, "dou_mp_provider", None),
            model=getattr(user, "dou_mp_model", None),
        )
    with _fase(f"docx MP {mp['numero']}"):
        docx_bytes = await asyncio.to_thread(build_docx, mp, nota)
    partes = []
    if caption_extra:
        partes.append(caption_extra)
    if not nota:
        partes.append("⚠️ Nota gerada sem análise da IA (texto base).")
    await bot.send_document(
        user.id,
        BufferedInputFile(docx_bytes, filename=docx_filename(mp)),
        caption="\n".join(partes) or None,
    )


async def deliver_to_user(
    bot, session: AsyncSession, user, target_date: date, *, force: bool = False,
    only_numeros: list[str] | None = None,
) -> tuple[int, list[str], str | None]:
    """Busca MPs da data, gera nota + DOCX e entrega no Telegram. Por padrão
    pula as já notificadas (dedup); com force=True entrega tudo que achar
    (usado pelo comando manual). only_numeros restringe a entrega a essas MPs
    (números) — usado pelo botão do proativo, que avisa de um subconjunto do dia
    e não deve regerar todas.

    Retorna (entregues, falhas, motivo): `motivo` é None quando houve entrega;
    quando entregues==0, diz POR QUÊ — "sem_edicao" (dia fechado sem Diário:
    domingo/feriado), "sem_mp" (houve DOU fechado, nenhuma MP), "sem_mp_extra"
    (DOU saiu sem MP mas o dia segue aberto — extra ainda pode trazer MP),
    "provisorio" (nada saiu ainda, dia aberto) ou "incompleto" (uma fonte
    falhou). O caller usa isso pra responder com precisão em vez de um "nenhuma
    MP" ambíguo. Levanta DouError se o fetch falhar (credencial, rede)."""
    from aiogram.types import BufferedInputFile

    with _fase(f"fetch DOU {target_date.isoformat()}"):
        mps = await fetch_mps(target_date)
    # Flags do fetch ANTES do filtro por only_numeros (que troca o MPList por uma
    # list crua e perderia provisorio/sem_edicao/incompleto).
    _provisorio = bool(getattr(mps, "provisorio", False))
    _incompleto = bool(getattr(mps, "incompleto", False))
    _sem_edicao = bool(getattr(mps, "sem_edicao", False))
    if only_numeros:
        alvo = set(only_numeros)
        mps = [mp for mp in mps if mp["numero"] in alvo]
    if force:
        novas = mps
    else:
        novas = await filter_unseen(session, user.id, mps)
    if not novas:
        # 3-tupla como o caminho cheio (um `return 0` cru estourava TypeError no
        # unpack do caller). `motivo` distingue os desfeches vazios:
        #  - incompleto: uma fonte FALHOU → não dá pra afirmar nada;
        #  - houve edição (sem_edicao=False), 0 MP:
        #       . dia aberto  → 'sem_mp_extra' (o DOU JÁ saiu sem MP; a EXTRA
        #                        ainda pode trazer MP — NÃO dizer "ainda pode sair");
        #       . dia fechado → 'sem_mp' (saiu sem MP, definitivo);
        #  - sem edição (sem_edicao=True):
        #       . dia aberto  → 'provisorio' (nada saiu ainda, pode sair);
        #       . dia fechado → 'sem_edicao' (domingo/feriado, não houve Diário).
        # A confusão do bug (03/08): edição normal saiu sem MP e o DO1E ainda não
        # tinha saído → provisorio dizia "o DOU ainda pode sair", com o DOU já no ar.
        if _incompleto:
            motivo = "incompleto"
        elif not _sem_edicao:
            motivo = "sem_mp_extra" if _provisorio else "sem_mp"
        elif _provisorio:
            motivo = "provisorio"
        else:
            motivo = "sem_edicao"
        return 0, [], motivo

    # set de já-vistas pra não duplicar linhas no banco quando force=True.
    rows = await session.scalars(select(DouSeenMP).where(DouSeenMP.user_id == user.id))
    ja_vistas = {(r.numero, r.ano) for r in rows}

    # 1) avisos imediatos de todas as MPs (não dependem da nota, que é lenta).
    avisadas = []
    for mp in novas:
        try:
            await bot.send_message(
                user.id, format_telegram_message(mp, None),
                parse_mode="HTML", disable_web_page_preview=True,
            )
        except Exception:
            logger.exception("dou: falha ao avisar MP %s/%s ao user %s",
                             mp["numero"], mp["ano"], user.id)
            continue
        if (mp["numero"], mp["ano"]) not in ja_vistas:
            await mark_seen(session, user.id, mp)
        avisadas.append(mp)

    # 2) nota técnica + DOCX de cada MP (best-effort, uma por vez).
    async def _nota_e_docx(mp: dict) -> bool:
        try:
            if _SEM_NOTA.locked():
                logger.info("dou: MP %s/%s aguardando outra nota terminar",
                            mp["numero"], mp["ano"])
            async with _SEM_NOTA:
                await _gerar_e_enviar(mp)
            return True
        except Exception:
            logger.exception("dou: falha ao gerar/enviar nota da MP %s/%s",
                             mp["numero"], mp["ano"])
            try:
                await bot.send_message(
                    user.id,
                    f"⚠️ Não consegui gerar a nota técnica da MP {mp['numero']}/{mp['ano']} "
                    "agora. Tente /mp_dou_agora mais tarde.",
                    parse_mode=None,
                )
            except Exception:
                pass
            return False

    async def _gerar_e_enviar(mp: dict) -> None:
        """Pipeline de UMA nota. Sempre chamada sob o _SEM_NOTA."""
        await gerar_e_enviar_nota(bot, user, mp)

    # Serial (não paralelo) DENTRO da chamada. Entre chamadas quem serializa é
    # o _SEM_NOTA — este laço sozinho nunca protegeu de duas gerações
    # simultâneas vindas de caminhos diferentes.
    #
    # ALERTA pra quem mexer aqui: a tentação é paralelizar (`asyncio.gather`)
    # pra entregar as notas do dia mais rápido. NÃO faça sem manter o
    # semáforo. A chave e o plano do LLM são de QUEM USA, não do projeto:
    # basta um usuário da casa num free tier pra duas gerações concorrentes
    # virarem 429 — e aí o DOCX sai só com o texto base ("sem análise da IA").
    # A nota chega, mas sem o produto. Se precisar de mais vazão, suba
    # _NOTA_MAX_POR_JANELA (as notas enfileiram no semáforo) em vez de remover
    # a serialização.
    #
    # Histórico: em 01/08/2026 eu defendi o teto de 1 alegando risco de quota
    # com "o provedor é pago". Premissa errada — o plano não é do projeto. O
    # semáforo torna a garantia independente de plano, provedor e de todo
    # chamador se comportar.
    # OUTBOX: a pendência é registrada ANTES de gerar e só recebe baixa no
    # fim. Sem isso, a MP já estava marcada como VISTA (mark_seen, acima) e um
    # restart do container no meio dos ~68s da nota matava a task junto com o
    # processo: nota nunca gerada, dedup impedindo nova tentativa, e NENHUM
    # aviso. Perda silenciosa — o modo de falha que este monitor existe pra
    # não ter. Na dúvida, prefere-se DOCX repetido a nota que nunca chega.
    chave_outbox = await _abrir_outbox(session, user.id, target_date, avisadas)

    falhas: list[str] = []
    for mp in avisadas:
        if not await _nota_e_docx(mp):
            falhas.append(mp["numero"])

    await _fechar_outbox(session, user.id, target_date, chave_outbox, falhas)
    # Devolve (entregues, falhas, motivo). O caller da FILA
    # (_entregar_nota_pendente) precisa do `falhas` pra NÃO dar baixa na
    # pendência quando a nota falhou — sem isso a entrada some da fila e a nota
    # nunca mais é re-tentada, sem nem o aviso de desistência (o pior modo de
    # falha do projeto). motivo=None aqui: houve entrega.
    return len(avisadas), falhas, None
