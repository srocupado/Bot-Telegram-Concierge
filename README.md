# Bot Telegram Concierge

Assistente pessoal de atividades diárias no Telegram. Combina digests automáticos
de **trânsito + clima** (rota casa↔trabalho) e **Medidas Provisórias** do
Congresso Nacional com **tarefas**, **lembretes**, **chat livre com LLM**
multi-provider (Anthropic/OpenAI/Gemini) e troca de modelo em runtime.

## Funcionalidades

- **Trânsito diário** (seg–sex, 07:20 BRT por padrão): rota casa↔trabalho com
  tempo real (Google Directions API) + previsão do tempo (Open-Meteo)!
  Suporta rota preferida via URL do Google Maps com waypoints.
- **Medidas Provisórias** (segunda, 07:00 BRT por padrão): agenda do
  Congresso Nacional via web scraping (BeautifulSoup), filtrando MPs e CMMPV.
- **Tarefas**: lista persistente — `/nova`, `/tarefas`, `/feito`.
- **Lembretes** com horário em linguagem natural (português, via `dateparser`):
  `/lembrar reunião amanhã 09:00`, `/lembrar tomar remédio em 30min`.
- **Chat livre com LLM agente**: mensagens de texto e voz viram conversa com
  contexto curto (TTL 30 min, em RAM) e **tool use** — o LLM aciona ferramentas
  pra criar/listar/concluir/apagar tarefas e lembretes, consultar clima e
  trânsito, sem você precisar lembrar slash commands.
- **Busca web nativa** no Anthropic (`web_search`, server-side, max 5 buscas
  por turno) e Gemini (`google_search`). O LLM pesquisa em tempo real
  quando você pede notícias, cotações, eventos atuais ou qualquer coisa
  que dependa de dados recentes, citando fontes. OpenAI não tem busca
  nativa nessa versão — use `/provider anthropic|gemini` quando precisar.
- **Mensagens de voz**: áudio é transcrito via **Gemini multimodal**
  (default `gemini-2.5-flash`, configurável via `VOICE_STT_MODEL`), aceita
  OGG/Opus nativo sem ffmpeg, e é roteado como comando (fast-path
  determinístico: rota/trânsito/congresso) ou cai no chat agente.
- **Alerta proativo de trânsito**: scheduler monitora sua rota
  casa→trabalho a cada 10 min na janela [horário do digest - 2h,
  horário + 30min] e avisa se o tempo estiver ≥30% acima da mediana
  habitual do mesmo `(dia da semana, hora)` (com piso de 30 min).
- **Rota com localização atual**: comando `/rota <destino>` (ou voz "rota
  para X") faz o bot pedir sua localização via botão do Telegram e calcula
  a rota até o destino, com geocoding para endereços livres.
- **Multi-provider LLM**: Anthropic (default), OpenAI, Gemini.
  Troca em runtime via `/provider`. Preferência persistida por usuário.
- **Acesso restrito por senha** (`ACCESS_PASSWORD`); isolamento por
  `telegram_user_id`.

## Comandos

### Trânsito casa↔trabalho
| Comando | Descrição |
|---|---|
| `/transito_agora casa` \| `/transito_agora trabalho` | Força consulta agora (mostra rota preferida + alternativa) |
| `/transito_on` / `/transito_off` | Assina/desassina o digest diário (seg-sex) |
| `/transito_at HH:MM` | Muda o horário do digest (sem arg volta ao default) |
| `/transito_reset` | Zera marca de envio de hoje (útil pra forçar reenvio) |
| `/transito_alerta_on` / `/transito_alerta_off` | Liga/desliga alerta proativo (default ligado) |

### Medidas Provisórias
| Comando | Descrição |
|---|---|
| `/congresso_agora` | Força resumo da semana agora |
| `/congresso_on` / `/congresso_off` | Assina/desassina o digest semanal (segunda) |
| `/congresso_at HH:MM` | Muda o horário do digest |
| `/congresso_reset` | Zera marca de envio da semana |

### Rota com sua localização
| Comando | Descrição |
|---|---|
| `/rota casa` \| `/rota trabalho` | Atalho — usa `HOME_COORDS` / `WORK_COORDS` como destino |
| `/rota <endereço>` | Geocoda o endereço (Google Geocoding API) e calcula a rota a partir da sua localização |

Em todos os casos o bot envia um teclado com botão **📍 Enviar localização**;
basta tocar uma vez. A localização não é persistida.

### Tarefas e lembretes
| Comando | Descrição |
|---|---|
| `/nova <texto>` | Cria tarefa |
| `/tarefas` | Lista tarefas abertas |
| `/feito <id>` | Marca tarefa como concluída |
| `/lembrar <texto> em 2h \| amanhã 09:00 \| sexta 18h` | Cria lembrete com horário |
| `/lembretes` | Lista lembretes pendentes |
| `/apagar_lembrete <id>` | Apaga um lembrete pendente |

> Você também pode pedir essas ações em texto/voz livre — o LLM aciona a tool
> certa. Ex: *"me lembre de pagar o boleto amanhã 10h"*, *"quais minhas
> tarefas?"*, *"apaga o lembrete 5"*, *"qual a previsão pra hoje?"*.

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

Mande um áudio para o bot. Ele transcreve via **Gemini multimodal**
(default `gemini-2.5-flash`, ajustável via `VOICE_STT_MODEL` — ex.
`gemini-2.5-flash-lite` pra latência menor) e roteia:

- Se a transcrição começa com `/` → executa o comando.
- Caso contrário → trata como chat livre, com o mesmo contexto de 30 min em RAM.

Três formas de invocar comandos por voz:

1. **Slash literal**: *"barra trânsito agora casa"* → `/transito_agora casa`.
2. **Fala natural — trânsito**: *"trânsito para casa"*, *"trânsito pra casa"*,
   *"trânsito para o trabalho"*, *"trânsito trabalho"* etc. → `/transito_agora …`.
3. **Fala natural — congresso**: *"pauta de MP do congresso agora"* (e
   variações próximas) → `/congresso_agora`.
4. **Fala natural — rota**: *"rota para casa"*, *"como chegar em Avenida
   Paulista 1000"*, *"me leva pro shopping X"* → `/rota …` (bot pede sua
   localização via botão).

Em conversa casual (ex: *"falei sobre o trânsito ontem com o motorista"*) a
transcrição é literal e cai no chat livre. Como o chat tem tool use,
pedidos de tarefa/lembrete/clima por voz funcionam naturalmente: *"nova
tarefa comprar pão"*, *"me lembre de pagar boleto amanhã 10h"*,
*"qual a previsão pra hoje?"* — o LLM aciona a tool e confirma.

O bot ecoa a transcrição antes da resposta (transparência). Áudios acima de
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

A `GOOGLE_MAPS_API_KEY` é usada para **Directions API** (digest diário e
`/transito_agora`) e também para **Geocoding API (New)** (`/rota <endereço>`).
Habilite ambas no Google Cloud Console; sem Geocoding, atalhos
(`/rota casa`, `/rota trabalho`) ainda funcionam mas endereços livres
retornam erro.

> O serviço de geocoding usa o endpoint v4beta
> (`geocode.googleapis.com/v4beta/geocode/address/...`), que é o SKU
> servido pela **Geocoding API (New)** — única versão disponível para
> projetos novos do Google Cloud. O endpoint legado
> `maps.googleapis.com/maps/api/geocoding/json` retorna 404 nesses
> projetos.

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
  `traffic_subscribed`, `traffic_hour/minute`, `last_traffic_digest_at`,
  `traffic_alert_enabled`, `last_traffic_alert_at` + os equivalentes para
  `congress_*`.
- **`tasks`**: `id`, `user_id`, `text`, `done`, `created_at`, `done_at`.
- **`reminders`**: `id`, `user_id`, `text`, `due_at` (UTC), `sent`, `sent_at`.
- **`traffic_samples`**: `id`, `user_id`, `weekday` (0-6), `hour` (0-23),
  `sampled_at`, `duration_seconds`. Coletado a cada 10 min na janela de
  monitoramento; usado pra calcular mediana rolling 7 dias do baseline de
  trânsito. Purge automática de samples >30 dias às 03:00 BRT.

Idempotência dos digests é via os timestamps `last_*_digest_at` no User
(não duplica no mesmo dia/semana). Migrações de coluna são aplicadas
inline em `init_db` (ALTER TABLE guardado por PRAGMA — SQLite).

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
│   ├── route.py                  # /rota + F.location + botão cancelar
│   ├── voice.py                  # F.voice|F.audio → transcribe + dispatch
│   └── chat.py                   # catch-all texto livre
├── services/
│   ├── llm/                      # base + factory + anthropic/openai/gemini
│   │                             # (chat_with_tools implementado nos 3)
│   ├── tools.py                  # registry de tools acionáveis pelo LLM
│   ├── traffic.py, weather.py    # Google Directions + Open-Meteo
│   ├── traffic_baseline.py       # mediana rolling + alerta proativo
│   ├── geocoding.py              # Google Geocoding (endereço → coords)
│   ├── congress.py               # Scraper MP
│   ├── tasks.py, reminders.py
│   ├── chat_memory.py            # in-memory TTL 30min
│   ├── route_pending.py          # /rota aguardando localização (in-memory)
│   ├── voice.py                  # STT via Gemini multimodal
│   └── scheduler.py              # loop async (trânsito, MP, watch, lembretes, purge)
└── utils/
    └── timez.py
scripts/
├── setup-swap.sh
└── backup.sh
```
