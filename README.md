# Bot Telegram Concierge

Assistente pessoal de atividades diárias no Telegram. Inspirado em
`srocupado/Telegram-Travels`, reaproveita o briefing de **trânsito + clima** e o
resumo semanal de **Medidas Provisórias** do Congresso, e adiciona **tarefas**,
**lembretes**, **chat livre com LLM** e troca de provider em runtime.

## Funcionalidades

- **Trânsito diário** (seg–sex, 07:00 por padrão): rota casa↔trabalho com
  trânsito real (Google Maps Routes) + previsão do tempo (Open-Meteo).
- **Medidas Provisórias** semanais (segunda, 08:00 por padrão).
- **Tarefas**: `/nova`, `/tarefas`, `/feito`.
- **Lembretes**: `/lembrar <texto> em 2h | amanhã 09:00`.
- **Chat livre com LLM**: mensagens de texto livre viram conversa com
  contexto curto (TTL 30 min, em RAM).
- **Multi-provider LLM**: Anthropic (default), OpenAI, Gemini.
  Troca em runtime via `/provider`.
- **Acesso restrito por senha** (`ACCESS_PASSWORD`).

## Comandos

| Comando | Descrição |
|---|---|
| `/start` | Início + fluxo de senha |
| `/help` | Lista de comandos |
| `/ping` | Testa o LLM atual |
| `/provider [anthropic\|openai\|gemini]` | Troca o LLM |
| `/transito` | Força briefing de trânsito + clima agora |
| `/mp` | Resumo de Medidas Provisórias agora |
| `/nova <texto>` | Cria tarefa |
| `/tarefas` | Lista tarefas abertas |
| `/feito <id>` | Marca tarefa como concluída |
| `/lembrar <texto> em/amanhã/...` | Cria lembrete com horário |
| `/lembretes` | Lista lembretes pendentes |
| `/reset` | Limpa o contexto da conversa livre |

Mensagens de texto sem `/` são tratadas como chat livre.

## Stack

- Python 3.12, aiogram 3 (long polling)
- SQLAlchemy 2 async + aiosqlite (SQLite em volume)
- httpx async, BeautifulSoup4
- pydantic-settings, python-json-logger
- SDKs: `anthropic`, `openai`, `google-generativeai`
- `dateparser` para parsing de lembretes em português

## Configuração

```bash
cp .env.example .env
# preencher BOT_TOKEN, ACCESS_PASSWORD, ANTHROPIC_API_KEY, GOOGLE_MAPS_API_KEY,
# HOME_COORDS, WORK_COORDS, WEATHER_LAT, WEATHER_LON, TIMEZONE
```

## Rodar localmente

```bash
pip install -r requirements.txt
python -m bot
```

## Rodar com Docker

```bash
docker compose up -d --build
docker compose logs -f
```

O SQLite é persistido em `./data/concierge.db`.

## Deploy em VM Google Cloud (free tier)

Resumo (ver passo a passo detalhado no plano):

1. Criar e2-micro em `us-west1`/`us-central1`/`us-east1`, **Standard provisioning**, Debian 12, 30 GB pd-standard.
2. Instalar Docker: `curl -fsSL https://get.docker.com | sudo sh && sudo usermod -aG docker $USER`.
3. Clonar repo → `cp .env.example .env` → preencher.
4. `docker compose up -d --build`.

Custo: 0 USD/mês dentro do free tier.

## Estrutura

```
bot/
├── __main__.py, runner.py        # entrypoint, dispatcher, scheduler
├── config.py, logging_setup.py
├── db/
│   ├── base.py, models.py, session.py
├── middlewares/
│   ├── auth.py, db.py
├── handlers/
│   ├── start.py, ping.py, provider.py, reset.py
│   ├── traffic.py, mp.py
│   ├── tasks.py, reminders.py
│   └── chat.py                   # catch-all texto livre
├── services/
│   ├── llm/                      # base + factory + anthropic/openai/gemini
│   ├── traffic.py, weather.py    # Google Maps Routes + Open-Meteo
│   ├── congress.py               # Scraper MP
│   ├── briefing.py               # compõe trânsito + clima
│   ├── tasks.py, reminders.py
│   ├── chat_memory.py            # in-memory TTL 30min
│   └── scheduler.py              # loop async (3 rotinas)
└── utils/
    └── timez.py
```
