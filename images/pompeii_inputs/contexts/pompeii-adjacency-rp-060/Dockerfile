FROM ghcr.io/codex-radar/dradar-pompeii-runtime@sha256:6fcfa035ff6d70316ca74f44d2444e694e46dd367b44731cb5d036aead825738
WORKDIR /app
COPY app/ /app/
RUN git init -b main && git config user.name dradar-task && git config user.email task@dradar.invalid \
 && git add . && GIT_AUTHOR_DATE=2026-01-01T00:00:00Z GIT_COMMITTER_DATE=2026-01-01T00:00:00Z git commit -m base \
 && git tag pompeii-base && git config core.hooksPath /dev/null
CMD ["/bin/bash"]
