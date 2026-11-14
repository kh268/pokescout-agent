FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends bash curl ca-certificates git \
    && rm -rf /var/lib/apt/lists/*
ENV CODEAF_TELEMETRY=off DO_NOT_TRACK=1 CODEAF_NO_UPDATE_CHECK=1
RUN curl --fail --show-error --location https://agentfield.ai/get/codeaf -o /tmp/install-codeaf.sh \
    && bash /tmp/install-codeaf.sh --version v0.8.2 --dir /usr/local/bin --no-modify-path --no-start \
    && rm /tmp/install-codeaf.sh \
    && codeaf --version
WORKDIR /tmp
