# DRadar generic verifier runtime; not an official Pompeii/RePAIR verifier image.
# Fixed current Python 3.12.15/slim-trixie base; not a claim about old built images.
# Original task test.sh, grader.py and gold.json are supplied only by private mounts.
FROM docker.io/library/python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f
ARG BUILD_COMMIT
ARG CONTEXT_SHA256
LABEL org.opencontainers.image.source="https://github.com/codex-radar/dradar" \
      org.opencontainers.image.revision="${BUILD_COMMIT}" \
      io.codexradar.context-sha256="${CONTEXT_SHA256}" \
      io.codexradar.source.kind="generic-runtime-without-dataset" \
      io.codexradar.official-repair-image="false"
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
