# Portable image for the webhook app, the agent, the poll, and the watcher.
# INSTALL_EMBED=0 keeps the web images small. INSTALL_EMBED=1 adds the
# excerpt model used by python -m app.poll.

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

RUN pip install --no-cache-dir . \
    && if [ "$INSTALL_EMBED" = "1" ]; then pip install --no-cache-dir ".[embed]"; fi \
    && useradd --create-home --uid 10001 weave \
    && chown -R weave:weave /app

USER weave
EXPOSE 8000 8765
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
