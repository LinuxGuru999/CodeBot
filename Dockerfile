FROM python:3.14-slim

LABEL maintainer="codebot"
LABEL description="CodeBot autonomous engineering platform"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CODEBOT_PROJECT_ROOT=/project \
    CODEBOT_STATE_DIR=/data/state \
    CODEBOT_LOGS_DIR=/data/logs

RUN useradd -m -r codebot && \
    mkdir -p /app /data/state /data/logs /project && \
    chown -R codebot:codebot /app /data /project

WORKDIR /app
COPY --chown=codebot:codebot bots/ ./bots/
COPY --chown=codebot:codebot .codebot/ ./.codebot/

USER codebot

VOLUME ["/data", "/project"]

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python3 -c "import json; print('ok')" || exit 1

ENTRYPOINT ["python3", "-m", "bots.codebot_bootstrap"]
CMD ["--project-root", "/project"]
