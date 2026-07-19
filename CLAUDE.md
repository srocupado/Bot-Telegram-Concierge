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
- Identidade do modelo (id `claude-*`) NÃO aparece em commit/PR/código.
