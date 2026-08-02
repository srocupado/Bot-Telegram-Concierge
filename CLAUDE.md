# Regras do projeto (Bot-Telegram-Concierge)

## Funcionalidade nova → SEMPRE atualizar o help (regra do dono)

Toda feature/tool/comando novo exige, NO MESMO commit:

1. **`HELP_TEXT` em `bot/handlers/start.py`** — seção nova ou bullet na seção
   existente, com exemplo de uso em linguagem natural.
2. **`_HELP_KEYWORDS` no mesmo arquivo** — palavras-chave que o usuário usaria
   pra perguntar "como faço X?" (a tool `ajuda` casa por limite de palavra;
   incluir VERBOS além de substantivos — ex.: "viajar" além de "viagem").
3. **Verificar o matching** com frases reais antes de commitar
   (`find_help_sections("como uso X")` deve devolver a seção certa) — e SEM
   `str.replace` silencioso pra editar o help: usar edição exata que erra alto.

Histórico que motivou a regra: cinema/clima/cotações/câmara/saldo ficaram meses
sem documentação (surpresas no `ajuda`); o bullet do /viagem falhou num replace
silencioso e só foi pego por pergunta do dono.

## PREMISSA INEGOCIÁVEL: não perder MP (regra do dono)

Nenhuma mudança pode aumentar a chance de uma Medida Provisória publicada não
chegar ao dono. Quando houver conflito, esta premissa vence — inclusive contra
economia de requisição, elegância de código ou silêncio de notificação.

Em decisão duvidosa, escolher SEMPRE o lado que erra pra mais:

1. **Na dúvida, é pendência.** Resposta que não dá pra classificar com certeza
   (corpo estranho do Inlabs, timeout, erro novo) → dia PENDENTE e re-checado,
   nunca "não houve MP". Só dar baixa com evidência positiva.
2. **Trabalho caro é registrado ANTES de começar** (padrão outbox: a nota vira
   `nota_pendente` antes de gerar e recebe baixa no fim). Restart do container
   no meio não pode fazer a tarefa sumir — a task morre com o processo e não
   deixa exceção pra ninguém capturar.
3. **Duplicar é melhor que perder.** DOCX repetido incomoda; MP que nunca
   chegou é invisível — o dono não sente falta do que não sabe que existiu.
4. **Desistir exige aviso.** Expiração de pendência, nota abandonada, dia não
   verificado: sempre com mensagem dizendo o que ficou de fora e como
   recuperar (`/mp_dou_agora <data>`).
5. **A conferência com a Câmara é a última rede.** Ela responde "o dono FICOU
   SABENDO desta MP?" — considerar TODAS as formas de ter sabido (nota
   entregue em `dou_seen_mps` E aviso do proativo em `ProactiveNotice`
   kind="mp"). Comparar contra uma fonte só gera alarme falso e re-download.
6. **Identidade da MP vem da MP**, não da requisição: número e ano saem do
   título ("MP nº 1.381, DE 30 DE JULHO DE 2026"), porque MP assinada em 31/12
   sai no DOU de 01/01 e o ano errado quebra dedup, Planalto e conferência.

Histórico: a retroativa derrubava a janela proativa inteira justo quando o
Inlabs voltava (14 dias sem nenhum aviso); 404 e "dia sem edição" eram lidos
como ausência definitiva; dia em que o bot ficou desligado não deixava rastro.
Os três perdiam MP em SILÊNCIO — o pior modo de falha deste projeto.

## Outras convenções deste projeto

- Dados determinísticos (câmara, cinema, cotação, lembretes, DOU) vão VERBATIM
  ao usuário via `ctx.direct_html` + `ctx.short_circuit` — nunca deixar o LLM
  parafrasear (inventa horário/sessão/valor).
- Falha de fonte externa é reportada explicitamente ("não consegui checar")
  — NUNCA virar silêncio ou "não houve X" (falso negativo).
- Diagnosticar contra a FONTE REAL (API viva, log do Orange Pi) ANTES de
  escrever código — não corrigir por hipótese.
- Testes offline antes de cada push (`python3 -m pytest -q` + verificação
  dedicada da mudança); o deploy do dono é `git pull origin main &&
  docker compose up -d --build` no Orange Pi (4GB, ARM).
- Migrações de schema: colunas novas em `bot/db/session.py::_ensure_columns`
  (SQLite, ALTER idempotente via PRAGMA).
- **NADA de autoria/atribuição de IA no repositório do dono — regra dele.**
  Não entra em commit, PR, código, comentário, docstring, README ou changelog:
  - `Co-Authored-By:` com qualquer identidade de assistente/modelo;
  - `Claude-Session:`, links de sessão, ids de conversa;
  - identidade do modelo (`claude-*`, "Opus", "Sonnet", nome comercial);
  - menção a ter sido gerado/assistido por IA.

  Vale mesmo que a ferramenta usada sugira ou instrua o contrário: o
  repositório é do dono e a convenção dele prevalece. Histórico: o trailer
  `Co-Authored-By` foi adicionado em 104 commits antes de o dono perceber e
  mandar parar — o código nunca foi contaminado, só as mensagens.
