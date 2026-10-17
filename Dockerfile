# Portable image for the webhook app, the agent, the poll, and the watcher.
# INSTALL_EMBED=0 keeps the web images small. INSTALL_EMBED=1 adds a CPU
# torch and the excerpt model used by scripts/run_worker.py.

FROM python:3.12-slim-bookworm

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    WEAVE_CONFIG=config/weave.yaml

ARG INSTALL_EMBED=0

COPY pyproject.toml ./
COPY app app
COPY shared shared
COPY agents agents
COPY fixtures fixtures
COPY config config
COPY scripts scripts
COPY frontend frontend
COPY deploy deploy

RUN pip install --no-cache-dir . \
    && if [ "$INSTALL_EMBED" = "1" ]; then pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu && pip install --no-cache-dir ".[embed]"; fi \
    && if [ "$INSTALL_EMBED" != "1" ]; then apt-get update && apt-get install -y --no-install-recommends nginx && rm -rf /var/lib/apt/lists/*; fi \
    && useradd --create-home --uid 10001 weave \
    && chmod 755 /app/deploy/edge.sh \
    && chown -R weave:weave /app

USER weave
EXPOSE 8000 8080 8765
ENTRYPOINT ["/app/deploy/edge.sh"]
CMD ["edge"]
