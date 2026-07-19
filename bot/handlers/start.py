from __future__ import annotations

import re
import unicodedata

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.config import settings
from bot.db.models import User

router = Router(name=__name__)


HELP_TEXT = (
    "🤖 <b>Concierge</b>\n\n"
    "<b>Trânsito casa↔trabalho</b>:\n"
    "• <code>/transito_agora casa</code> | <code>/transito_agora trabalho</code> — força consulta agora\n"
    "• <code>/transito_on</code> / <code>/transito_off</code> — assina/desassina digest diário (seg-sex)\n"
    "• <code>/transito_at HH:MM</code> — muda horário do digest (sem arg volta ao default)\n"
    "• <code>/transito_reset</code> — zera marca de envio de hoje\n"
    "• <code>/transito_alerta_on</code> / <code>/transito_alerta_off</code> — alerta proativo se rota estiver ≥30% acima do habitual\n"
    "• <i>\"que horas é melhor sair pro trabalho?\"</i> — testa várias janelas de saída e diz a melhor pra pegar menos trânsito.\n\n"
    "<b>Medidas Provisórias — pauta do Congresso</b>:\n"
    "• <code>/congresso_agora</code> — força resumo da semana agora\n"
    "• <code>/congresso_on</code> / <code>/congresso_off</code> — assina/desassina digest semanal (segunda)\n"
    "• <code>/congresso_at HH:MM</code> — muda horário do digest\n"
    "• <code>/congresso_reset</code> — zera marca de envio da semana\n\n"
    "<b>Câmara — comissões e pautas</b>:\n"
    "• <i>\"quais comissões têm reunião deliberativa hoje?\"</i> → nomes e horários das reuniões.\n"
    "• <i>\"pauta da CCJ hoje\"</i> / <i>\"o que a comissão de Saúde vota amanhã?\"</i> → pauta oficial da comissão (dado da Câmara).\n"
    "• <i>\"quais comissões com reunião hoje têm projeto do Podemos?\"</i> → varre as comissões e cruza autoria/relatoria de um partido/deputado (leva alguns segundos).\n\n"
    "<b>Medidas Provisórias — publicação no Diário Oficial</b>:\n"
    "• <code>/mp_dou_on</code> / <code>/mp_dou_off</code> — assina/desassina o digest diário de MPs novas no DOU\n"
    "• <code>/mp_dou_agora [AAAA-MM-DD]</code> — busca agora; entrega nota técnica (gerada por IA) + DOCX\n"
    "• <code>/dou_provider</code> — escolhe o motor da nota técnica (ex.: <code>/dou_provider 3.5</code>, <code>/dou_provider gemini 3.1-lite</code>, <code>/dou_provider anthropic sonnet</code>, <code>/dou_provider opus</code>, <code>/dou_provider padrao</code>). Ajusta latência/qualidade sem mexer no .env\n"
    "• Por voz/texto: <i>\"saiu MP nova hoje?\"</i> → lista número + ementa\n\n"
    "<b>Agente proativo</b> (opt-in):\n"
    "• <code>/proativo_on</code> / <code>/proativo_off</code> — liga/desliga avisos automáticos (vencimentos chegando, briefing matinal, MP nova, lembretes de hábito)\n"
    "• <code>/proativo</code> — status e janelas\n"
    "• <code>/proativo_agora</code> — força a checagem agora (teste)\n\n"
    "<b>Rota com sua localização</b>:\n"
    "• <code>/rota casa</code> | <code>/rota trabalho</code> — atalhos sem geocode\n"
    "• <code>/rota &lt;endereço&gt;</code> — bot pede sua localização e "
    "calcula a rota até o destino\n\n"
    "<b>Busca web</b>:\n"
    "• <code>/buscar &lt;termo&gt;</code> — pesquisa na web. Usa Anthropic se o "
    "provider ativo for Anthropic, senão cai pro Gemini. Cita fontes.\n"
    "• Preço de produto: <i>\"quanto custa uma GoPro Hero 13?\"</i>, "
    "<i>\"preço do iPhone 16\"</i>, <i>\"onde compro um monitor 4k?\"</i> → "
    "acha o preço e onde comprar (não precisa de comando).\n"
    "• Lugar/estabelecimento: <i>\"telefone da farmácia X\"</i>, <i>\"que horas "
    "abre o shopping Y\"</i>, <i>\"endereço da clínica Z\"</i> → dados oficiais "
    "do Google.\n\n"
    "<b>Cinema</b> (rede Cinemark):\n"
    "• <i>\"que horas passa o filme X no Iguatemi Brasília?\"</i> / "
    "<i>\"programação do Pier 21 amanhã\"</i> → sessões oficiais (2D/3D, "
    "dublado/legendado), qualquer data.\n\n"
    "<b>Clima</b>:\n"
    "• <i>\"qual a previsão pra hoje?\"</i> / <i>\"vai chover essa semana?\"</i> "
    "→ previsão do tempo (hoje ou próximos 7 dias, dia a dia).\n\n"
    "<b>Cotações</b> (ao vivo):\n"
    "• <i>\"quanto está o dólar?\"</i>, <i>\"cotação da PETR4\"</i>, "
    "<i>\"quanto tá o HGLG11 / bitcoin?\"</i> → preço atual de moeda, ação, "
    "FII, ETF ou cripto (B3 e mercados).\n\n"
    "<b>Tarefas e lembretes</b>:\n"
    "• <code>/nova &lt;texto&gt;</code> — cria tarefa\n"
    "• <code>/tarefas</code> — lista tarefas abertas\n"
    "• <code>/feito &lt;id&gt;</code> — marca tarefa como concluída\n"
    "• <code>/lembrar &lt;texto&gt; em 2h | amanhã 09:00</code> — cria lembrete\n"
    "• <code>/lembretes</code> — lista lembretes pendentes\n"
    "• <code>/apagar_lembrete &lt;id&gt;</code> — apaga um lembrete pendente\n"
    "• <code>/agendar_comando &lt;tipo&gt; [args] &lt;quando&gt;</code> — agenda uma ação (transito/congresso/clima/chat) pra rodar no horário\n"
    "• Recorrência por chat: <i>\"todo dia útil 7h…\"</i>, <i>\"a cada 2 horas…\"</i> (aceita padrão cron)\n\n"
    "<b>LLM</b>:\n"
    "• <code>/ping</code> — testa o LLM atual (mostra provider e modelo)\n"
    "• <code>/provider anthropic|openai|gemini</code> — troca de LLM. Dá pra fixar o modelo por id: <code>/provider gemini gemini-3.5-flash</code> | <code>/provider anthropic claude-sonnet-5</code> | <code>/provider openai gpt-5.1</code> (sem id = volta ao .env; no Gemini ainda valem aliases <code>pro</code>|<code>flash</code>)\n"
    "• <code>/provider modelos [provider]</code> — lista dinâmica dos modelos (vem da API; modelo novo aparece sozinho)\n"
    "• <code>/provider_visao anthropic|openai|gemini|auto</code> — provider só pra fotos (auto = segue /provider). <code>/provider_visao modelos</code> lista só modelos com entrada de imagem\n"
    "• <code>/voice gemini|openai</code> — provider da transcrição de voz. Gemini converte voz→/comando; OpenAI (Whisper) é literal mas mais estável. <code>/voice modelos</code> lista só modelos com entrada de áudio\n"
    "• <code>/tradutor &lt;idioma&gt;</code> — modo tradutor de VIA DUPLA: manda áudio, recebe a tradução em texto e voz; áudio já no idioma-alvo (a resposta do atendente) volta em português (ex.: <code>/tradutor japonês</code>). <code>/tradutor off</code> desliga\n"
    "• <code>/tradutor_provider openai|gemini [id]</code> — motor do tradutor (openai não treina; gemini grátis pode). Fixa o modelo por id (ex.: <code>gemini gemini-3.1-flash-lite</code>); <code>/tradutor_provider modelos</code> lista os de áudio\n"
    "• <code>/reset</code> — limpa o contexto da conversa livre\n"
    "• <code>/reset_memoria [tudo]</code> — zera a memória de longo prazo "
    "(resumo automático; com <code>tudo</code>, também o histórico pesquisável)\n"
    "• Memória: o bot lembra da conversa entre restarts, resume o que sai do "
    "contexto e busca em conversas antigas (<i>\"o que eu te falei sobre…?\"</i>)\n\n"
    "<b>Academia</b>:\n"
    "• Mande voz/texto descrevendo o treino: <i>\"hoje malhei peito e fiz 10min de cardio\"</i>, "
    "<i>\"ontem treinei costas\"</i> — bot registra.\n"
    "• Categorias: <code>peito</code>, <code>costas</code>, <code>pernas</code>, <code>cardio</code>. "
    "Subdivisões (supino/panturrilha/esteira) são normalizadas; ombros/braço/abdomen não entram.\n"
    "• <i>\"como tá minha semana de academia?\"</i> → resumo dom → sab.\n"
    "• <i>\"apaga o treino de hoje\"</i> / <i>\"errei, não treinei isso ontem\"</i> "
    "→ limpa os registros do dia pra você regravar.\n"
    "• Histórico zera todo domingo — só a semana corrente fica.\n\n"
    "<b>Gerenciador financeiro</b> (Firestore):\n"
    "• <code>/financeiro_setup</code> — configura service account (envie JSON) e UID do Firebase.\n"
    "• <i>\"lança 250 no cartão de crédito, mercado, hoje\"</i> → cria compra no cartão.\n"
    "• <i>\"paguei conta de luz 180\"</i> / <i>\"recebi 5 mil de salário\"</i> → lançamento no banco.\n"
    "• <i>\"aportei 1000 no Tesouro IPCA+ 2035\"</i> → contribuição em título existente.\n"
    "• <i>\"como tá meu cartão esse mês?\"</i> → consulta os últimos lançamentos.\n"
    "• <i>\"qual meu saldo?\"</i> / <i>\"quanto tá a fatura em aberto?\"</i> → saldo bancário atual e total da fatura do ciclo.\n\n"
    "<b>Análise de gastos</b>:\n"
    "• <i>\"quanto gastei com alimentação em maio?\"</i> / <i>\"maior categoria do trimestre\"</i>\n"
    "• <i>\"evolução dos meus gastos mês a mês\"</i> / <i>\"compara maio e junho\"</i>\n"
    "• <i>\"gastei mais nas semanas que treinei?\"</i> — cruza dados quando dá.\n\n"
    "<b>Desfazer</b>:\n"
    "• <i>\"desfaz\"</i> / <i>\"errei, cancela isso\"</i> → desfaz o último lançamento/tarefa/lembrete/item criado pelo bot. Encadeia (chama de novo desfaz o anterior).\n\n"
    "<b>Agendamento recorrente</b>:\n"
    "• <i>\"todo domingo 20h me manda o resumo da semana\"</i> → dispara sozinho no horário, repetindo.\n\n"
    "<b>Lista de compras</b> (persistente):\n"
    "• <i>\"acabou o sal\"</i> / <i>\"preciso comprar arroz\"</i> → adiciona item.\n"
    "• <i>\"compra detergente, papel higiênico e 2kg de açúcar\"</i> → vários itens de uma vez.\n"
    "• <i>\"to indo no mercado\"</i> / <i>\"o que tem na lista?\"</i> → mostra pendentes.\n"
    "• <i>\"comprei o sal\"</i> → marca como comprado (vira ☑️).\n"
    "• <i>\"voltei, limpa o que comprei\"</i> → remove só os marcados.\n"
    "• <i>\"tira o X da lista\"</i> → apaga item permanente.\n\n"
    "<b>Viagens — passagens e hotéis</b> (via SerpAPI):\n"
    "• <i>\"acha voo BSB→GRU saindo 15/07 voltando 22/07\"</i> / "
    "<i>\"tem passagem pra São Paulo amanhã?\"</i> → busca agora e mostra a melhor oferta.\n"
    "• <i>\"hotel em Paris 10–14/09 pra 2 pessoas\"</i> / "
    "<i>\"hospedagem barata em Salvador semana que vem\"</i> → busca diárias.\n"
    "• <i>\"monitora esse trecho até R$1500\"</i> / <i>\"me avisa quando esse hotel cair\"</i> → "
    "cria watch que roda 1x/dia e alerta na queda.\n"
    "• <i>\"minhas viagens monitoradas\"</i> → lista watches ativos. "
    "<i>\"cancela o watch #3\"</i> → encerra.\n"
    "• Cidades viram IATA automaticamente (Brasília=BSB, NY=JFK, etc.); datas relativas (\"sexta\", \"15/07\") também.\n\n"
    "<b>Imagens</b>:\n"
    "• Mande uma foto (com ou sem caption) — o bot analisa via LLM agente. "
    "Casos típicos: OCR de recibo/boleto, leitura de placa de rua, resumo "
    "de screenshot, identificação de objetos. O LLM pode acionar tools "
    "(criar lembrete a partir de boleto, agendar trânsito a partir de endereço, etc).\n\n"
    "<b>Voz</b>:\n"
    "• Mande um áudio — o bot transcreve via Gemini e executa o comando "
    "(se começar com /) ou responde no chat livre.\n"
    "• <b>Slash literal</b>: <i>\"barra trânsito agora casa\"</i> → "
    "<code>/transito_agora casa</code>.\n"
    "• <b>Trânsito natural</b>: <i>\"trânsito para casa\"</i>, "
    "<i>\"trânsito para o trabalho\"</i> (e variantes) → "
    "<code>/transito_agora …</code>.\n"
    "• <b>Congresso natural</b>: <i>\"pauta de MP do congresso agora\"</i> → "
    "<code>/congresso_agora</code>.\n"
    "• <b>Rota natural</b>: <i>\"rota para casa\"</i>, "
    "<i>\"como chegar em Avenida Paulista 1000\"</i> → "
    "<code>/rota …</code> (bot pede sua localização).\n"
    "• <b>Busca natural</b>: <i>\"busca X\"</i>, <i>\"pesquisa X\"</i>, "
    "<i>\"procura X\"</i>, <i>\"google X\"</i> → <code>/buscar …</code>.\n"
    "• Em conversa casual sobre trânsito ou MPs, transcrição literal "
    "(cai no chat livre, não dispara comando).\n\n"
    "Mensagens de texto livre são enviadas ao LLM com contexto curto."
)

# Seção exibida só pro dono do bot (OWNER_TELEGRAM_ID) — os demais usuários
# não ficam sabendo que o recurso existe.
AGENT_HELP_TEXT = (
    "<b>Agente de execução de código</b> (só você):\n"
    "• <code>/agente &lt;tarefa&gt;</code> — escreve, EXECUTA e itera código "
    "num workspace isolado; pesquisa docs na web; entrega os arquivos prontos. "
    "Ex.: <i>/agente cria um script que baixa cotações e plota um gráfico</i>\n"
    "• Em linguagem natural também: <i>\"constrói um app que…\"</i> "
    "(texto ou voz). Por voz com slash: <i>\"barra agente …\"</i>.\n"
    "• Após terminar, RESPONDER (reply) à mensagem de entrega CONTINUA a "
    "mesma tarefa (janela configurável). Texto solto segue como chat normal; "
    "<code>/agente_fim</code> encerra a janela antes da hora.\n"
    "• SSH/rede local (ex.: backup de pasta de outra máquina): chave em "
    "<code>./workspace/.ssh</code> — passo a passo em <code>/agente</code> "
    "(sem argumentos)\n"
    "• Anexos: mande um documento e ele é salvo em "
    "<code>workspace/uploads/</code> (caption <i>\"guarda como nome.ext\"</i> "
    "renomeia; PDF sem caption vai pra análise). <code>/arquivos</code> "
    "lista, <code>/arquivos baixar &lt;nome&gt;</code> manda o arquivo de "
    "volta no chat, <code>/arquivos apagar &lt;nome&gt;</code> remove — ou "
    "pergunte <i>\"que arquivos você tem salvos?\"</i>. No agente: "
    "<i>/agente pega o uploads/X e…</i>\n"
    "• <code>/agente_status</code> · <code>/agente_parar</code>\n"
    "• <code>/agente_config</code> — modelo/timeout/turnos/custo/ttl em "
    "runtime, sem reiniciar (ex.: <code>/agente_config modelo opus</code>)\n"
    "• GitHub: com AGENT_GITHUB_TOKEN no .env, o agente clona, commita, "
    "faz push e abre PRs.\n"
    "• Agendado (cron): <i>\"todo dia útil 7h, roda o agente pra…\"</i> — "
    "execução recorrente. Comando shell fixo sem LLM: <i>\"todo dia 3h roda "
    "&lt;comando&gt;\"</i> (tipo shell). Veja em /lembretes; apaga com "
    "/apagar_lembrete.\n"
    "• Guardrails: confinado a ./workspace, env limpo, 1 tarefa por vez, "
    "teto de custo por tarefa (+ teto diário pro agendado).\n"
)


@router.message(Command("start"))
async def cmd_start(message: Message, user: User) -> None:
    if user.is_authorized:
        await message.answer("Olá de novo! Digite /help para ver os comandos.")
        return
    await message.answer(
        "Olá! 👋 Este bot é de uso restrito.\n\nDigite a senha de acesso para continuar."
    )


def _chunk_text(text: str, limit: int = 4000) -> list[str]:
    """Quebra o texto em blocos < limit do Telegram (4096), preferindo
    cortar em quebras de parágrafo pra não partir tags HTML no meio."""
    chunks: list[str] = []
    current = ""
    for para in text.split("\n\n"):
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = para
    if current:
        chunks.append(current)
    return chunks


# ─────────────── Ajuda natural (tool `ajuda`) ───────────────
# O HELP_TEXT é a FONTE ÚNICA: fatiamos ele em seções (cada bloco começa com
# <b>Título</b>) pra que a tool `ajuda` devolva o trecho EXATO — verbatim, sem
# o LLM improvisar a sintaxe dos comandos (flash-lite erraria /lembrar etc.).


def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    ).lower()


def _parse_help_sections(help_text: str) -> list[tuple[str, str]]:
    """[(título_sem_tags, bloco_html)] das seções do HELP_TEXT (blocos separados
    por linha em branco, começando com <b>Título</b>). Ignora o cabeçalho."""
    out: list[tuple[str, str]] = []
    for bloco in help_text.split("\n\n"):
        bloco = bloco.strip()
        m = re.match(r"<b>(.+?)</b>", bloco)
        if not m or "•" not in bloco and "<i>" not in bloco:
            continue  # cabeçalho "🤖 Concierge" ou bloco sem itens
        titulo = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        out.append((titulo, bloco))
    return out


_HELP_SECTIONS: list[tuple[str, str]] = _parse_help_sections(HELP_TEXT)

# palavra-chave (sem acento) → fragmento do TÍTULO da seção a casar.
_HELP_KEYWORDS: dict[str, str] = {
    "lembrete": "lembrete", "lembrar": "lembrete", "lembretes": "lembrete",
    "tarefa": "lembrete", "tarefas": "lembrete", "reminder": "lembrete", "afazer": "lembrete",
    "compra": "compras", "compras": "compras", "lista": "compras",
    "mercado": "compras", "comprar": "compras", "supermercado": "compras",
    "transito": "transito", "trafego": "transito", "engarrafamento": "transito",
    "rota": "rota", "chegar": "rota", "caminho": "rota", "trajeto": "rota",
    "voo": "viagens", "voos": "viagens", "passagem": "viagens", "passagens": "viagens",
    "hotel": "viagens", "hospedagem": "viagens", "viagem": "viagens", "viagens": "viagens",
    "fuso": "viagens", "modo viagem": "viagens",
    "mp": "diario oficial", "dou": "diario oficial", "medida provisoria": "diario oficial",
    "nota tecnica": "diario oficial",
    "congresso": "pauta do congresso", "pauta": "pauta do congresso",
    "financeiro": "gerenciador financeiro", "cartao": "gerenciador financeiro",
    "despesa": "gerenciador financeiro", "fatura": "gerenciador financeiro",
    "banco": "gerenciador financeiro", "salario": "gerenciador financeiro",
    "aporte": "gerenciador financeiro", "investimento": "gerenciador financeiro",
    "gasto": "analise de gastos", "gastos": "analise de gastos",
    "saldo": "gerenciador financeiro", "extrato": "gerenciador financeiro",
    # câmara — comissões/pautas (consultar_pauta_camara / varrer_comissoes_partido)
    "comissao": "camara", "comissoes": "camara", "camara": "camara",
    "deliberativa": "camara", "relatoria": "camara", "autoria": "camara",
    "ccj": "camara",
    # cinema (consultar_sessoes_cinema)
    "cinema": "cinema", "filme": "cinema", "filmes": "cinema",
    "sessao": "cinema", "sessoes": "cinema", "programacao": "cinema",
    # clima (consultar_clima)
    "chuva": "clima", "chover": "clima", "previsao": "clima",
    # cotações (consultar_cotacao)
    "cotacao": "cotacoes", "cotacoes": "cotacoes", "dolar": "cotacoes",
    "euro": "cotacoes", "bitcoin": "cotacoes", "cripto": "cotacoes",
    "acoes": "cotacoes", "fii": "cotacoes", "bolsa": "cotacoes",
    # melhor horário de sair (melhor_horario_sair) — é trânsito
    "melhor horario": "transito", "melhor hora": "transito", "sair": "transito",
    "academia": "academia", "treino": "academia", "malhar": "academia", "malho": "academia",
    "tradutor": "llm", "traduzir": "llm", "traducao": "llm",
    "provider": "llm", "modelo": "llm", "llm": "llm", "voz": "voz", "audio": "voz",
    "memoria": "llm", "lembra": "llm",
    "foto": "imagens", "imagem": "imagens", "recibo": "imagens", "boleto": "imagens",
    "proativo": "proativo", "briefing": "proativo", "aviso": "proativo",
    "busca": "busca web", "pesquisa": "busca web", "pesquisar": "busca web",
    "buscar": "busca web", "google": "busca web", "internet": "busca web",
    # preço/produto e lugar caem na seção de Busca (buscar_preco / buscar_local)
    "preco": "busca web", "precos": "busca web", "produto": "busca web",
    "produtos": "busca web", "custa": "busca web", "custo": "busca web",
    "onde comprar": "busca web", "onde compro": "busca web",
    "telefone": "busca web", "endereco": "busca web", "horario": "busca web",
    "shopping": "busca web",  # pt-BR: shopping = o lugar (não a lista)
    "clima": "clima", "tempo": "clima",  # seção Clima dedicada (consultar_clima)
    # inglês (a esposa fala PT, mas o LLM às vezes passa o assunto em EN)
    "reminder": "lembrete", "task": "lembrete", "shopping list": "compras",
    "grocery": "compras", "traffic": "transito", "flight": "viagens",
    "translate": "llm", "workout": "academia",
}


def find_help_sections(query: str) -> list[str]:
    """Blocos HTML do HELP_TEXT que casam com o assunto perguntado. Casa por
    palavra-chave (LIMITE DE PALAVRA — senão 'mp' casaria dentro de 'co-mp-ras')
    e também direto contra o título da seção. Vazio se nada bate."""
    q = _strip_accents(query or "")
    alvos: set[str] = set()
    for kw, alvo in _HELP_KEYWORDS.items():
        if re.search(rf"\b{re.escape(kw)}\b", q):
            alvos.add(alvo)
    if not alvos:  # tenta casar o texto direto contra os títulos
        for titulo, _bloco in _HELP_SECTIONS:
            if _strip_accents(titulo) in q or q in _strip_accents(titulo):
                alvos.add(_strip_accents(titulo))
    out: list[str] = []
    for titulo, bloco in _HELP_SECTIONS:
        nt = _strip_accents(titulo)
        if any(a in nt for a in alvos):
            out.append(bloco)
    return out


def help_topic_list() -> str:
    """Lista curta dos tópicos (títulos das seções) pro caso 'sem match'."""
    titulos = [t for t, _ in _HELP_SECTIONS]
    return "\n".join(f"• {t}" for t in titulos)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    text = HELP_TEXT
    if (
        settings.owner_telegram_id
        and message.from_user
        and message.from_user.id == settings.owner_telegram_id
    ):
        text = AGENT_HELP_TEXT + "\n" + text
    for chunk in _chunk_text(text):
        await message.answer(chunk, parse_mode="HTML", disable_web_page_preview=True)
