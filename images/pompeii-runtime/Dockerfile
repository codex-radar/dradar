# DRadar generic runtime; not an official Pompeii/RePAIR task image.
# No task input, source dataset, gold, collection sidecar, or credential is copied.
FROM docker.io/library/node:22-bookworm@sha256:0e5f906573693feaa1e21057ebdcfdb5bd5021f050b2dc7c9deceb629c7da2a8
ARG BUILD_COMMIT
ARG CONTEXT_SHA256
LABEL org.opencontainers.image.source="https://github.com/codex-radar/dradar" \
      org.opencontainers.image.revision="${BUILD_COMMIT}" \
      io.codexradar.context-sha256="${CONTEXT_SHA256}" \
      io.codexradar.source.kind="generic-runtime-without-dataset" \
      io.codexradar.official-repair-image="false"
RUN apt-get update && apt-get install -y --no-install-recommends git python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
CMD ["/bin/bash"]
