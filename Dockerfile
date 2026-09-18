FROM python:3.14-slim

LABEL maintainer="codebot"
LABEL description="CodeBot autonomous engineering platform"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CODEBOT_PROJECT_ROOT=/project \
    CODEBOT_STATE_DIR=/data/state \
    CODEBOT_LOGS_DIR=/data/logs \
    CONTROL_TOKEN="" \
    SSH_PRIVATE_KEY="" \
    GH_TOKEN="" \
    GITHUB_DRY_RUN="1"

RUN useradd -m -r codebot && \
    mkdir -p /app /data/state /data/logs /project && \
    chown -R codebot:codebot /app /data /project

WORKDIR /app
COPY --chown=codebot:codebot codebot/ ./codebot/
COPY --chown=codebot:codebot pyproject.toml README.md ./

USER codebot

VOLUME ["/data", "/project"]

EXPOSE 8081

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/health')" || exit 1

ENTRYPOINT ["python3", "-m", "codebot"]
CMD ["serve", "--project", "/project"]
