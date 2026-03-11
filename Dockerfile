FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Optional external agent packs — space-separated GitHub repo URLs.
# Default empty = minimal image (no external agents).
# Example: https://github.com/bwiebe-solace/sam-homelab-agents
ARG EXTERNAL_AGENT_PACKS=""
RUN if [ -n "$EXTERNAL_AGENT_PACKS" ]; then \
    apt-get update -qq && \
    apt-get install -y --no-install-recommends git ca-certificates && \
    mkdir -p agents configs && \
    for repo_url in $EXTERNAL_AGENT_PACKS; do \
        git clone --depth=1 "$repo_url" /tmp/ext-pack && \
        if [ -f /tmp/ext-pack/requirements.txt ]; then \
            pip install --no-cache-dir -r /tmp/ext-pack/requirements.txt; \
        fi && \
        [ -d /tmp/ext-pack/agents ] && cp -r /tmp/ext-pack/agents/. agents/ || true && \
        [ -d /tmp/ext-pack/configs ] && cp -r /tmp/ext-pack/configs/. configs/ || true && \
        rm -rf /tmp/ext-pack; \
    done && \
    apt-get purge -y git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*; \
fi

COPY . .

CMD ["sam", "run", "--system-env"]
