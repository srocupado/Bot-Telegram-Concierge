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
  + fatura do cartão), **briefing matinal** (previsão do tempo do dia,
  lembretes do dia, trânsito casa→trabalho e MPs do dia anterior), **nudges**
  por inatividade
  (treino/finanças/lista parados) e **revisão da carteira** na última janela do
  dia (cotação de mercado dos ativos B3 via brapi.dev: investido vs mercado +
  P&L, atualizando o `currentPrice` no Firestore). Gatilhos 100% determinísticos; baixo ruído
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
- **Viagens** (SerpAPI): busca de **passagens** (Google Flights) e **hotéis**
  (Google Hotels) por texto/voz — *"voo BSB→GRU dia 20/07"*, *"hotel em
  Florianópolis de 10 a 14/01"*. Supports voos ida e volta, classe de viagem
  (econômica/executiva/etc.) e múltiplos passageiros. **Watches diários**: o
  bot verifica o preço 1x/dia (8h BRT por padrão) e avisa quando cair abaixo do
  mínimo histórico ou de um teto que você definir — *"monitora esse voo e me
  avisa se ficar abaixo de 800"*. Gestão por texto: *"lista meus watches"*,
  *"cancela o watch 3"*. Requer `SERPAPI_KEY`.
- **Memória persistente de fatos**: o LLM salva automaticamente — ou você pede
  explicitamente — qualquer informação pessoal que queira que o bot lembre entre
  sessões: preferências, nomes de família, alergias, hábitos, configurações
  favoritas etc. Sobrevive a restart do container (gravado no SQLite). Exemplos:
  *"lembra que minha esposa se chama Dani"*, *"meu editor preferido é nvim"*,
  *"tenho alergia a amendoim"*. Para consultar: *"o que você sabe sobre mim?"*.
  Para esquecer: *"esquece meu editor preferido"*.
- **Desfazer**: *"desfaz"* / *"errei, cancela"* desfaz o último item criado
  pelo bot (lançamento/tarefa/lembrete/compra). Encadeia.
- **Chat livre com LLM agente**: texto, voz e imagem viram conversa com
  contexto curto (TTL 30 min, em RAM) e **tool use** — o LLM aciona ferramentas
  sem você precisar lembrar slash commands.
- **Busca web nativa** no Anthropic (`web_search`) e Gemini (`google_search`),
  citando fontes. (OpenAI não tem busca nativa nessa versão.)
- **Busca web com leitura de página** (tool `buscar_web` + `/buscar`): busca
  **e lê o corpo renderizado** da página — funciona pra dados que só existem
  dentro da página e mudam com o tempo (horários de sessão de cinema,
  funcionamento de loja, preços, cardápios). Backend **SearXNG+Jina**
  (self-hosted, custo zero) como primário e **Firecrawl** como fallback.
  Ver [Busca web](#busca-web-buscar_web--buscar).
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
  Prompt caching no Anthropic pra reduzir custo; no Gemini o caching implícito
  (2.5/3.x) aproveita o mesmo prefixo estável. Para isso o prefixo `system +
  tools` é mantido fixo — data/hora e o resumo de memória entram na mensagem do
  usuário (via `inject_context`), não no system prompt, pra não furar o cache a
  cada minuto. O uso de tokens (incl. `cached`) é logado nos dois providers.
- **Acesso restrito por senha** (`ACCESS_PASSWORD`); isolamento por
  `telegram_user_id`. **Multi-usuário** simultâneo (família, casal): SQLite
  configurado em modo WAL + `busy_timeout`, cada user tem seu próprio
  provider/modelo/STT/`firebase_uid`/lembretes/lista/financeiro.

## Comandos

### Agente de execução de código (owner-only)

Estilo Claude Code dentro do bot: escreve, **executa** e itera código num
workspace isolado (`./workspace`), pesquisa documentação na web
(WebSearch/WebFetch nativos) e entrega os arquivos prontos pelo Telegram.
Só o usuário com `OWNER_TELEGRAM_ID` vê/usa — pros demais o recurso não existe.

| Comando | Descrição |
|---|---|
| `/agente <tarefa>` | Inicia a tarefa em background; progresso numa mensagem editada |
| texto/voz livre | `"constrói um app que..."` aciona via tool `executar_agente`; por voz com slash: `"barra agente ..."` |
| reply em TTL | **Responder (reply)** à mensagem de entrega (resumo ou arquivos) **continua a mesma sessão** (resume); texto solto segue como chat normal |
| `/agente_fim` | Encerra a janela de continuação antes do TTL |
| `/agente_parar` | Interrompe a tarefa em andamento |
| `/agente_status` | Rodando há quanto tempo / sessão ativa |
| `/agente_config` | Ajustes finos em runtime, sem restart: `modelo opus\|sonnet\|haiku`, `timeout 1800`, `turnos 20`, `custo 5`, `ttl 60`, `padrao` (volta ao `.env`) |
| agendado (cron) | *"todo dia útil 7h, roda o agente pra..."* — execução recorrente via scheduler; ver [Tarefas e lembretes](#tarefas-e-lembretes) |

Guardrails: 1 tarefa por vez; env do agente com **whitelist** (nunca vê
`BOT_TOKEN`/senhas); deny rules de leitura/escrita fora do workspace
(`/app/data`, `/app/bot`) + hook que bloqueia `Bash` nesses paths;
`max_turns`, timeout e **teto de custo por tarefa** (`max_budget_usd`) —
e, pra execuções agendadas, teto **diário** opcional
(`AGENT_CRON_DAILY_BUDGET_USD`); `mem_limit: 2g` no compose protege o host.
Com `AGENT_GITHUB_TOKEN` (fine-grained PAT restrito aos repos permitidos) o
agente também clona privados, commita, faz push e abre PRs via `git`/`gh`.

#### Arquivos anexados (uploads)

Owner-only: anexe um documento no chat e ele é salvo em
`./workspace/uploads/` (persiste no host; máx 20 MB — limite de download da
Bot API). Caption `guarda como <nome.ext>` renomeia; **PDF sem caption**
continua indo pra análise multimodal (caption `guarda` força salvar).
`/arquivos` lista, `/arquivos baixar <nome>` manda o arquivo de volta no
chat (≤ 50 MB), `/arquivos apagar <nome>` remove — em linguagem natural
também: *"que arquivos você tem salvos?"* (tool `listar_arquivos`). Como a
pasta fica no workspace do agente, o fluxo natural é anexar a planilha hoje
e amanhã pedir */agente pega o uploads/planilha.xlsx e cruza com…*

#### SSH / rede local (ex.: backup de pasta de outra máquina)

O container está na rede bridge do Docker com saída pra LAN, e a imagem traz
`openssh-client` e `rsync`. Autenticação **somente por chave** (nunca mande
senhas no chat: o prompt vai pro histórico do Telegram e pra API da
Anthropic, e os comandos executados aparecem na mensagem de progresso).
Setup único, no host do bot:

```bash
mkdir -p workspace/.ssh
ssh-keygen -t ed25519 -f workspace/.ssh/id_ed25519 -N ""
cat workspace/.ssh/id_ed25519.pub  # → authorized_keys do user remoto
```

Na máquina remota, use um user **dedicado e sem sudo**; ideal: prefixe a
chave no `authorized_keys` com `command="rrsync -ro /pasta"` pra travá-la em
rsync somente-leitura daquela pasta — via SSH o confinamento do workspace
não se aplica, a chave define o alcance do agente lá. A chave fica em
`./workspace/.ssh` (montada no container, visível pro agente, **não** é
entregue no Telegram por já existir antes da tarefa). Aí é só pedir:

> /agente backup via rsync da pasta /home/fulano/docs da máquina
> 192.168.1.50, user backup, chave .ssh/id_ed25519, pra backups/\<data\>

Backups ficam em `./workspace` no host; pro Telegram só vão arquivos
≤ 50 MB. Recorrente sem custo de API: peça ao agente um `backup.sh` testado
uma vez e agende como shell (*"todo dia 3h roda @silencioso bash
/app/workspace/backup.sh"*).

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

### Viagens (voos e hotéis)

> Tudo por texto/voz livre — não há slash commands dedicados.

**Busca pontual:**
- *"voo de BSB pra GRU no dia 20/07"* → melhor oferta (companhia, horários, preço, link)
- *"voo ida e volta BSB→GRU, saindo 20/07 voltando 27/07"*
- *"hotel em Florianópolis de 10 a 14/01 para 2 pessoas"*
- *"passagem executiva GRU→JFK para 2 adultos em 15/08"*

**Watches diários (monitor de preço):**

| Intenção | Exemplo |
|---|---|
| Criar watch de voo | *"monitora passagem BSB→GRU 20/07 e me avisa se cair"* |
| Watch com teto | *"cria alerta se o voo ficar abaixo de R$ 800"* |
| Criar watch de hotel | *"monitora hotel em SP de 10 a 12/07, teto R$ 300/noite"* |
| Listar watches | *"lista meus watches de viagem"* |
| Cancelar watch | *"cancela o watch 3"* |

O bot verifica 1x/dia (padrão 8h BRT). Sem teto definido, avisa sempre que bater um novo mínimo histórico. Requer `SERPAPI_KEY`.

### Memória persistente de fatos

> Tudo por texto/voz — sem slash commands.

O LLM salva fatos automaticamente quando você conta algo sobre si, ou você pede explicitamente. Os fatos sobrevivem a restart do container e ficam disponíveis em qualquer sessão futura.

| Intenção | Exemplo |
|---|---|
| Salvar fato | *"lembra que minha esposa se chama Dani"* |
| Salvar preferência | *"meu editor preferido é nvim"* |
| Salvar dado de saúde | *"tenho alergia a amendoim"* |
| Consultar tudo | *"o que você sabe sobre mim?"* |
| Consultar um fato | *"como eu disse que prefiro café?"* |
| Esquecer um fato | *"esquece meu editor preferido"* |

Chaves são snake_case curto (ex: `esposa_nome`, `alergia`, `editor_preferido`). Um fato com a mesma chave sobrescreve o anterior.

### Memória de conversa (contexto persistente)

Três camadas, sem nenhuma config nova no `.env` (defaults fixos no código):

1. **Contexto que sobrevive a restart** — toda mensagem do chat livre é
   gravada em `chat_log` (write-through). No boot o bot re-hidrata o que
   ainda está no TTL de 30 min: deploy/restart deixa de "apagar" a conversa.
2. **Resumo rolante (longo prazo)** — quando turnos saem do contexto
   (estouro de 10 turnos ou TTL), o **mesmo provider/modelo do seu
   `/provider`** compacta o que é duradouro num resumo ≤ ~1.500 chars por
   usuário, injetado no system prompt (nenhum modelo hardcoded). *"Aquele
   plano que montamos ontem"* passa a funcionar dias depois.
3. **Busca no histórico** — tool `buscar_historico` com FTS5 do SQLite
   (fallback `LIKE`): *"o que eu te falei sobre o orçamento da reforma?"*
   responde com trechos datados de conversas antigas.

| Comando | Descrição |
|---|---|
| `/reset` | Limpa o contexto atual (RAM + janela re-hidratável) |
| `/reset_memoria` | Apaga o resumo de longo prazo (se reconstrói depois) |
| `/reset_memoria tudo` | Apaga resumo **e** todo o histórico pesquisável |

Retenção do histórico: 90 dias (purge diário às 3h). Privacidade: as
conversas ficam em texto puro no `concierge.db` local — o backup do banco
passa a conter conversas.

### Tarefas e lembretes
| Comando | Descrição |
|---|---|
| `/nova <texto>` | Cria tarefa |
| `/tarefas` | Lista tarefas abertas |
| `/feito <id>` | Marca tarefa como concluída |
| `/lembrar <texto> em 2h \| amanhã 09:00` | Cria lembrete com horário |
| `/lembretes` | Lista lembretes pendentes |
| `/apagar_lembrete <id>` | Apaga um lembrete pendente |
| `/agendar_comando <tipo> [args] <quando>` | Agenda uma ação automática (`transito_casa`/`transito_trabalho`/`congresso`/`clima`/`chat`; owner também: `agente`/`shell`) |

> Também por texto/voz livre: *"me lembre de pagar o boleto amanhã 10h"*,
> *"todo domingo 20h me manda o resumo da semana"*, *"apaga o lembrete 5"*.

**Recorrência cron** — além dos presets (`daily`, `weekday`, `weekend`,
`weekly:<dias>`, `monthly`), qualquer agendamento aceita `cron:<expressão de
5 campos>` (avaliada no fuso do usuário, intervalo mínimo de 10 min). Pedidos
como *"a cada 2 horas"* ou *"dia 1 e 15 às 9h"* viram cron automaticamente
via chat. Tipos owner-only:

- **`agente`** — roda o [agente de execução](#agente-de-execução-de-código-owner-only)
  com a tarefa dada (sessão sempre nova, progresso e artefatos como no
  `/agente`). Se o agente estiver ocupado no disparo, o scheduler re-tenta a
  cada tick até despachar.
- **`shell`** — executa um comando **fixo** no shell do container, sem LLM
  (custo zero, determinístico): backups, healthchecks, limpeza. Timeout de
  300s, saída (cap 3000 chars) + exit code no chat; prefixo `@silencioso`
  só notifica em falha. Roda com env mínimo (sem tokens do bot), `cwd` no
  workspace.

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

### Viagens (SerpAPI)

```bash
SERPAPI_KEY=...          # cadastro em https://serpapi.com (plano free inclui 100 buscas/mês)
TRAVELS_ALERT_HOUR=8     # hora BRT em que os watches diários são verificados (padrão: 8)
```

Sem `SERPAPI_KEY` as tools de viagem ficam desabilitadas mas não causam erro.

A **mesma** `SERPAPI_KEY` alimenta a tool **`buscar_preco`** (preço de produto
via Google Shopping: preço + loja + link direto do anúncio — `buscar_web` não
serve aqui porque o marketplace bloqueia e o link sai genérico). A cota é
**compartilhada** com voo/hotel. Se o SerpAPI ficar sem cota ou fora do ar,
`buscar_preco` **cai automaticamente** pra busca web (`search_and_read`),
devolvendo preço aproximado — nunca erro duro.

### Busca web (`buscar_web` / `/buscar`)

Busca **e lê** o corpo das páginas (markdown renderizado, com JS) — permite
responder perguntas cujo dado só existe dentro da página e muda com o tempo:
*"que horas tem sessão do filme X no Cinemark do shopping Y?"*, horário de
funcionamento, preços, cardápios. A busca web **nativa** (snippets) acha a
página certa mas não traz esses dados; só lendo a página eles aparecem.

Dois caminhos usam isso:
- **Chat livre / voz** → o agente aciona a tool `buscar_web` quando faz sentido.
- **Comando `/buscar <termo>`** (e por voz: *"busca X"*, *"pesquisa X"*,
  *"procura X"*, *"google X"*) → lê página + síntese curta, e **cai pra busca
  nativa** (Anthropic `web_search` / Gemini `google_search`) só se nenhum
  backend de leitura estiver configurado ou todos falharem.

#### Backends (primário → fallback)

```bash
WEBSEARCH_BACKEND=searxng     # primário: "searxng" (padrão) ou "firecrawl"
WEBSEARCH_FALLBACK=true       # se o primário falhar, tenta o outro

# SearXNG (self-hosted, custo ZERO) — primário recomendado
SEARXNG_URL=http://192.168.1.50:8080
JINA_API_KEY=                 # opcional (Jina Reader lê os links); sobe o rate limit

# Firecrawl (turnkey, gasta créditos) — fallback por padrão
FIRECRAWL_API_KEY=...         # https://www.firecrawl.dev → Dashboard → API Keys (free tier)
```

Como funciona o `search_and_read` (`bot/services/websearch.py`):

1. Tenta o backend de `WEBSEARCH_BACKEND`. Um backend **sem credencial é
   pulado** (não conta como falha) — dá pra rodar só com um dos dois.
2. **SearXNG**: `GET {SEARXNG_URL}/search?q=...&format=json` pega os links →
   **Jina Reader** (`https://r.jina.ai/<url>`) lê cada um. Resultado vazio
   (ex.: engines em 429) conta como falha → aciona o fallback.
3. **Firecrawl**: `search + scrape` num call só.
4. Se o primário falhar e `WEBSEARCH_FALLBACK=true`, o outro é tentado.

O contrato é o mesmo pros dois backends, então a escolha é só de configuração —
o agente e a tool `buscar_web` não mudam.

> **Instalar o SearXNG:** passo a passo em
> [Setup de serviços externos → SearXNG](#searxng-backend-primário-de-busca-web).

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

### Cotação de carteira (B3)

```bash
BRAPI_TOKEN=...     # cadastro gratuito em https://brapi.dev
```

Usado na **revisão de carteira** do agente proativo (última janela do dia,
ex.: 19h): busca o preço de mercado de ações/FIIs/ETFs via brapi.dev, grava o
`currentPrice` no Firestore (o app web passa a mostrar P&L real também) e
manda uma mensagem com **investido vs valor de mercado + P&L** por ativo.
Tesouro Direto fica de fora (valor projetado, sem ticker de bolsa); cripto não
é coberto. Sem `BRAPI_TOKEN`, a revisão é pulada sem afetar o resto.

### Gerenciador financeiro (Firestore)

Configure em runtime com `/financeiro_setup` (envie o JSON da service account
do Firebase e informe o UID). As credenciais ficam no SQLite (`kv_settings`),
não no `.env`. Integra com o app externo *gerenciador-financeiro* — usa as
mesmas chaves do `state` no Firestore (`bankTransactions`, `cardEntries`,
`treasuryHoldings`, `investments.assets`) e nunca altera o schema. Cálculos de
saldo, projeção do Tesouro pra hoje e métricas dos ativos (PM/posição/P&L)
replicam as fórmulas do `src/store.jsx`/`src/investimentos.jsx` do app.

### Agente de execução (Claude Code headless)

Passos manuais pra ligar (depois do rebuild):

1. **`OWNER_TELEGRAM_ID`** (obrigatório): seu ID numérico do Telegram (pegue
   com o @userinfobot). Vazio = agente desabilitado.
2. **`ANTHROPIC_API_KEY`**: o agente reusa a chave já configurada. Atenção a
   custo — cada tarefa consome mais tokens que um chat; `AGENT_MAX_COST_USD`
   (default US$ 1,50) limita o gasto **por tarefa**.
3. **`AGENT_GITHUB_TOKEN`** (opcional): fine-grained PAT em GitHub → Settings →
   Developer settings → Fine-grained tokens, selecionando **apenas os repos**
   que o agente pode tocar (Contents read/write + Pull requests read/write).
   Sem ele o agente só clona repositórios públicos.
4. **Rebuild**: `git pull && docker compose build && docker compose up -d`
   (a primeira build instala Node 22 + Claude Code CLI + git + gh — demora
   mais no Pi).
5. Ajustes finos (`AGENT_MODEL`, `AGENT_TIMEOUT_SECONDS`, `AGENT_MAX_TURNS`,
   `AGENT_MAX_COST_USD`, `AGENT_SESSION_TTL_MINUTES`) têm default no `.env.example`
   e podem ser mudados **em runtime** via `/agente_config`, sem restart — os
   overrides ficam em `data/agent_config.json` e sobrevivem a reinício.
6. **`AGENT_CRON_DAILY_BUDGET_USD`** (opcional, default 0 = sem teto): limite
   diário somado das execuções **agendadas** (cron) do agente. Atingiu o teto,
   as ocorrências do dia são puladas (aviso 1×/dia). Contador em memória —
   zera no restart.

Limitações de desenho: a execução vive dentro da tarefa (processos longos,
tipo um servidor web, morrem no fim/timeout; o compose não expõe portas) e o
conteúdo web que o agente lê é não-confiável — por isso env limpo + workspace
confinado + revisão humana dos artefatos.

### Google Maps

A `GOOGLE_MAPS_API_KEY` é usada para **Directions API** (digest e
`/transito_agora`), **Geocoding API (New)** (`/rota <endereço>`, endpoint
v4beta) e **Places API (New)** (tool `buscar_local`: telefone/endereço/horário
de funcionamento oficial de estabelecimentos). Habilite as três no Google
Cloud. Sem Geocoding, os atalhos (`/rota casa`/`trabalho`) ainda funcionam;
sem a Places API (New), `buscar_local` retorna 403.

> **Por que `buscar_local` e não `buscar_web` pra contato de lugar?** Telefone/
> endereço/horário de estabelecimento mora no painel estruturado do Google; a
> busca web (SearXNG/Firecrawl) cai em agregadores com dado secundário/errado.
> O agente é instruído a usar `buscar_local` (Places API) pra essa categoria.

### Rota preferida (`ROUTE_GOOGLE_MAPS_URL`)

Trace a rota habitual no Google Maps **com paradas intermediárias** →
Compartilhar → Copiar link → cole em `ROUTE_GOOGLE_MAPS_URL`. O bot extrai os
waypoints e compara com a melhor alternativa do Google em tempo real.

## Setup de serviços externos

Tutoriais de instalação dos requisitos auto-hospedados. (APIs gerenciadas —
Anthropic, Gemini, SerpAPI, Firecrawl, Google Maps — são só uma chave no
`.env`; ver [Configuração](#configuração).)

### SearXNG (backend primário de busca web)

O `buscar_web` usa **SearXNG** (metabusca self-hosted, custo zero) como
primário e Firecrawl como fallback. O SearXNG roda em qualquer máquina da sua
rede (inclusive um Orange Pi/Raspberry antigo) — só precisa de Docker e ser
alcançável pelo bot na LAN. Aponte `SEARXNG_URL` pro IP:porta dele.

**1. Pré-checagem**
```bash
uname -m            # aarch64 (ok) | armv7l (32-bit, ver nota no fim)
docker --version    # sem docker:  curl -fsSL https://get.docker.com | sh
sudo systemctl enable --now docker
```

**2. Estrutura + `docker-compose.yml`**
```bash
sudo mkdir -p /opt/searxng/config && cd /opt/searxng
sudo tee docker-compose.yml >/dev/null <<'YAML'
services:
  searxng:
    image: searxng/searxng:latest
    container_name: searxng
    restart: unless-stopped
    ports:
      - "8080:8080"                 # exposto na LAN (ver firewall no passo 6)
    volumes:
      - ./config:/etc/searxng
    environment:
      - SEARXNG_BASE_URL=http://0.0.0.0:8080/
      - SEARXNG_PORT=8080
    cap_drop: [ALL]
    cap_add: [CHOWN, SETGID, SETUID, DAC_OVERRIDE]
    mem_limit: 512m
    logging:
      driver: json-file
      options: { max-size: "10m", max-file: "3" }
YAML
```
> Sem Valkey/Redis de propósito: instância **privada** consumida só pelo bot →
> o limiter (anti-abuso de instância pública) fica off (passo 4), então Redis
> não é necessário. Menos um container = mais leve em hardware fraco.

**3. Primeiro boot** (gera o `config/settings.yml`)
```bash
sudo docker compose up -d && sleep 10 && ls config/
```
> Imagens recentes usam **granian** (não uwsgi) — só `settings.yml` é gerado,
> sem `uwsgi.ini`. É o esperado.

**4. Endurecer o `settings.yml`**
```bash
# secret aleatório (troca o placeholder)
sudo sed -i "s|ultrasecretkey|$(openssl rand -hex 32)|g" config/settings.yml
sudo nano config/settings.yml
```
Garanta estes blocos (`server.limiter: false` p/ instância privada; e
`json` em `search.formats` é **essencial** pra API):
```yaml
server:
  limiter: false
  public_instance: false
search:
  formats:
    - html
    - json
```

**5. Reinicia e valida o JSON**
```bash
sudo docker compose restart && sleep 5
curl -s 'http://localhost:8080/search?q=teste&format=json' | python3 -m json.tool | head -20
```
Esperado: JSON com `results: [...]`. Se vier HTML/`403`, o `json` em
`search.formats` não pegou ou o limiter ainda está on.

**6. Firewall (LAN-only)** — não exponha à internet:
```bash
sudo ufw allow from 192.168.0.0/16 to any port 8080 proto tcp
```

**7. No bot** (`.env` na máquina do Concierge):
```bash
WEBSEARCH_BACKEND=searxng
SEARXNG_URL=http://IP_DA_INSTANCIA:8080
```

**Operação / troubleshooting**
- Saúde dos engines: `http://IP:8080/stats/errors`. Se um engine der 429 (ex.:
  Brave, comum), desligue só ele — em `engines:` do `settings.yml`:
  ```yaml
  engines:
    - name: brave
      disabled: true
  ```
- Logs/uso: `sudo docker compose logs -f` · `sudo docker stats searxng --no-stream`
- Atualizar: `sudo docker compose pull && sudo docker compose up -d`
- **ARM 32-bit (`armv7l`)**: a imagem publica `linux/arm/v7`; se o `up` reclamar
  de *"no matching manifest"*, é hardware sem imagem oficial — use outra máquina.

## Rodar localmente

```bash
git clone https://github.com/srocupado/Bot-Telegram-Concierge.git && cd Bot-Telegram-Concierge
cp .env.example .env && nano .env          # preencha BOT_TOKEN, ACCESS_PASSWORD e as chaves
pip install -r requirements.txt && python -m bot
```

Atualizar depois:
```bash
git pull origin main && pip install -r requirements.txt && python -m bot
```

## Rodar com Docker

```bash
git clone https://github.com/srocupado/Bot-Telegram-Concierge.git && cd Bot-Telegram-Concierge
cp .env.example .env && nano .env          # preencha as chaves
docker compose up -d --build && docker compose logs -f --tail=40
```

Atualizar depois:
```bash
git pull origin main && docker compose up -d --build && docker compose logs -f --tail=40
```

O SQLite é persistido em `./data/concierge.db` (volume).

### Quando rebuildar vs recriar

| Mudou | Comando |
|---|---|
| Código (`bot/`, `requirements.txt`) | `docker compose up -d --build` |
| Só `.env` | `docker compose up -d --force-recreate` |
| Nada (só restart) | `docker compose restart` |

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
- **`user_facts`**: memória persistente entre sessões (`user_id`, `key`
  snake_case, `value` texto livre, `updated_at`). Chave única por usuário;
  upsert automático.
- **`chat_log`**: histórico do chat livre (`user_id`, `role`, `content`,
  `created_at`) — write-through do contexto em RAM; re-hidrata no boot,
  alimenta a busca FTS5 (`chat_log_fts` + triggers). Purge >90 dias.
- **`chat_summaries`**: resumo rolante de longo prazo por usuário
  (`user_id` PK, `summary`, `updated_at`), injetado no system prompt.
- **`travel_watches`**: monitors diários de preço (`kind` flight/hotel,
  `params` JSON com IATA/datas/classe, `max_price`, `min_price_seen`,
  `last_price`, `last_checked_at`, `last_alert_at`, `status`
  active/cancelled, `currency`, `snooze_until`).
- **`travel_price_snapshots`**: histórico de preços capturados a cada
  verificação (`watch_id`, `price`, `currency`, `raw` JSON). Purge >90 dias.
- **`travel_alerts`**: log de alertas enviados (`watch_id`, `snapshot_id`,
  `price`, `reason` below_max/new_min). Purge >90 dias.

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
