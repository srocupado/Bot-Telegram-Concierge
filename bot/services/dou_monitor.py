"""Monitor de Medidas Provisórias no Diário Oficial da União (Inlabs/DOU).

Autônomo — não compartilha código nem estado com o Monitor-de-MP externo.
Fluxo: autentica no Inlabs → baixa ZIPs do DOU (DO1E + DO1) → extrai MPs
publicadas → gera nota técnica via Claude → entrega no Telegram (mensagem
+ DOCX). Dedup por (usuário, número, ano) na tabela dou_seen_mps.

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
import time
import zipfile
from datetime import date, datetime, timedelta
from xml.etree import ElementTree as ET

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import DouSeenMP

logger = logging.getLogger(__name__)

PLANALTO_BASE = "https://www.planalto.gov.br"
INLABS_BASE = "https://inlabs.in.gov.br"
DOU_SECTIONS = ["DO1E", "DO1"]

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
        stripped = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text_excerpt)).strip()
        m = re.search(
            r"DE\s+\d{4}\s+(.+?)(?=\s+O\s+PRESIDENTE|\s+A\s+PRESIDENTA|\s+O\s+VICE)",
            stripped, re.IGNORECASE | re.DOTALL,
        )
        ementa = (re.sub(r"\s+", " ", m.group(1)).strip()[:300] if m else stripped[:300])
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
        logger.info(
            "dou: MP %s — pubDate do XML=%s | target=%s | base usada=%s",
            numero, pub.isoformat() if pub else "NÃO ENCONTRADO",
            target_date.isoformat(), (pub or target_date).isoformat(),
        )
        results.append(
            _build_mp_dict(client, numero, year, body_text or title_text, target_date, pub_date=pub)
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


# O Inlabs (login + download dos ZIPs) solta 502/503/504 e timeouts com
# frequência — transitórios. Retry com backoff evita perder a busca inteira por
# um Bad Gateway passageiro. Roda em thread (fetch_mps), então time.sleep é ok.
_INLABS_RETRY_STATUS = frozenset({500, 502, 503, 504})
# O Inlabs serve uma página "Sistema em Manutenção" (com status 502) quando o
# sistema está fora pra manutenção — não adianta retentar, e o usuário merece
# uma mensagem clara em vez de "HTTP 502".
_MAINT_RE = re.compile(r"manuten[çc][ãa]o", re.IGNORECASE)


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
        # login (com retry em 502/503/504/timeout transitórios do Inlabs)
        try:
            resp = _inlabs_call(lambda: client.post(
                f"{INLABS_BASE}/logar.php",
                data={"email": email, "password": password},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=20.0,
            ))
            resp.raise_for_status()
        except DouError:
            raise  # InlabsMaintenanceError já traz mensagem clara
        except Exception as exc:
            raise DouError(f"falha ao autenticar no Inlabs: {exc}") from exc
        cookie = client.cookies.get("inlabs_session_cookie")
        if not cookie:
            raise DouError("autenticação Inlabs falhou (cookie ausente) — verifique e-mail/senha.")

        date_str = target_date.strftime("%Y-%m-%d")
        failed_sections: list[str] = []
        for section in DOU_SECTIONS:
            url = f"{INLABS_BASE}/index.php?p={date_str}&dl={date_str}-{section}.zip"
            try:
                r = _inlabs_call(lambda: client.get(
                    url, headers={"Cookie": f"inlabs_session_cookie={cookie}"}, timeout=60.0,
                ))
                if r.status_code == 404:
                    continue  # seção não publicada nessa data (legítimo)
                r.raise_for_status()
                content = r.content
                if len(content) < 100 or content[:2] != b"PK":
                    continue  # não publicada / resposta HTML (legítimo)
                with zipfile.ZipFile(io.BytesIO(content)) as zf:
                    for name in (n for n in zf.namelist() if n.lower().endswith(".xml")):
                        xml_data = zf.read(name).decode("utf-8", errors="replace")
                        if "MEDIDA PROVIS" not in xml_data.upper():
                            continue
                        for mp in _parse_dou_xml(client, xml_data, target_date):
                            if mp["numero"] not in seen_numeros:
                                seen_numeros.add(mp["numero"])
                                mp["edicao"] = "Extra" if section == "DO1E" else "Normal"
                                results.append(mp)
            except InlabsMaintenanceError:
                raise  # manutenção → mensagem clara, não é "seção incompleta"
            except zipfile.BadZipFile:
                logger.warning("dou: ZIP inválido em %s", section)
                failed_sections.append(section)
            except Exception as exc:  # 502/timeout após retries, conexão…
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
    if failed_sections:
        logger.warning("dou: %s — seção(ões) %s falharam; lista pode estar "
                       "incompleta", target_date, failed_sections)
    return results


async def fetch_mps(target_date: date) -> list[dict]:
    """Busca MPs publicadas na data (Inlabs DO1E + DO1). Roda o I/O
    bloqueante numa thread pra não travar o event loop."""
    return await asyncio.to_thread(_fetch_mps_sync, target_date)


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
        f"MP nº {mp['numero']}/{mp['ano']} — Edição {mp.get('edicao', 'Normal')} "
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


def _gemini_no_thinking_config():
    """ThinkingConfig com budget=0 — STT/nota técnica não precisam de
    raciocínio interno, e o thinking come max_output_tokens, causando
    JSON truncado. (Igual ao tratamento em voice.py.)"""
    try:
        from google.genai import types
        return types.ThinkingConfig(thinking_budget=0)
    except Exception:
        return None


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
        config = types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            max_output_tokens=1500,
        )
        resp = client.models.generate_content(model=model, contents=prompt, config=config)
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
        config = types.GenerateContentConfig(
            system_instruction=_NOTA_SYSTEM,
            response_mime_type="application/json",
            response_schema=schema,
            max_output_tokens=16384,
            thinking_config=_gemini_no_thinking_config(),
        )
        resp = client.models.generate_content(model=model, contents=user_content, config=config)
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
        f"<i>Edição {mp.get('edicao', 'Normal')} do DOU de {_br(pub)}</i>",
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


async def mark_seen(session: AsyncSession, user_id: int, mp: dict) -> None:
    session.add(DouSeenMP(user_id=user_id, numero=mp["numero"], ano=mp["ano"]))
    await session.commit()


# ──────────────────────── entrega (mensagem + DOCX) ────────────────────────

async def deliver_to_user(
    bot, session: AsyncSession, user, target_date: date, *, force: bool = False,
    only_numeros: list[str] | None = None,
) -> int:
    """Busca MPs da data, gera nota + DOCX e entrega no Telegram. Por padrão
    pula as já notificadas (dedup); com force=True entrega tudo que achar
    (usado pelo comando manual). only_numeros restringe a entrega a essas MPs
    (números) — usado pelo botão do proativo, que avisa de um subconjunto do dia
    e não deve regerar todas. Retorna quantas MPs foram entregues.
    Levanta DouError se o fetch falhar (credencial, rede)."""
    from aiogram.types import BufferedInputFile

    mps = await fetch_mps(target_date)
    if only_numeros:
        alvo = set(only_numeros)
        mps = [mp for mp in mps if mp["numero"] in alvo]
    if force:
        novas = mps
    else:
        novas = await filter_unseen(session, user.id, mps)
    if not novas:
        return 0

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

    # 2) nota técnica + DOCX de cada MP, EM PARALELO (best-effort).
    async def _nota_e_docx(mp: dict) -> None:
        try:
            logger.info("dou: gerando nota técnica MP %s/%s…", mp["numero"], mp["ano"])
            nota = await generate_nota_tecnica(
                mp,
                provider=getattr(user, "dou_mp_provider", None),
                model=getattr(user, "dou_mp_model", None),
            )
            docx_bytes = await asyncio.to_thread(build_docx, mp, nota)
            await bot.send_document(
                user.id,
                BufferedInputFile(docx_bytes, filename=docx_filename(mp)),
                caption=None if nota else "⚠️ Nota gerada sem análise da IA (texto base).",
            )
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

    # Serial (não paralelo) pra não estourar o RPM do free tier do Gemini.
    for mp in avisadas:
        await _nota_e_docx(mp)
    return len(avisadas)
