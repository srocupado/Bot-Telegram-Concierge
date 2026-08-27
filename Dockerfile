FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        tini \
        ca-certificates \
        tzdata \
        curl \
        gnupg \
        git \
        openssh-client \
        rsync \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Node 22 LTS (NodeSource, arm64 ok) + Claude Code CLI pinado + gh CLI —
# runtime do agente de execução (/agente). git/gh habilitam clone/push/PRs.
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs gh \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code@2.1.175

COPY requirements.txt .
RUN pip install -r requirements.txt

# pytest na IMAGEM (não em requirements.txt, que é produção): o agente
# escreve teste junto com toda tool nova (/tool_nova) e o /tool_ativar roda
# esse teste antes de deixar o dono aprovar. Sem isto o agente gastava
# turnos instalando na marra E a instalação sumia no rebuild seguinte —
# aí /tool_ativar reprovaria toda candidata com "o teste falhou", culpando
# o teste quando faltava o executor (medido em 27/08/2026).
RUN pip install --no-cache-dir pytest==8.3.4 pytest-asyncio==0.24.0

COPY bot ./bot

RUN mkdir -p /app/data /app/workspace

ENTRYPOINT ["tini", "--"]
CMD ["python", "-m", "bot"]
