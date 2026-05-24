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
import logging
import os
import re
import zipfile
from datetime import date, timedelta
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
    """Best-effort: busca ementa + texto limpo na página do Planalto."""
    try:
        resp = client.get(url, timeout=20.0)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        lines = [ln.strip() for ln in soup.get_text("\n", strip=True).splitlines() if ln.strip()]
        ementa = ""
        found_title = False
        for ln in lines[:40]:
            if re.match(r"MEDIDA PROVIS[ÓO]RIA\s+N", ln, re.I):
                found_title = True
                continue
            if found_title and len(ln) > 20:
                if not re.match(r"(A PRESIDENTA|O PRESIDENTE|O VICE)", ln, re.I):
                    ementa = ln
                    break
        if not ementa:
            ementa = lines[0] if lines else ""
        return ementa, "\n".join(lines[:500])
    except Exception as exc:
        logger.warning("dou: erro ao buscar página planalto %s: %s", url, exc)
        return "", ""


def _build_mp_dict(client: httpx.Client, numero: str, year: int, text_excerpt: str, target_date: date) -> dict:
    period = _planalto_period(year)
    ano2d = str(year)[-2:]
    planalto_url = f"{PLANALTO_BASE}/ccivil_03/_Ato{period}/{year}/Mpv/mpv{numero}-{ano2d}.htm"
    ementa_page, texto_planalto = _fetch_mp_page(client, planalto_url)
    if ementa_page:
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
        "data_publicacao": target_date.isoformat(),
        "url_planalto": planalto_url,
        "texto_integral": texto_planalto or text_excerpt[:6000],
    }


def _parse_dou_xml(client: httpx.Client, xml_content: str, target_date: date) -> list[dict]:
    year = target_date.year
    results: list[dict] = []
    seen: set[str] = set()

    def _try(title_text: str, body_text: str) -> None:
        m = _TITLE_RE.match(title_text.strip().upper())
        if not m:
            return
        numero = m.group(1).replace(".", "")
        if numero in seen:
            return
        seen.add(numero)
        results.append(_build_mp_dict(client, numero, year, body_text or title_text, target_date))

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
        _try(text, body_text)
    for elem in root.iter():
        attr_title = elem.get("title", "").strip()
        if attr_title:
            _try(attr_title, ET.tostring(elem, encoding="unicode", method="text"))

    if not results:
        for raw_line in xml_content.splitlines():
            line = re.sub(r"<[^>]+>", " ", raw_line).strip()
            if 20 <= len(line) <= 400:
                _try(line, xml_content)
    return results


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
        # login
        try:
            resp = client.post(
                f"{INLABS_BASE}/logar.php",
                data={"email": email, "password": password},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=20.0,
            )
            resp.raise_for_status()
        except Exception as exc:
            raise DouError(f"falha ao autenticar no Inlabs: {exc}")
        cookie = client.cookies.get("inlabs_session_cookie")
        if not cookie:
            raise DouError("autenticação Inlabs falhou (cookie ausente) — verifique e-mail/senha.")

        date_str = target_date.strftime("%Y-%m-%d")
        for section in DOU_SECTIONS:
            url = f"{INLABS_BASE}/index.php?p={date_str}&dl={date_str}-{section}.zip"
            try:
                r = client.get(
                    url, headers={"Cookie": f"inlabs_session_cookie={cookie}"}, timeout=60.0,
                )
                if r.status_code == 404:
                    continue
                r.raise_for_status()
                content = r.content
                if len(content) < 100 or content[:2] != b"PK":
                    continue  # seção não publicada / resposta HTML
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
            except zipfile.BadZipFile:
                logger.warning("dou: ZIP inválido em %s", section)
            except Exception as exc:
                logger.warning("dou: erro processando %s: %s", section, exc)
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

# Tools de web search/fetch nativas da Anthropic (Passo 2 das diretrizes).
_WEB_TOOLS = [
    {"type": "web_search_20260209", "name": "web_search"},
    {"type": "web_fetch_20260209", "name": "web_fetch"},
]


async def _pesquisar_contexto(client, mp: dict) -> str:
    """Fase 1: pesquisa contexto político/noticioso da MP via web search.
    Retorna um dossiê textual (ou string vazia se desligado/indisponível).
    Limitado no tempo pra não pendurar a entrega."""
    if not settings.dou_mp_web_research:
        return ""
    prompt = (
        f"Pesquise o contexto da Medida Provisória nº {mp['numero']}, de "
        f"{mp['ano']} (publicada no DOU em {mp['data_publicacao']}). "
        f"Ementa: {mp['ementa']}\n\n"
        "Busque: evento motivador (calamidade, crise setorial, pacote do "
        "governo), atores políticos (ministros, lideranças), valores e dados "
        "quantitativos, MPs correlatas anteriores e reações de setores "
        "afetados. Fontes: Planalto, Agência Gov, Câmara, Senado, imprensa. "
        "Responda com um dossiê objetivo em tópicos — só fatos com fonte, sem "
        "redigir a nota ainda."
    )
    messages = [{"role": "user", "content": prompt}]
    bounded = client.with_options(timeout=60.0, max_retries=1)
    try:
        async def _run() -> str:
            msgs = messages
            for _ in range(3):
                resp = await bounded.messages.create(
                    model=settings.anthropic_model,
                    max_tokens=2048,
                    tools=_WEB_TOOLS,
                    messages=msgs,
                )
                if resp.stop_reason == "pause_turn":
                    msgs = [
                        {"role": "user", "content": prompt},
                        {"role": "assistant", "content": resp.content},
                    ]
                    continue
                return "\n".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
            return ""
        return await asyncio.wait_for(_run(), timeout=150.0)
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
        f"{(mp.get('texto_integral') or 'Não disponível')[:8000]}\n\n"
        "Emita a nota técnica seguindo a estrutura de 5 parágrafos."
    )


async def generate_nota_tecnica(mp: dict) -> dict | None:
    """Gera o conteúdo da nota técnica (pesquisa + redação estruturada),
    roteando pelo provider de DOU_MP_PROVIDER. Retorna {ementa, p1_contexto,
    ...} ou None se indisponível/falhar."""
    if settings.dou_mp_provider == "gemini":
        return await _gen_nota_gemini(mp)
    return await _gen_nota_anthropic(mp)


# ── Anthropic (Claude + web_search) ──

async def _gen_nota_anthropic(mp: dict) -> dict | None:
    if not settings.anthropic_api_key:
        logger.warning("dou: ANTHROPIC_API_KEY ausente; nota pulada")
        return None
    try:
        import anthropic
    except ImportError:
        return None
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    dossie = await _pesquisar_contexto(client, mp)
    user_content = _nota_user_content(mp, dossie)
    try:
        resp = await client.with_options(timeout=120.0, max_retries=1).messages.create(
            model=settings.anthropic_model,
            max_tokens=4096,
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


def _gemini_models() -> list[str]:
    """Modelo principal + fallback (sem repetir)."""
    models = [settings.dou_mp_gemini_model]
    fb = settings.dou_mp_gemini_model_fallback
    if fb and fb != settings.dou_mp_gemini_model:
        models.append(fb)
    return models


def _is_quota_error(exc: Exception) -> bool:
    s = str(exc).lower()
    return any(t in s for t in ("429", "resource_exhausted", "quota", "rate limit"))


async def _pesquisar_contexto_gemini(client, mp: dict) -> str:
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

    for model in _gemini_models():
        try:
            return await asyncio.wait_for(asyncio.to_thread(_call, model), timeout=120.0)
        except Exception as exc:
            if _is_quota_error(exc) and model != _gemini_models()[-1]:
                logger.warning("dou: pesquisa gemini %s estourou cota; tentando fallback", model)
                continue
            logger.warning("dou: pesquisa web (gemini) indisponível p/ MP %s (%s)", mp["numero"], exc)
            return ""
    return ""


async def _gen_nota_gemini(mp: dict) -> dict | None:
    if not settings.gemini_api_key:
        logger.warning("dou: GEMINI_API_KEY ausente; nota pulada")
        return None
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return None
    import json
    client = genai.Client(api_key=settings.gemini_api_key)
    dossie = await _pesquisar_contexto_gemini(client, mp)
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
        config = types.GenerateContentConfig(
            system_instruction=_NOTA_SYSTEM,
            response_mime_type="application/json",
            response_schema=schema,
            max_output_tokens=6144,
        )
        resp = client.models.generate_content(model=model, contents=user_content, config=config)
        return (resp.text or "").strip()

    models = _gemini_models()
    for model in models:
        try:
            raw = await asyncio.wait_for(asyncio.to_thread(_call, model), timeout=120.0)
            data = json.loads(raw)
            if not data.get("p1_contexto") or not data.get("p2_dispositivos"):
                logger.warning("dou: nota gemini incompleta MP %s/%s (%s)", mp["numero"], mp["ano"], model)
            logger.info("dou: nota gerada MP %s/%s via %s", mp["numero"], mp["ano"], model)
            return data
        except Exception as exc:
            if _is_quota_error(exc) and model != models[-1]:
                logger.warning("dou: nota gemini %s estourou cota MP %s; tentando fallback",
                               model, mp["numero"])
                continue
            logger.exception("dou: falha (gemini/%s) na nota MP %s/%s", model, mp["numero"], mp["ano"])
            return None
    return None


# ──────────────────────── prazos + formatação ────────────────────────

def compute_prazos(pub: date) -> dict:
    """Prazos regimentais a partir da publicação (dia 1 = publicação)."""
    return {
        "eficacia_fim": pub + timedelta(days=59),       # 60 dias
        "sobrestamento": pub + timedelta(days=45),
        "emendas_fim": pub + timedelta(days=7),
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
) -> int:
    """Busca MPs da data, gera nota + DOCX e entrega no Telegram. Por padrão
    pula as já notificadas (dedup); com force=True entrega tudo que achar
    (usado pelo comando manual). Retorna quantas MPs foram entregues.
    Levanta DouError se o fetch falhar (credencial, rede)."""
    from aiogram.types import BufferedInputFile

    mps = await fetch_mps(target_date)
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
            nota = await generate_nota_tecnica(mp)
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
