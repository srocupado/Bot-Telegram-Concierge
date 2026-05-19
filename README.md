# Bot Telegram Concierge

Assistente pessoal de atividades diárias no Telegram. Combina digests automáticos
de **trânsito + clima** (rota casa↔trabalho) e **Medidas Provisórias** do
Congresso Nacional com **tarefas**, **lembretes**, **chat livre com LLM**
multi-provider (Anthropic/OpenAI/Gemini) e troca de modelo em runtime.

## Funcionalidades

- **Trânsito diário** (seg–sex, 07:20 BRT por padrão): rota casa↔trabalho com
  tempo real (Google Directions API) + previsão do tempo (Open-Meteo).
  Suporta rota preferida via URL do Google Maps com waypoints.
- **Medidas Provisórias** (segunda, 07:00 BRT por padrão): agenda do
  Congresso Nacional via web scraping (BeautifulSoup), filtrando MPs e CMMPV.
- **Tarefas**: lista persistente — `/nova`, `/tarefas`, `/feito`.
- **Lembretes** com horário em linguagem natural (português, via `dateparser`):
  `/lembrar reunião amanhã 09:00`, `/lembrar tomar remédio em 30min`.
- **Chat livre com LLM**: mensagens de texto livre viram conversa com contexto
  curto (TTL 30 min, em RAM).
- **Mensagens de voz**: áudio é transcrito via **Gemini 2.5 Flash multimodal**
  (aceita OGG/Opus nativo, sem ffmpeg) e roteado como comando (se começa com
  `/`) ou como chat livre.
- **Multi-provider LLM**: Anthropic (default), OpenAI, Gemini.
  Troca em runtime via `/provider`. Preferência persistida por usuário.
- **Acesso restrito por senha** (`ACCESS_PASSWORD`); isolamento por
  `telegram_user_id`.

## Comandos

### Trânsito casa↔trabalho
| Comando | Descrição |
|---|---|
| `/transito_now casa` \| `/transito_now trabalho` | Força consulta agora (mostra rota preferida + alternativa) |
| `/transito_on` / `/transito_off` | Assina/desassina o digest diário (seg-sex) |
| `/transito_at HH:MM` | Muda o horário do digest (sem arg volta ao default) |
| `/transito_reset` | Zera marca de envio de hoje (útil pra forçar reenvio) |

### Medidas Provisórias
| Comando | Descrição |
|---|---|
| `/congresso_now` | Força resumo da semana agora |
| `/congresso_on` / `/congresso_off` | Assina/desassina o digest semanal (segunda) |
| `/congresso_at HH:MM` | Muda o horário do digest |
| `/congresso_reset` | Zera marca de envio da semana |

### Tarefas e lembretes
| Comando | Descrição |
|---|---|
| `/nova <texto>` | Cria tarefa |
| `/tarefas` | Lista tarefas abertas |
| `/feito <id>` | Marca tarefa como concluída |
| `/lembrar <texto> em 2h \| amanhã 09:00 \| sexta 18h` | Cria lembrete com horário |
| `/lembretes` | Lista lembretes pendentes |

### LLM e utilitários
| Comando | Descrição |
|---|---|
| `/ping` | Testa o LLM atual (mostra provider e modelo) |
| `/provider anthropic\|openai\|gemini` | Troca o LLM |
| `/reset` | Limpa o contexto da conversa livre |
| `/start` | Início + fluxo de senha |
| `/help` | Lista todos os comandos |

Mensagens de texto sem `/` são tratadas como chat livre com o LLM atual.

### Mensagens de voz

Mande um áudio para o bot. Ele transcreve via **Gemini 2.5 Flash multimodal**
(aceita OGG/Opus do Telegram nativamente) e roteia:

- Se a transcrição começa com `/` → executa o comando (ex: você grava
  *"barra trânsito now casa"*, vira `/transito_now casa`).
- Caso contrário → trata como chat livre, com o mesmo contexto de 30 min em RAM.

O bot ecoa a transcrição antes da resposta (transparência). Audios acima de
`VOICE_MAX_SECONDS` (default 120s) são rejeitados. Para desativar:
`VOICE_ENABLED=false`.

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
# preencher: BOT_TOKEN, ACCESS_PASSWORD, ANTHROPIC_API_KEY,
#            GOOGLE_MAPS_API_KEY, HOME_COORDS, WORK_COORDS,
#            ROUTE_GOOGLE_MAPS_URL (opcional), TIMEZONE
```

A previsão do tempo (Open-Meteo) usa `HOME_COORDS`, sem chave própria.

### Rota preferida (`ROUTE_GOOGLE_MAPS_URL`)

Trace sua rota habitual no Google Maps **com paradas intermediárias** que
forcem o trajeto real → Compartilhar → Copiar link → cole em
`ROUTE_GOOGLE_MAPS_URL`. O bot extrai os waypoints e usa essa rota como
"preferida", comparando com a melhor alternativa do Google em tempo real.

Sem `ROUTE_GOOGLE_MAPS_URL`, usa rota direta + uma alternativa.

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

O SQLite é persistido em `./data/concierge.db` (volume).

### Quando rebuildar vs recriar

| Mudou | Comando |
|---|---|
| Código (`bot/`, `requirements.txt`) | `docker compose up -d --build` |
| Só `.env` | `docker compose up -d --force-recreate` |
| Nada (só restart) | `docker compose restart` |

## Deploy em VM Google Cloud (free tier)

1. Criar **e2-micro** em `us-west1`/`us-central1`/`us-east1`,
   **Standard provisioning**, Debian 12, 30 GB pd-standard.
2. Instalar Docker:
   ```bash
   curl -fsSL https://get.docker.com | sudo sh
   sudo usermod -aG docker $USER
   ```
   Reabra o SSH.
3. Ativar swap (recomendado em 1 GB RAM): `sudo ./scripts/setup-swap.sh`.
4. Clonar o repo → `cp .env.example .env` → preencher.
5. `docker compose up -d --build`.

Custo: 0 USD/mês dentro do free tier (1 e2-micro + 30 GB pd-standard
+ 1 GB egress NA por billing account).

### Swap de memória

A e2-micro tem 1 GB de RAM. Build do Docker + bot pode estourar e causar OOM.

```bash
sudo ./scripts/setup-swap.sh           # 1 GB padrão
sudo ./scripts/setup-swap.sh 2         # 2 GB
```

Idempotente: persiste em `/etc/fstab`, ajusta `vm.swappiness=10`.

### Backup do SQLite

`./scripts/backup.sh` gera `concierge-YYYY-MM-DD-HHMM.tgz` em
`/var/backups/concierge` e mantém 14 dias de histórico.

Agendar via cron diário às 03:00:

```bash
sudo crontab -e
# adicionar:
0 3 * * * /home/USER/apps/Bot-Telegram-Concierge/scripts/backup.sh
```

(troque `USER` pelo seu usuário SSH)

## Modelo de dados

- **`users`**: `id` (telegram_id), `chat_id`, `username`, `first_name`,
  `is_authorized`, `provider`, `timezone`, e campos de subscription:
  `traffic_subscribed`, `traffic_hour/minute`, `last_traffic_digest_at`
  + os equivalentes para `congress_*`.
- **`tasks`**: `id`, `user_id`, `text`, `done`, `created_at`, `done_at`.
- **`reminders`**: `id`, `user_id`, `text`, `due_at` (UTC), `sent`, `sent_at`.

Idempotência dos digests é via os timestamps `last_*_digest_at` no User
(não duplica no mesmo dia/semana).

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
│   ├── traffic.py                # /transito_*
│   ├── congress.py               # /congresso_*
│   ├── tasks.py, reminders.py
│   ├── voice.py                  # F.voice|F.audio → transcribe + dispatch
│   └── chat.py                   # catch-all texto livre
├── services/
│   ├── llm/                      # base + factory + anthropic/openai/gemini
│   ├── traffic.py, weather.py    # Google Directions + Open-Meteo
│   ├── congress.py               # Scraper MP
│   ├── tasks.py, reminders.py
│   ├── chat_memory.py            # in-memory TTL 30min
│   ├── voice.py                  # STT via Gemini multimodal
│   └── scheduler.py              # loop async (trânsito, MP, lembretes)
└── utils/
    └── timez.py
scripts/
├── setup-swap.sh
└── backup.sh
```
