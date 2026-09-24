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
# "s" é o parâmetro real da busca (visto ao vivo, 24/09/2026, digitando na
# caixa do site). O "q" usado antes era IGNORADO: a página devolvia a lista
# genérica de Brasília (Rock Night, Bailão...) e o concerto nunca seria achado.
SEARCH_URL = BASE_URL + "/eventos?s={query}"

_KV_EMAIL = "sympla_email"
_KV_PASSWORD = "sympla_password"
_KV_NOME = "sympla_nome_completo"
_KV_CPF = "sympla_cpf"


class SymplaError(Exception):
    """Falha no fluxo — a mensagem já diz o PASSO onde parou. `print_meio`
    guarda uma tela capturada NO MEIO da etapa (o print do fim nem sempre
    mostra o que deu errado)."""

    def __init__(self, msg: str, print_meio: bytes | None = None):
        super().__init__(msg)
        self.print_meio = print_meio


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
# Prazo pra abrir o formulário de login. Medido com a CPU limitada: 6x
# levou ~19s, 12x ~40s (do goto até o formulário). O Pi real não foi medido.
LOGIN_ABRIR_TIMEOUT_S = 120.0
BUSCA_TIMEOUT_MS = 60_000
CLOUDFLARE_ESPERA_S = 30.0


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
    print_meio: bytes | None = None


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


async def _dump_clicaveis(page, limite: int = 40) -> str:
    """Lista os botões/links VISÍVEIS da tela — usado só em falha, pra virar
    diagnóstico real em vez de eu ter que adivinhar de novo às cegas.

    Nasceu do primeiro uso real (23/09/2026): a etapa "login" estourou
    porque "Entrar" não existe como TEXTO em lugar nenhum do JS da Sympla —
    o gatilho do header é provavelmente ícone sem rótulo visível, com texto
    vindo de um arquivo de tradução que não dá pra enumerar por fora. Sem
    isto, cada falha exigiria outra rodada de tentativa-e-erro cega."""
    try:
        itens = await page.eval_on_selector_all(
            "button, a, [role=button]",
            "els => els.filter(e => e.offsetParent !== null).slice(0, %d)"
            ".map(e => (e.innerText || e.getAttribute('aria-label') || "
            "e.getAttribute('title') || '').trim()).filter(Boolean)" % limite,
        )
        # Filtra/apara de novo em Python: não confia só no .trim()/Boolean do
        # lado do JS (achado pelo próprio teste deste módulo — um item só de
        # espaço passava pelo filtro do JS e sobrevivia até aqui).
        limpos = [t.strip() for t in itens if isinstance(t, str) and t.strip()]
        texto = " | ".join(dict.fromkeys(limpos)) or "(nenhum elemento com texto visível)"
        # Teto de tamanho: o dump entra dentro da mensagem de erro que vai
        # pro Telegram, e uma tela com muitos elementos NÃO pode estourar o
        # limite da mensagem e mascarar o próprio diagnóstico.
        return texto if len(texto) <= 800 else texto[:800] + "…"
    except Exception:
        return "(não consegui listar os elementos da tela)"


_CLOUDFLARE = re.compile(
    r"verifica[çc][aã]o de seguran[çc]a|prote[çc][aã]o contra bots|"
    r"verifying you are human|checking your browser|just a moment",
    re.I)


async def _cloudflare_na_tela(page) -> bool:
    try:
        return bool(await page.get_by_text(_CLOUDFLARE).count())
    except Exception:
        return False


async def _passar_cloudflare(page) -> None:
    """Print real do Pi (23/09/2026): a tela no fim do teste era a
    verificação anti-bot do Cloudflare ("Executando verificação de
    segurança"), não a Sympla. É o desafio que qualquer navegador recebe e
    que costuma liberar sozinho em segundos — aqui só se ESPERA, como um
    navegador comum. Não há tentativa de burlar: se não liberar, a etapa
    falha dizendo isso, em vez de ler a página do desafio como se fosse a
    Sympla (foi assim que um evento podia sumir como "não achei")."""
    if not await _cloudflare_na_tela(page):
        return
    import time as _time
    prazo = _time.monotonic() + CLOUDFLARE_ESPERA_S
    while _time.monotonic() < prazo:
        await page.wait_for_timeout(1_000)
        if not await _cloudflare_na_tela(page):
            await page.wait_for_load_state("domcontentloaded")
            return
    raise SymplaError(
        f"a Sympla mostrou a verificação anti-bot do Cloudflare e ela não "
        f"liberou em {CLOUDFLARE_ESPERA_S:.0f}s — não consegui continuar.")


async def _ir(page, url: str, timeout_ms: int = 60_000) -> None:
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    await _passar_cloudflare(page)


async def _clicar(page, loc, timeout_ms: int = 10_000) -> None:
    """Clique de mouse de verdade no centro do elemento, SEM o teste de
    "estável" do locator.click().

    Log real do Orange Pi (24/09/2026): o botão foi achado, mas o click()
    ficou 10s em "waiting for element to be visible, enabled and stable" e
    desistiu. Reproduzido aqui com a CPU do Chromium limitada 6x e 12x: o
    botão estava visível, habilitado e PARADO (mesma posição medida várias
    vezes), mas o teste de estabilidade depende de quadros de animação que
    não chegam numa CPU lenta — 20 re-cliques em 100s, todos falharam. O
    clique por coordenada abriu o modal nos mesmos cenários."""
    await loc.wait_for(state="visible", timeout=timeout_ms)
    caixa = await loc.bounding_box(timeout=timeout_ms)
    if caixa is None:
        raise SymplaError("elemento sem posição na tela")
    await page.mouse.click(caixa["x"] + caixa["width"] / 2,
                           caixa["y"] + caixa["height"] / 2)


async def _visivel(loc) -> bool:
    try:
        return await loc.is_visible()
    except Exception:
        return False


async def _abrir_login(page) -> None:
    """Abre o formulário de e-mail+senha. Caminho CONFIRMADO num Chromium de
    verdade contra o site ao vivo (24/09/2026), não inferido:

      1. o botão do header com aria-label "Open Dropdown" (Radix) — é o
         pílula hambúrguer+boneco que o dono circulou no print. Deslogado,
         ele NÃO abre menu nenhum: abre direto o modal "Que bom ter você
         aqui!";
      2. no modal, "Continuar com e-mail e senha" — só então aparecem os
         campos de e-mail e senha.

    Cada volta do laço OLHA A TELA antes de agir, em vez de repetir cliques
    às cegas: numa CPU lenta (reproduzido com limite de 6x) o 1º clique é
    processado atrasado — o modal abre DEPOIS que o 2º clique já saiu, e o
    2º cai no fundo escuro e FECHA o modal. Então: senha visível → pronto;
    opção "e-mail e senha" visível → modal aberto, clica nela; senão → abre
    o modal. Um fechamento atrasado só custa mais uma volta.

    Histórico: versões anteriores procuravam um item de menu que não existe
    e apertavam Escape (fechando o modal), e o plano B por texto clicava no
    FAQ "Não consigo acessar minha conta" do rodapé da home — o print que
    parecia a Central de Ajuda era isso."""
    gatilho = page.get_by_role(
        "button", name=re.compile(r"^open dropdown$", re.I)).first
    opcao = page.get_by_role(
        "button", name=re.compile(r"e-?mail e senha", re.I)).first
    senha = page.locator("input[type=password]:visible").first
    # Título do modal (visto ao vivo). Modal aberto com a opção ainda não
    # desenhada = ESPERAR: clicar no botão de novo cairia no fundo escuro e
    # fecharia o modal.
    modal = page.get_by_text(re.compile(r"que bom ter voc", re.I)).first
    import time as _time
    inicio = _time.monotonic()
    prazo = inicio + LOGIN_ABRIR_TIMEOUT_S
    ultimo_erro: Exception | None = None
    # O Pi falhou aqui e não consegui reproduzir (nem com a mesma build do
    # Chromium e CPU 20x mais lenta): o rastro e o print do meio existem pra
    # a próxima falha dizer O QUE a tela mostrava, em vez de eu adivinhar.
    rastro: list[str] = []
    print_meio: bytes | None = None
    while _time.monotonic() < prazo:
        try:
            await _passar_cloudflare(page)
            if await _visivel(senha):
                return
            if await _visivel(opcao):
                rastro.append(f"{_time.monotonic() - inicio:.0f}s opção visível → clico nela")
                await _clicar(page, opcao)
                await senha.wait_for(state="visible", timeout=10_000)
                return
            if await _visivel(modal):
                rastro.append(f"{_time.monotonic() - inicio:.0f}s modal aberto sem a opção → espero")
                await opcao.wait_for(state="visible", timeout=10_000)
                continue
            rastro.append(f"{_time.monotonic() - inicio:.0f}s clico no botão da conta")
            await _clicar(page, gatilho)
            await opcao.wait_for(state="visible", timeout=10_000)
        except Exception as exc:
            ultimo_erro = exc
            if print_meio is None:
                print_meio = await _screenshot_seguro(page)
                rastro.append(
                    f"{_time.monotonic() - inicio:.0f}s (print do meio) "
                    f"modal={await _visivel(modal)} "
                    f"desafio={await _cloudflare_na_tela(page)} "
                    f"url={getattr(page, 'url', '?')}")
    visiveis = await _dump_clicaveis(page)
    erro = str(ultimo_erro).splitlines()[0] if ultimo_erro else "-"
    raise SymplaError(
        f"não consegui abrir o formulário de e-mail e senha em "
        f"{LOGIN_ABRIR_TIMEOUT_S:.0f}s (último erro: {erro}). "
        f"Rastro: {' | '.join(rastro[:12])}. "
        f"Elementos visíveis na tela: {visiveis}",
        print_meio=print_meio,
    )


async def _login(page, creds: SymplaCredenciais) -> None:
    await _ir(page, BASE_URL)
    await _abrir_login(page)

    # :visible é obrigatório (visto ao vivo): há um 2º input[type=email]
    # escondido no modal, e a busca "Buscar experiências" (type=text) vem
    # ANTES do modal no DOM — o seletor antigo digitava o e-mail na busca.
    campo_email = page.locator("input[type=email]:visible").first
    campo_senha = page.locator("input[type=password]:visible").first
    await campo_email.fill(creds.email, timeout=30_000)
    await campo_senha.fill(creds.senha, timeout=30_000)

    # O botão ENTRAR é type="button" (não submit), confirmado ao vivo.
    await _clicar(page, page.get_by_role(
        "button", name=re.compile(r"^entrar$", re.I)).first, 30_000)

    # Login certo fecha o modal (o campo some). Login errado deixa aberto
    # com "E-mail ou senha inválidos" (texto visto ao vivo com conta falsa).
    campo = page.locator("input[type=password]").first
    recusa = page.get_by_text(re.compile(r"senha inv[aá]lid", re.I))
    import time as _time
    prazo = _time.monotonic() + 60
    while _time.monotonic() < prazo:
        if await recusa.count():
            raise SymplaError(
                "a Sympla recusou o login: 'E-mail ou senha inválidos'. "
                "Confira com /sympla_setup email e /sympla_setup senha.")
        if await _cloudflare_na_tela(page):
            # O campo some atrás do desafio — isso NÃO é login concluído.
            await _passar_cloudflare(page)
            continue
        if not await _visivel(campo):
            return
        await page.wait_for_timeout(1_000)
    raise SymplaError(
        "cliquei em ENTRAR mas o login não concluiu em 60s (o formulário "
        "continuou aberto, sem mensagem de erro).")


async def _buscar_candidatos(page, query: str) -> list[EventoCandidato]:
    """Busca é renderizada client-side — precisa de JS rodando, por isso é
    Playwright e não um scraper leve à parte."""
    url = SEARCH_URL.format(query=quote(query))
    await _ir(page, url)
    # Espera o ESTADO FINAL da busca, visto ao vivo: "N eventos encontrados"
    # ou "Que tal tentar outra busca" (zero resultados). Não dá pra esperar
    # só por link de evento: com zero resultados a página mostra "Eventos em
    # alta" (links de outros eventos). E o timeout antigo (10s, devolvendo
    # lista vazia) virava "não achei" em CPU lenta — com a CPU limitada 12x
    # os resultados levaram 13,4s pra aparecer.
    estado = page.get_by_text(
        re.compile(r"\d+\s+eventos?\s+encontrad|tentar outra busca", re.I)).first
    try:
        await estado.wait_for(state="visible", timeout=BUSCA_TIMEOUT_MS)
    except Exception as exc:
        raise SymplaError(
            f"a busca da Sympla não terminou de carregar em "
            f"{BUSCA_TIMEOUT_MS // 1000}s"
        ) from exc
    if await page.get_by_text(re.compile(r"tentar outra busca", re.I)).count():
        return []
    await page.wait_for_selector("a[href*='/evento/']", timeout=BUSCA_TIMEOUT_MS)
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
    await _ir(page, cand.url)
    payload = await _ler_hydration(page)
    # Página que não deu pra ler NÃO é "evento que não serve": pular em
    # silêncio fazia um evento bloqueado virar "não achei nenhum evento".
    ev = evento_da_hydration(cand.url, payload) if payload else None
    if ev is None:
        raise SymplaError(f"não consegui ler a página do evento {cand.url}")
    return ev


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
    """Seleciona `qty` ingressos do PRIMEIRO tipo da lista e avança.

    Conferido ao vivo (24/09/2026) num evento aberto de verdade, sem
    comprar: o "+" é um botão com aria-label "Increase Amount" (um por tipo
    de ingresso; NÃO existe botão chamado "+", que era o que a versão
    anterior procurava — falharia na quarta), e o avançar é
    data-testid="buy-button" (há dois na página, um escondido), que muda de
    "Selecione um Ingresso" pra "2 Comprar Ingressos" com 2 selecionados.

    Não conferido: se o concerto grátis tem um tipo só (o código pega o
    primeiro) e o que vem depois do clique em comprar."""
    mais = page.get_by_role(
        "button", name=re.compile(r"^increase amount$", re.I)).first
    for _ in range(qty):
        await _clicar(page, mais, 30_000)
        await page.wait_for_timeout(500)
    comprar = page.locator("[data-testid=buy-button]:visible").first
    texto = (await comprar.inner_text(timeout=30_000)).strip()
    # Confere a quantidade na própria tela antes de avançar: limite por
    # pessoa menor que `qty`, ou clique perdido, aparece aqui em vez de
    # seguir com a quantidade errada.
    if not re.match(rf"^{qty}\b", texto):
        raise SymplaError(
            f"pedi {qty} ingresso(s), mas o botão de compra mostra "
            f"'{' '.join(texto.split())}'.")
    await _clicar(page, comprar, 30_000)


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
    await _clicar(page, finalizar, 30_000)


async def retirar_ingresso(
    creds: SymplaCredenciais, query: str, qty: int, *, agora: datetime | None = None,
    poll_timeout_s: float = POLL_TIMEOUT_S, on_etapa=None,
) -> SymplaResultado:
    """Orquestra o fluxo inteiro. Qualquer exceção numa etapa vira resultado
    de FALHA com screenshot — nunca propaga pro chamador como traceback cru,
    porque quem chama precisa poder avisar o dono mesmo quando algo aqui
    quebra de um jeito que eu não previ.

    Achado no 3º uso real (23/09/2026): "não mandou nada fora essa msg" — a
    screenshot de falha NUNCA chegava, desde a 1ª versão. O `finally:
    await browser.close()` fechava o navegador ANTES da exceção alcançar o
    except de fora; `_screenshot_seguro(page)` numa página já fechada
    estoura e é engolido em silêncio DENTRO da própria função (ela existe
    pra não derrubar o fluxo por causa do print, não pra esconder que
    falhou). Por isso a captura agora acontece no except INTERNO, com o
    browser ainda vivo — só o caso "evento não encontrado" (um `return`
    direto, não uma exceção) escapava do bug, e foi o único que já
    funcionava."""
    from playwright.async_api import async_playwright

    hoje = (agora or datetime.now(BRT)).astimezone(BRT).date()
    etapa = "iniciar navegador"

    async def _avisar(nova: str) -> None:
        nonlocal etapa
        etapa = nova
        if on_etapa is not None:
            try:
                await on_etapa(nova)
            except Exception:
                logger.exception("sympla: falha ao avisar progresso")

    falha_screenshot: bytes | None = None
    falha_print_meio: bytes | None = None
    falha_extra = ""
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=CHROMIUM_ARGS)
            page = None
            try:
                context = await browser.new_context(locale="pt-BR")
                page = await context.new_page()

                await _avisar("login")
                await _login(page, creds)

                await _avisar("localizar o evento da semana")
                evento = None
                import time as _time
                deadline = _time.monotonic() + poll_timeout_s
                erro_busca: Exception | None = None
                while True:
                    try:
                        evento = await _localizar_evento(page, query, hoje)
                        erro_busca = None
                    except SymplaError as exc:
                        # Busca que não carregou NÃO é "não tem evento":
                        # tenta de novo até o prazo e, se a última falhou,
                        # reporta como "não consegui checar".
                        evento, erro_busca = None, exc
                    if evento is not None or _time.monotonic() >= deadline:
                        break
                    await page.wait_for_timeout(int(POLL_INTERVALO_S * 1000))
                if evento is None and erro_busca is not None:
                    return SymplaResultado(
                        False, etapa,
                        f"não consegui checar se o evento saiu: {erro_busca}",
                        screenshot=await _screenshot_seguro(page),
                    )
                if evento is None:
                    return SymplaResultado(
                        False, etapa,
                        f"não achei nenhum evento aberto pra '{query}'"
                        + (f" em {poll_timeout_s:.0f}s de tentativas."
                           if poll_timeout_s else " (busca única)."),
                        screenshot=await _screenshot_seguro(page),
                    )

                await _avisar("abrir o evento")
                await _ir(page, evento.url)

                await _avisar("selecionar ingressos e reservar")
                await _selecionar_e_reservar(page, qty)

                await _avisar("preencher checkout")
                await _preencher_checkout(page, creds)

                return SymplaResultado(
                    True, "concluído",
                    f"{qty} ingresso(s) retirado(s) para '{evento.titulo}'.",
                    evento_titulo=evento.titulo, evento_url=evento.url,
                    screenshot=await _screenshot_seguro(page),
                )
            except Exception as exc:
                # CAPTURA AQUI, com o browser ainda vivo — é a correção do
                # bug do print que nunca chegava (ver docstring). O
                # re-raise devolve pro except de fora, que só MONTA a
                # mensagem; não precisa mais tocar a página.
                if page is not None:
                    falha_screenshot = await _screenshot_seguro(page)
                    falha_print_meio = getattr(exc, "print_meio", None)
                    # SymplaError já embute o dump de elementos visíveis
                    # quando faz sentido (ver _abrir_login). Pras demais
                    # etapas, monta aqui — transforma "estourou de novo" em
                    # "aqui está o texto certo do botão".
                    if not isinstance(exc, SymplaError):
                        visiveis = await _dump_clicaveis(page)
                        falha_extra = f" | elementos visíveis: {visiveis}"
                raise
            finally:
                await browser.close()
    except Exception as exc:
        logger.exception("sympla: falha na etapa '%s'", etapa)
        detalhe = f"{type(exc).__name__}: {exc}{falha_extra}"
        return SymplaResultado(
            False, etapa, detalhe, screenshot=falha_screenshot,
            print_meio=falha_print_meio,
        )
