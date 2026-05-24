from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.db.models import User

router = Router(name=__name__)


HELP_TEXT = (
    "🤖 <b>Concierge</b>\n\n"
    "<b>Trânsito casa↔trabalho</b>:\n"
    "• <code>/transito_agora casa</code> | <code>/transito_agora trabalho</code> — força consulta agora\n"
    "• <code>/transito_on</code> / <code>/transito_off</code> — assina/desassina digest diário (seg-sex)\n"
    "• <code>/transito_at HH:MM</code> — muda horário do digest (sem arg volta ao default)\n"
    "• <code>/transito_reset</code> — zera marca de envio de hoje\n"
    "• <code>/transito_alerta_on</code> / <code>/transito_alerta_off</code> — alerta proativo se rota estiver ≥30% acima do habitual\n\n"
    "<b>Medidas Provisórias</b>:\n"
    "• <code>/congresso_agora</code> — força resumo da semana agora\n"
    "• <code>/congresso_on</code> / <code>/congresso_off</code> — assina/desassina digest semanal (segunda)\n"
    "• <code>/congresso_at HH:MM</code> — muda horário do digest\n"
    "• <code>/congresso_reset</code> — zera marca de envio da semana\n\n"
    "<b>Rota com sua localização</b>:\n"
    "• <code>/rota casa</code> | <code>/rota trabalho</code> — atalhos sem geocode\n"
    "• <code>/rota &lt;endereço&gt;</code> — bot pede sua localização e "
    "calcula a rota até o destino\n\n"
    "<b>Busca web</b>:\n"
    "• <code>/buscar &lt;termo&gt;</code> — pesquisa na web. Usa Anthropic se o "
    "provider ativo for Anthropic, senão cai pro Gemini. Cita fontes.\n\n"
    "<b>Tarefas e lembretes</b>:\n"
    "• <code>/nova &lt;texto&gt;</code> — cria tarefa\n"
    "• <code>/tarefas</code> — lista tarefas abertas\n"
    "• <code>/feito &lt;id&gt;</code> — marca tarefa como concluída\n"
    "• <code>/lembrar &lt;texto&gt; em 2h | amanhã 09:00</code> — cria lembrete\n"
    "• <code>/lembretes</code> — lista lembretes pendentes\n"
    "• <code>/apagar_lembrete &lt;id&gt;</code> — apaga um lembrete pendente\n"
    "• <code>/agendar_comando &lt;tipo&gt; [args] &lt;quando&gt;</code> — agenda uma ação (transito/congresso/clima/chat) pra rodar no horário\n\n"
    "<b>LLM</b>:\n"
    "• <code>/ping</code> — testa o LLM atual (mostra provider e modelo)\n"
    "• <code>/provider anthropic|openai|gemini</code> — troca de LLM\n"
    "• <code>/provider_visao anthropic|openai|gemini|auto</code> — provider só pra fotos (auto = segue /provider; não precisa chave nova)\n"
    "• <code>/reset</code> — limpa o contexto da conversa livre\n\n"
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
    "• <i>\"como tá meu cartão esse mês?\"</i> → consulta os últimos lançamentos.\n\n"
    "<b>Lista de compras</b> (persistente):\n"
    "• <i>\"acabou o sal\"</i> / <i>\"preciso comprar arroz\"</i> → adiciona item.\n"
    "• <i>\"compra detergente, papel higiênico e 2kg de açúcar\"</i> → vários itens de uma vez.\n"
    "• <i>\"to indo no mercado\"</i> / <i>\"o que tem na lista?\"</i> → mostra pendentes.\n"
    "• <i>\"comprei o sal\"</i> → marca como comprado (vira ☑️).\n"
    "• <i>\"voltei, limpa o que comprei\"</i> → remove só os marcados.\n"
    "• <i>\"tira o X da lista\"</i> → apaga item permanente.\n\n"
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


@router.message(Command("start"))
async def cmd_start(message: Message, user: User) -> None:
    if user.is_authorized:
        await message.answer("Olá de novo! Digite /help para ver os comandos.")
        return
    await message.answer(
        "Olá! 👋 Este bot é de uso restrito.\n\nDigite a senha de acesso para continuar."
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT, parse_mode="HTML", disable_web_page_preview=True)
