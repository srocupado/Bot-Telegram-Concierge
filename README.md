# Bot Telegram Concierge

Assistente pessoal de atividades diárias no Telegram. Combina **agente
proativo opt-in** (avisa por conta própria), digests de **trânsito + clima**
(rota casa↔trabalho), **Medidas Provisórias** (pauta do Congresso + publicação
no Diário Oficial com nota técnica gerada por IA), **gerenciador financeiro**
(Firestore), **academia**, **lista de compras**, **tarefas/lembretes** e
**chat livre com LLM** multi-provider (Anthropic/OpenAI/Gemini) com troca de
provider/modelo em runtime, além de **voz** (STT) e **imagens** (visão).

## Funcionalidades

- **Agente proativo (opt-in)**: roda em janelas do dia (default 7/13/19h BRT)
  e avisa por conta própria — vencimentos chegando (lembretes não recorrentes
  + fatura do cartão), **briefing matinal** (consolida lembretes do dia,
  trânsito casa→trabalho e MPs do dia anterior) e **nudges** por inatividade
  (treino/finanças/lista parados). Gatilhos 100% determinísticos; baixo ruído
  (1 mensagem por janela, dedup, cooldown, silêncio quando não há nada).
  Liga com `/proativo_on`.
- **Trânsito diário** (seg–sex): rota casa↔trabalho com tempo real (Google
  Directions API) + previsão do tempo (Open-Meteo). Suporta rota preferida via
  URL do Google Maps com waypoints. **Alerta proativo** quando o tempo está
  ≥30% acima do habitual.
- **Medidas Provisórias — pauta do Congresso** (digest semanal): agenda do
  Congresso Nacional via web scraping, filtrando MPs/CMMPV. Também por chat:
  *"como está a pauta do congresso?"* → mesma saída do `/congresso_agora`.
- **Medidas Provisórias — publicação no Diário Oficial (DOU)**: autentica no
  **Inlabs**, baixa os ZIPs do DOU (DO1E + DO1), extrai as MPs do dia e entrega
  um aviso (número, ementa, prazos, link) + **nota técnica em DOCX** no padrão
  institucional, gerada por IA em duas fases (pesquisa de contexto com busca
  web + redação estruturada). A detecção de MP nova está integrada ao **agente
  proativo** (avisa nas janelas do dia e cobre a véspera no briefing); a nota
  completa vem sob demanda (`/mp_dou_agora` ou botão "gerar nota").
- **Gerenciador financeiro** (Firestore, integra com o app externo
  *gerenciador-financeiro*): lança compras no cartão, movimentos no banco,
  aportes em Tesouro Direto e **operações de compra/venda em ações, FIIs,
  ETFs, Renda Fixa, fundos e cripto** (`state.investments.assets`); consulta
  lançamentos; **saldo bancário + entradas/saídas do mês + total em
  investimentos** num só lugar; análise de gastos por categoria/período.
  Por voz/texto: *"qual meu saldo?"*, *"lança 250 no cartão, mercado, hoje"*,
  *"comprei 10 HGLG11 a 168,50"*, *"como tá meu cartão esse mês?"*,
  *"lista meus investimentos"*.
- **Academia**: registra treino por voz/texto (*"hoje malhei peito e fiz
  cardio"*), resumo semanal (dom→sáb), correção/apagamento do dia. Categorias
  peito/costas/pernas/cardio normalizadas.
- **Lista de compras** persistente: *"acabou o sal"*, *"compra arroz e
  detergente"*, *"o que tem na lista?"*, *"comprei o sal"* (☑️), *"limpa o que
  comprei"*. Zerar a lista pede confirmação por botão.
- **Tarefas e lembretes**: `/nova`, `/tarefas`, `/feito`; lembretes em
  linguagem natural (`/lembrar tomar remédio em 30min`), **recorrentes**
  (*"todo domingo 20h..."*), e **ações agendadas** (trânsito/congresso/clima/
  prompt livre disparam no horário).
- **Desfazer**: *"desfaz"* / *"errei, cancela"* desfaz o último item criado
  pelo bot (lançamento/tarefa/lembrete/compra). Encadeia.
- **Chat livre com LLM agente**: texto, voz e imagem viram conversa com
  contexto curto (TTL 30 min, em RAM) e **tool use** — o LLM aciona ferramentas
  sem você precisar lembrar slash commands.
- **Busca web nativa** no Anthropic (`web_search`) e Gemini (`google_search`),
  citando fontes. (OpenAI não tem busca nativa nessa versão.)
- **Voz (STT)**: áudio transcrito via **Gemini multimodal** (default
  `gemini-3.5-flash`) ou **OpenAI Whisper/gpt-4o-transcribe** — selecionável por
  usuário com `/voice gemini|openai`. Aceita OGG/Opus nativo sem ffmpeg. Quando
  a cadeia Gemini inteira falha (503/timeout/etc), o bot **cai automaticamente
  no Whisper** se `OPENAI_API_KEY` estiver configurada — o áudio sempre vira
  texto.
- **Imagens (visão)**: mande uma foto (com/sem caption) — o bot analisa via LLM
  agente (OCR de recibo/boleto, leitura de placa, resumo de screenshot…), e
  pode acionar tools. Provider de visão configurável (`/provider_visao`).
- **Multi-provider LLM**: Anthropic, OpenAI, Gemini (com modelos 2.5 e 3.x).
  Troca em runtime via `/provider`; preferência persistida por usuário.
  Prompt caching no Anthropic pra reduzir custo.
- **Acesso restrito por senha** (`ACCESS_PASSWORD`); isolamento por
  `telegram_user_id`. **Multi-usuário** simultâneo (família, casal): SQLite
  configurado em modo WAL + `busy_timeout`, cada user tem seu próprio
  provider/modelo/STT/`firebase_uid`/lembretes/lista/financeiro.

## Comandos

### Agente proativo
| Comando | Descrição |
|---|---|
| `/proativo_on` / `/proativo_off` | Liga/desliga os avisos automáticos |
| `/proativo` | Status e janelas |
| `/proativo_agora [briefing]` | Força a checagem agora (ignora dedup; `briefing` testa o resumo matinal) |

### Trânsito casa↔trabalho
| Comando | Descrição |
|---|---|
| `/transito_agora casa` \| `/transito_agora trabalho` | Força consulta agora (rota preferida + alternativa) |
| `/transito_on` / `/transito_off` | Assina/desassina o digest diário (seg-sex) |
| `/transito_at HH:MM` | Muda o horário do digest |
| `/transito_reset` | Zera marca de envio de hoje |
| `/transito_alerta_on` / `/transito_alerta_off` | Liga/desliga alerta proativo |

### Medidas Provisórias — pauta do Congresso
| Comando | Descrição |
|---|---|
| `/congresso_agora` | Força resumo da semana agora |
| `/congresso_on` / `/congresso_off` | Assina/desassina o digest semanal (segunda) |
| `/congresso_at HH:MM` | Muda o horário do digest |
| `/congresso_reset` | Zera marca de envio da semana |

### Medidas Provisórias — publicação no Diário Oficial (DOU)
| Comando | Descrição |
|---|---|
| `/mp_dou_on` / `/mp_dou_off` | Assina/desassina o acompanhamento de MPs novas no DOU (alimenta o agente proativo) |
| `/mp_dou_agora [AAAA-MM-DD]` | Busca agora (data opcional); entrega a nota técnica + DOCX |

> Por voz/texto: *"saiu MP nova hoje?"* lista número + ementa. A nota completa
> + DOCX vêm por `/mp_dou_agora` ou pelo botão "gerar nota" do aviso proativo.

### Gerenciador financeiro
| Comando | Descrição |
|---|---|
| `/financeiro_setup` | Configura a service account (envie o JSON) e o UID do Firebase |

> O resto é por voz/texto. Saldo: *"qual meu saldo?"*, *"quanto sobrou esse
> mês?"* → devolve o cabeçalho **Visão Geral** do app (saldo bancário atual,
> entradas/saídas do mês, total em investimentos). Lançamentos: *"paguei conta
> de luz 180"*, *"recebi 5 mil de salário"*, *"aportei 1000 no Tesouro IPCA+
> 2035"*. **Investimentos** (ações/FIIs/ETFs/RF/fundos/cripto): *"comprei 10
> HGLG11 a 168,50"*, *"vendi 50 ITUB4 a 32,10"*, *"lista meus investimentos"*
> (mostra Tesouro + carteira agrupados por classe, com posição, PM, P&L,
> proventos — réplica do painel Visão geral). Análise: *"como tá meu cartão?"*,
> *"quanto gastei com alimentação em maio?"*, *"compara maio e junho"*.

### Rota com sua localização
| Comando | Descrição |
|---|---|
| `/rota casa` \| `/rota trabalho` | Atalho — usa `HOME_COORDS` / `WORK_COORDS` |
| `/rota <endereço>` | Geocoda o endereço e calcula a rota a partir da sua localização |

O bot envia um botão **📍 Enviar localização** (não persiste a localização).

### Tarefas e lembretes
| Comando | Descrição |
|---|---|
| `/nova <texto>` | Cria tarefa |
| `/tarefas` | Lista tarefas abertas |
| `/feito <id>` | Marca tarefa como concluída |
| `/lembrar <texto> em 2h \| amanhã 09:00` | Cria lembrete com horário |
| `/lembretes` | Lista lembretes pendentes |
| `/apagar_lembrete <id>` | Apaga um lembrete pendente |
| `/agendar_comando <tipo> [args] <quando>` | Agenda uma ação automática (`transito_casa`/`transito_trabalho`/`congresso`/`clima`/`chat`) |

> Também por texto/voz livre: *"me lembre de pagar o boleto amanhã 10h"*,
> *"todo domingo 20h me manda o resumo da semana"*, *"apaga o lembrete 5"*.

### LLM e utilitários
| Comando | Descrição |
|---|---|
| `/ping` | Testa o LLM atual (provider e modelo) |
| `/provider anthropic\|openai\|gemini` | Troca o LLM. No Gemini escolhe o modelo: `/provider gemini 3.5` \| `3.1-pro` \| `3.1-lite` \| `pro` \| `flash` |
| `/provider_visao anthropic\|openai\|gemini\|auto` | Provider só para imagens (auto = segue o `/provider`) |
| `/voice gemini\|openai` | Provider da transcrição de voz (Gemini multimodal vs Whisper) |
| `/reset` | Limpa o contexto da conversa livre |
| `/start` | Início + fluxo de senha |
| `/help` | Lista todos os comandos |

Mensagens de texto sem `/` são chat livre com o LLM atual.

### Mensagens de voz

Mande um áudio. O bot transcreve e roteia:

- Transcrição começa com `/` → executa o comando.
- Caso contrário → chat livre, com o mesmo contexto de 30 min em RAM.

**Provider de STT** (`/voice` ou `VOICE_STT_PROVIDER`):

- `gemini` (default, `gemini-3.5-flash`): multimodal; além de transcrever, faz
  a **conversão voz→/comando** (atalhos abaixo). Em 503/sobrecarga, tenta
  fallback de modelo automaticamente (`3.1-flash-lite` → `3.1-pro`). Se TODA a
  cadeia falhar e `OPENAI_API_KEY` estiver configurada, **cai automaticamente
  no Whisper** como último recurso.
- `openai` (Whisper / `gpt-4o-transcribe`): transcrição **literal** e estável;
  **não** dispara os atalhos de slash por voz (cai sempre no chat livre).

Atalhos de comando por voz (provider `gemini`):

1. **Slash literal**: *"barra trânsito agora casa"* → `/transito_agora casa`.
2. **Trânsito**: *"trânsito para casa"*, *"trânsito pro trabalho"* → `/transito_agora …`.
3. **Congresso**: *"pauta de MP do congresso agora"* → `/congresso_agora`.
4. **Rota**: *"rota para casa"*, *"como chegar em Avenida Paulista 1000"* → `/rota …`.
5. **Busca**: *"busca X"*, *"pesquisa X"*, *"google X"* → `/buscar …`.

> Pedidos de trânsito casa↔trabalho no chat livre (voz `openai` ou texto)
> devolvem a **mesma mensagem** do `/transito_agora` (2 rotas, tempo atual vs
> típico, link do mapa) — sem paráfrase do LLM. Idem congresso e consulta de MP.

O bot ecoa a transcrição antes da resposta. Áudios acima de `VOICE_MAX_SECONDS`
(default 120s) são rejeitados. Para desativar: `VOICE_ENABLED=false`.

## Stack

- Python 3.12, aiogram 3 (long polling)
- SQLAlchemy 2 async + aiosqlite (SQLite em volume; **modo WAL** +
  `busy_timeout=5000` + `synchronous=NORMAL` aplicados em `connect`)
- httpx async, BeautifulSoup4
- pydantic-settings, python-json-logger
- SDKs: `anthropic`, `openai`, `google-genai` (1.x — chat com tools, visão,
  STT de voz e Google Search grounding). O `google-genai` é HTTP (não usa o
  antigo `google-generativeai`/gRPC, que pendurava em ARM/docker).
- `firebase-admin` (gerenciador financeiro / Firestore)
- `python-docx` (nota técnica das MPs no template institucional)
- `dateparser` (lembretes em português)

## Configuração

```bash
cp .env.example .env
# preencher: BOT_TOKEN, ACCESS_PASSWORD, ANTHROPIC_API_KEY / OPENAI_API_KEY /
#            GEMINI_API_KEY, GOOGLE_MAPS_API_KEY, HOME_COORDS, WORK_COORDS,
#            ROUTE_GOOGLE_MAPS_URL (opcional), TIMEZONE
```

A previsão do tempo (Open-Meteo) usa `HOME_COORDS`, sem chave própria.

### Agente proativo

```bash
PROACTIVE_ENABLED=true          # gate global (o usuário ainda precisa de /proativo_on)
PROACTIVE_HOURS=7,13,19         # janelas BRT (CSV)
PROACTIVE_BRIEFING_HOUR=7       # hora do briefing matinal
PROACTIVE_LOOKAHEAD_HOURS=48    # antecedência de vencimentos (em toda janela até vencer)
PROACTIVE_USE_LLM=false         # false = texto determinístico (barato, sem alucinar)
```

### Voz (STT)

```bash
VOICE_ENABLED=true
VOICE_MAX_SECONDS=120
VOICE_STT_MODEL=gemini-3.5-flash         # geração nova, estável
VOICE_STT_PROVIDER=gemini                # gemini | openai
VOICE_STT_OPENAI_MODEL=gpt-4o-mini-transcribe
```

### Monitor de MPs no DOU

```bash
# credencial do Inlabs (cadastro gratuito em inlabs.in.gov.br/acessar.php)
INLABS_EMAIL=...
INLABS_PASSWORD=...
DOU_MP_HOUR=18                  # horário BRT mencionado nas mensagens
DOU_MP_PROVIDER=gemini          # gemini (mais barato) | anthropic
DOU_MP_GEMINI_MODEL=gemini-3.5-flash         # geração nova, estável
DOU_MP_GEMINI_MODEL_FALLBACK=gemini-3.1-flash-lite  # rede pra 503/JSON truncado
DOU_MP_WEB_RESEARCH=true        # pesquisa de contexto via busca web
```

Reusa `GEMINI_API_KEY` / `ANTHROPIC_API_KEY` conforme o provider. A detecção
de MP nova roda dentro do **agente proativo** (assine com `/mp_dou_on`); a nota
técnica completa vem por `/mp_dou_agora` ou pelo botão do aviso. A geração é
feita com `thinking_budget=0` (o thinking automático consumia o orçamento de
tokens e truncava o JSON estruturado) e o fallback dispara também em
`JSONDecodeError`/timeout, não só em quota.

### Gerenciador financeiro (Firestore)

Configure em runtime com `/financeiro_setup` (envie o JSON da service account
do Firebase e informe o UID). As credenciais ficam no SQLite (`kv_settings`),
não no `.env`. Integra com o app externo *gerenciador-financeiro* — usa as
mesmas chaves do `state` no Firestore (`bankTransactions`, `cardEntries`,
`treasuryHoldings`, `investments.assets`) e nunca altera o schema. Cálculos de
saldo, projeção do Tesouro pra hoje e métricas dos ativos (PM/posição/P&L)
replicam as fórmulas do `src/store.jsx`/`src/investimentos.jsx` do app.

### Google Maps

A `GOOGLE_MAPS_API_KEY` é usada para **Directions API** (digest e
`/transito_agora`) e **Geocoding API (New)** (`/rota <endereço>`, endpoint
v4beta). Habilite ambas no Google Cloud. Sem Geocoding, os atalhos
(`/rota casa`/`trabalho`) ainda funcionam.

### Rota preferida (`ROUTE_GOOGLE_MAPS_URL`)

Trace a rota habitual no Google Maps **com paradas intermediárias** →
Compartilhar → Copiar link → cole em `ROUTE_GOOGLE_MAPS_URL`. O bot extrai os
waypoints e compara com a melhor alternativa do Google em tempo real.

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

### Swap de memória

```bash
sudo ./scripts/setup-swap.sh           # 1 GB padrão
sudo ./scripts/setup-swap.sh 2         # 2 GB
```

Idempotente: persiste em `/etc/fstab`, ajusta `vm.swappiness=10`.

### Backup do SQLite

`./scripts/backup.sh` gera `concierge-YYYY-MM-DD-HHMM.tgz` no diretório
informado (default `/mnt/kodak/Bot-Concierge`; passe outro como `$1` se
preferir) e mantém 14 dias. Como o engine roda em modo WAL, o script usa a
**Online Backup API** do SQLite (`sqlite3.backup()` via `docker compose exec`)
em vez de copiar o arquivo `.db` direto — assim a cópia é transacionalmente
consistente sem precisar parar o container, e não fica refém de
`-wal`/`-shm` desatualizados.

Agendar via cron:

```bash
sudo crontab -e
0 3 * * * /home/USER/apps/Bot-Telegram-Concierge/scripts/backup.sh
```

Para restaurar: descompactar o `.tgz`, renomear o `.concierge-backup-tmp.db`
para `concierge.db` e colocar dentro de `./data/`.

## Modelo de dados

- **`users`**: `id` (telegram_id), `chat_id`, `username`, `first_name`,
  `is_authorized`, `provider`, `gemini_model` (modelo Gemini por usuário),
  `vision_provider`, `voice_stt_provider`, `timezone`; subscriptions de
  `traffic_*`, `congress_*`, `dou_mp_subscribed`, `proactive_enabled`; e a
  integração financeira (`firebase_uid`).
- **`tasks`**: `id`, `user_id`, `text`, `done`, `created_at`, `done_at`.
- **`reminders`**: `id`, `user_id`, `text`, `due_at` (UTC), `sent`, `sent_at`,
  `command_kind`/`command_args` (ações agendadas), `recurrence` (recorrência).
- **`traffic_samples`**: baseline rolling 7 dias (mediana por `weekday`/`hour`)
  pro alerta proativo. Purge >30 dias.
- **`dou_seen_mps`**: dedup do monitor de MPs no DOU.
- **`proactive_notices`**: dedup do agente proativo (`kind`, `key`, `sent_at`).
  Purge >90 dias.
- **`shopping_items`**: lista de compras (`text`, `quantity`, `checked`).
- **`workout_logs`**: registros de academia da semana corrente.

Idempotência dos digests é via timestamps `last_*_digest_at` no User. Migrações
de coluna são aplicadas inline em `init_db` (ALTER TABLE guardado por PRAGMA).

## Estrutura

```
bot/
├── __main__.py, runner.py        # entrypoint, dispatcher, scheduler
├── config.py, logging_setup.py
├── db/
│   └── base.py, models.py, session.py
├── middlewares/
│   └── auth.py, db.py
├── handlers/
│   ├── start.py, ping.py, provider.py, reset.py, search.py
│   ├── traffic.py                # /transito_*
│   ├── congress.py               # /congresso_*
│   ├── dou_mp.py                 # /mp_dou_* + botão nota técnica
│   ├── proactive.py              # /proativo_*
│   ├── financeiro.py             # /financeiro_setup + captura do JSON
│   ├── shopping.py               # confirmação de limpeza da lista (botões)
│   ├── tasks.py, reminders.py, reminder_callbacks.py
│   ├── route.py                  # /rota + F.location
│   ├── voice.py                  # F.voice|F.audio → transcribe + dispatch
│   ├── photo.py                  # F.photo → visão multimodal
│   ├── document.py               # F.document (PDF) → multimodal
│   └── chat.py                   # catch-all texto livre
├── services/
│   ├── llm/                      # base + factory + anthropic/openai/gemini
│   ├── tools.py                  # registry de tools acionáveis pelo LLM
│   ├── scheduled_actions.py      # dispatch de ações agendadas
│   ├── traffic.py, weather.py, traffic_baseline.py, geocoding.py
│   ├── congress.py               # scraper pauta do Congresso
│   ├── dou_monitor.py            # Inlabs/DOU + nota técnica + DOCX
│   ├── proactive.py              # agente proativo (coletores + orquestrador)
│   ├── financeiro.py             # gerenciador financeiro (Firestore)
│   ├── finance_guard.py          # blindagem anti-alucinação de lançamento
│   ├── workouts.py               # academia
│   ├── shopping.py               # lista de compras
│   ├── tasks.py, reminders.py
│   ├── chat_memory.py            # in-memory TTL 30min
│   ├── route_pending.py
│   ├── voice.py                  # STT (Gemini multimodal / OpenAI Whisper)
│   └── scheduler.py              # loop async (trânsito, proativo, watch, lembretes, purge)
└── assets/
    └── nota_template.docx        # template da nota técnica
scripts/
├── setup-swap.sh
└── backup.sh
```
