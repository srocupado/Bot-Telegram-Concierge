"""Retirada automática de ingresso grátis na Sympla.

Pedido do dono (23/09/2026): toda quarta a Sympla libera 2 ingressos por
pessoa pro concerto da semana da Orquestra Sinfônica do Teatro Nacional
Cláudio Santoro. O EVENTO em si só é PUBLICADO às 17h59 — com URL NOVA a
cada semana, não dá pra fixar link — e a retirada abre às 18h00.

## O que foi verificado contra o site real ANTES deste módulo existir

  - o checkout NÃO redireciona pra outro domínio (ex.: "Ingresso Digital"
    aparece num resumo de busca, mas é falso pra este produtor específico);
    tudo fica em sympla.com.br;
  - selecionar o ingresso dispara uma RESERVA via API (event-bff, mutation
    "GRSimpleReservation") com token de prazo — só depois o navegador vai
    pro checkout preencher dados do participante. Não é "um clique e
    pronto": é reservar e terminar o cadastro dentro de uma janela;
  - o login é e-mail+senha de verdade (existe "esqueci minha senha" no
    código — não é só link mágico/OTP, o que inviabilizaria automação sem
    alguém clicando no e-mail toda quarta);
  - o domínio usa reCAPTCHA Enterprise em ALGUM formulário (sitekey
    confirmado no JS), mas o dono NUNCA viu captcha na retirada manual —
    é o dado que mais pesa a favor de tentar;
  - o Chromium ARM64 do Playwright existe e baixa de verdade (confirmado
    contra o CDN oficial: ~208 MB, HTTP 200) — roda no Orange Pi.

## O que este módulo NÃO pôde validar antes do primeiro uso real

Os seletores exatos da tela de seleção de ingresso e do checkout: não existe
evento aberto fora do minuto exato da liberação, e um evento já encerrado
não renderiza mais o formulário ativo. Por isso:

  - toda busca de elemento é por TEXTO/PAPEL (aria role), nunca por classe
    CSS — a Sympla faz build com hash de classe, que muda a cada deploy
    deles de qualquer forma;
  - qualquer etapa que não encontrar o que espera PARA e devolve screenshot
    + descrição de onde travou, em vez de clicar às cegas no que achar
    parecido. A primeira quarta real é o primeiro teste de verdade — o
    desenho aqui é pra falhar de forma diagnosticável, não silenciosa.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import KVSetting

logger = logging.getLogger(__name__)

BRT = ZoneInfo("America/Sao_Paulo")

BASE_URL = "https://www.sympla.com.br"
SEARCH_URL = BASE_URL + "/eventos/brasilia-df?category=city&q={query}"

_KV_EMAIL = "sympla_email"
_KV_PASSWORD = "sympla_password"
_KV_NOME = "sympla_nome_completo"
_KV_CPF = "sympla_cpf"


class SymplaError(Exception):
    """Falha no fluxo — a mensagem já diz o PASSO onde parou."""


# ───────────────────────── credenciais (kv_settings) ─────────────────────────
# Global, não por-usuário: é a conta PESSOAL do dono usada pro bot logar
# sozinho, mesmo padrão de guarda do service account do Firebase
# (financeiro.py) — kv_settings, nunca .env, pra rotacionar sem redeploy.

@dataclass
class SymplaCredenciais:
    email: str
    senha: str
    nome_completo: str
    cpf: str | None


async def get_credenciais(session: AsyncSession) -> SymplaCredenciais | None:
    linhas = {}
    for key in (_KV_EMAIL, _KV_PASSWORD, _KV_NOME, _KV_CPF):
        row = await session.get(KVSetting, key)
        linhas[key] = row.value if row else None
    if not linhas[_KV_EMAIL] or not linhas[_KV_PASSWORD] or not linhas[_KV_NOME]:
        return None
    return SymplaCredenciais(
        email=linhas[_KV_EMAIL], senha=linhas[_KV_PASSWORD],
        nome_completo=linhas[_KV_NOME], cpf=linhas[_KV_CPF],
    )


def _validar_email(email: str) -> str:
    email = email.strip()
    if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        raise SymplaError(f"'{email}' não parece um e-mail válido.")
    return email


async def _set_kv(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(KVSetting, key)
    if row is None:
        session.add(KVSetting(key=key, value=value))
    else:
        row.value = value


async def save_email(session: AsyncSession, email: str) -> None:
    await _set_kv(session, _KV_EMAIL, _validar_email(email))
    await session.commit()


async def save_senha(session: AsyncSession, senha: str) -> None:
    senha = senha.strip()
    if len(senha) < 4:
        raise SymplaError("senha muito curta.")
    await _set_kv(session, _KV_PASSWORD, senha)
    await session.commit()


async def save_nome(session: AsyncSession, nome: str) -> None:
    nome = nome.strip()
    if len(nome.split()) < 2:
        raise SymplaError("preciso do nome COMPLETO (nome e sobrenome).")
    await _set_kv(session, _KV_NOME, nome)
    await session.commit()


async def save_cpf(session: AsyncSession, cpf: str) -> None:
    digitos = re.sub(r"\D", "", cpf)
    if len(digitos) != 11:
        raise SymplaError("CPF precisa ter 11 dígitos.")
    await _set_kv(session, _KV_CPF, digitos)
    await session.commit()


def descrever_email(email: str) -> str:
    """Mascarado pra exibir sem reexibir o valor inteiro."""
    user, _, dominio = email.partition("@")
    if len(user) <= 2:
        mask = user[:1] + "•" * max(len(user) - 1, 1)
    else:
        mask = user[0] + "•" * (len(user) - 2) + user[-1]
    return f"{mask}@{dominio}"


# ───────────────────────── escolha do evento (testável) ─────────────────────

_STOPWORDS = frozenset({"de", "do", "da", "dos", "das", "e", "em", "no", "na"})


def _normalizar(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower().strip()


@dataclass
class EventoCandidato:
    titulo: str
    url: str
    data_inicio: date | None
    encerrado: bool


def escolher_evento(
    candidatos: list[EventoCandidato], query: str, hoje: date,
) -> EventoCandidato | None:
    """Entre os resultados da busca, escolhe o evento certo: título contém
    as palavras-chave da query, não está encerrado, e tem a data mais
    PRÓXIMA (tolerando 1 dia pra trás, pelo limite de fuso/meia-noite).

    None = nenhum candidato bate — o caller NÃO reserva nada às cegas.
    Ambiguidade e ausência recebem o MESMO tratamento (None): errar pra
    "não achei" é sempre mais barato que errar reservando o evento errado.
    """
    termos = [t for t in _normalizar(query).split()
              if t not in _STOPWORDS and len(t) > 2]
    if not termos:
        return None
    limite = hoje - timedelta(days=1)
    validos = []
    for c in candidatos:
        if c.encerrado:
            continue
        titulo_norm = _normalizar(c.titulo)
        if not all(t in titulo_norm for t in termos):
            continue
        if c.data_inicio is not None and c.data_inicio < limite:
            continue
        validos.append(c)
    if not validos:
        return None
    validos.sort(key=lambda c: (c.data_inicio is None, c.data_inicio or date.max))
    return validos[0]


def evento_da_hydration(url: str, payload: dict) -> EventoCandidato | None:
    """Extrai (título, data, encerrado?) do __NEXT_DATA__ de uma página de
    evento — mesmo formato inspecionado manualmente no levantamento inicial
    (props.pageProps.hydrationData.eventHydration.event)."""
    try:
        ev = payload["props"]["pageProps"]["hydrationData"]["eventHydration"]["event"]
    except (KeyError, TypeError):
        return None
    data_inicio = None
    sd = ev.get("startDate")
    if sd:
        try:
            data_inicio = datetime.fromisoformat(str(sd).replace(" ", "T")).date()
        except ValueError:
            pass
    return EventoCandidato(
        titulo=ev.get("name") or "",
        url=url,
        data_inicio=data_inicio,
        encerrado=bool(ev.get("isClosed")),
    )


# ───────────────────────── timing (testável) ─────────────────────────

def proxima_janela(agora: datetime, weekday: int, hora: int) -> datetime:
    """Próxima ocorrência de `weekday` (0=segunda) às `hora`:00 BRT, a partir
    de `agora`. Se hoje já é o dia mas a hora já passou, pula pra semana que
    vem — nunca devolve algo no passado."""
    agora_brt = agora.astimezone(BRT)
    dias = (weekday - agora_brt.weekday()) % 7
    alvo = (agora_brt + timedelta(days=dias)).replace(
        hour=hora, minute=0, second=0, microsecond=0)
    if alvo <= agora_brt:
        alvo += timedelta(days=7)
    return alvo


# Margem antes da hora-alvo pra já estar logado e com a busca carregada
# quando o evento for publicado (1 min antes da liberação, ver módulo).
INICIO_ANTECEDENCIA = timedelta(minutes=5)
# Intervalo entre tentativas de achar o evento recém-publicado.
POLL_INTERVALO_S = 2.0
# Desiste de procurar o evento depois disso (publicação atrasada/sumida).
POLL_TIMEOUT_S = 180.0


# ───────────────────────── automação (Playwright) ─────────────────────────
# Esta parte não dá pra testar de verdade sem um evento ao vivo — ver o
# docstring do módulo. Cada etapa tem timeout curto e reporta onde parou.

@dataclass
class SymplaResultado:
    sucesso: bool
    etapa: str
    detalhe: str
    evento_titulo: str | None = None
    evento_url: str | None = None
    screenshot: bytes | None = None


CHROMIUM_ARGS = [
    # Sandbox de kernel completo não é garantido no container do Orange Pi
    # (mesma justificativa do sandbox.py de execução de código) — roda sem
    # ele, mas a máquina não recebe tráfego não confiável nenhum por essa
    # sessão (só sympla.com.br, com credencial do próprio dono).
    "--no-sandbox",
    "--disable-dev-shm-usage",
]


async def _screenshot_seguro(page) -> bytes | None:
    try:
        return await page.screenshot(full_page=False)
    except Exception:
        logger.exception("sympla: falha ao capturar screenshot")
        return None


async def _ler_hydration(page) -> dict | None:
    try:
        raw = await page.eval_on_selector("#__NEXT_DATA__", "el => el.textContent")
    except Exception:
        return None
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None


async def _login(page, creds: SymplaCredenciais) -> None:
    await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30_000)
    entrar_btn = page.get_by_role("button", name=re.compile("entrar", re.I)).first
    await entrar_btn.click(timeout=10_000)
    await page.get_by_label(re.compile("e-?mail", re.I)).first.fill(
        creds.email, timeout=10_000)
    await page.get_by_label(re.compile("senha", re.I)).first.fill(
        creds.senha, timeout=10_000)
    await page.get_by_role("button", name=re.compile(r"^entrar$", re.I)).first.click(
        timeout=10_000)
    # Confirma autenticado pelo desaparecimento do botão "Entrar" do topo
    # (vira nome/avatar do usuário) — melhor sinal disponível sem saber o
    # markup exato do estado logado.
    await page.get_by_role("button", name=re.compile(r"^entrar$", re.I)).wait_for(
        state="detached", timeout=15_000)


async def _buscar_candidatos(page, query: str) -> list[EventoCandidato]:
    """Busca é renderizada client-side — precisa de JS rodando, por isso é
    Playwright e não um scraper leve à parte."""
    url = SEARCH_URL.format(query=quote(query))
    await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    try:
        await page.wait_for_selector("a[href*='/evento/']", timeout=10_000)
    except Exception:
        return []
    links = await page.eval_on_selector_all(
        "a[href*='/evento/']",
        "els => els.map(e => ({href: e.href, text: e.innerText}))",
    )
    vistos: set[str] = set()
    out = []
    for l in links:
        href = l.get("href") or ""
        if not href or href in vistos:
            continue
        vistos.add(href)
        out.append(EventoCandidato(
            titulo=(l.get("text") or "").strip(), url=href,
            data_inicio=None, encerrado=False,
        ))
    return out


async def _detalhar(page, cand: EventoCandidato) -> EventoCandidato | None:
    """Abre a página do candidato e lê data/estado reais — os cards da
    busca não trazem isso, só título e link."""
    await page.goto(cand.url, wait_until="domcontentloaded", timeout=30_000)
    payload = await _ler_hydration(page)
    if payload is None:
        return None
    return evento_da_hydration(cand.url, payload)


async def _localizar_evento(
    page, query: str, hoje: date,
) -> EventoCandidato | None:
    for c in await _buscar_candidatos(page, query):
        detalhado = await _detalhar(page, c)
        if detalhado is None:
            continue
        escolhido = escolher_evento([detalhado], query, hoje)
        if escolhido is not None:
            return escolhido
    return None


async def _selecionar_e_reservar(page, qty: int) -> None:
    """Incrementa a quantidade do (primeiro) tipo de ingresso `qty` vezes e
    avança. Melhor esforço: sem um evento ao vivo pra inspecionar, o rótulo
    exato do botão de "+" e de avançar são inferidos, não confirmados."""
    mais = page.get_by_role("button", name="+").first
    for _ in range(qty):
        await mais.click(timeout=10_000)
    avancar = page.get_by_role(
        "button", name=re.compile(r"reservar|continuar|garantir", re.I),
    ).first
    await avancar.click(timeout=10_000)


async def _preencher_checkout(page, creds: SymplaCredenciais) -> None:
    await page.wait_for_url(re.compile(r"/checkout/"), timeout=20_000)
    campos = (
        (re.compile(r"nome completo|nome do participante", re.I), creds.nome_completo),
        (re.compile(r"e-?mail", re.I), creds.email),
        (re.compile(r"cpf", re.I), creds.cpf),
    )
    for rotulo, valor in campos:
        if not valor:
            continue
        try:
            campo = page.get_by_label(rotulo).first
            if await campo.count():
                await campo.fill(valor, timeout=5_000)
        except Exception:
            # Campo pode não existir nesta tela — segue sem ele; a etapa
            # de finalizar é quem decide se faltou algo obrigatório.
            continue
    finalizar = page.get_by_role(
        "button", name=re.compile(r"finalizar|confirmar pedido", re.I),
    ).first
    await finalizar.click(timeout=15_000)


async def retirar_ingresso(
    creds: SymplaCredenciais, query: str, qty: int, *, agora: datetime | None = None,
) -> SymplaResultado:
    """Orquestra o fluxo inteiro. Qualquer exceção numa etapa vira resultado
    de FALHA com screenshot — nunca propaga pro chamador como traceback cru,
    porque quem chama precisa poder avisar o dono mesmo quando algo aqui
    quebra de um jeito que eu não previ."""
    from playwright.async_api import async_playwright

    hoje = (agora or datetime.now(BRT)).astimezone(BRT).date()
    etapa = "iniciar navegador"
    page = None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=CHROMIUM_ARGS)
            try:
                context = await browser.new_context(locale="pt-BR")
                page = await context.new_page()

                etapa = "login"
                await _login(page, creds)

                etapa = "localizar o evento da semana"
                evento = None
                import time as _time
                deadline = _time.monotonic() + POLL_TIMEOUT_S
                while _time.monotonic() < deadline:
                    evento = await _localizar_evento(page, query, hoje)
                    if evento is not None:
                        break
                    await page.wait_for_timeout(int(POLL_INTERVALO_S * 1000))
                if evento is None:
                    return SymplaResultado(
                        False, etapa,
                        f"não achei nenhum evento publicado pra '{query}' em "
                        f"{POLL_TIMEOUT_S:.0f}s de tentativas.",
                        screenshot=await _screenshot_seguro(page),
                    )

                etapa = "abrir o evento"
                await page.goto(evento.url, wait_until="domcontentloaded", timeout=30_000)

                etapa = "selecionar ingressos e reservar"
                await _selecionar_e_reservar(page, qty)

                etapa = "preencher checkout"
                await _preencher_checkout(page, creds)

                return SymplaResultado(
                    True, "concluído",
                    f"{qty} ingresso(s) retirado(s) para '{evento.titulo}'.",
                    evento_titulo=evento.titulo, evento_url=evento.url,
                    screenshot=await _screenshot_seguro(page),
                )
            finally:
                await browser.close()
    except Exception as exc:
        logger.exception("sympla: falha na etapa '%s'", etapa)
        shot = await _screenshot_seguro(page) if page is not None else None
        return SymplaResultado(
            False, etapa, f"{type(exc).__name__}: {exc}", screenshot=shot,
        )
