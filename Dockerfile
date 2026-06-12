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
        sshpass \
        rsync \
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

COPY bot ./bot

RUN mkdir -p /app/data /app/workspace

ENTRYPOINT ["tini", "--"]
CMD ["python", "-m", "bot"]
