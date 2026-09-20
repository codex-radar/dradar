"""The install commands this repository publishes must name its own real path.

`SecurityMind/dradar` is where this repository used to live. github.com still
answers for it with a redirect here, so a command built from the old path installs
the right code today -- and keeps doing so right up until somebody creates a *new*
repository at that path. At that moment the redirect stops and the same pasted
command resolves to that repository instead. Nothing errors; the source silently
changes. `SecurityMind` is an account this project logs into every day, so this is
a self-inflicted path, not a hypothetical.

README.md is the first thing anyone arriving from GitHub search or a link reads,
and its `uvx --from` lines are meant to be copied and run, so it is held to the
same rule as the website and the server's /install.sh.
"""

from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]

DEPRECATED = "securitymind/dradar"
CANONICAL = "codex-radar/dradar"


def deprecated_refs(text: str) -> list[str]:
    # Case-insensitive on purpose: GitHub resolves repository paths without regard
    # to case, so `securitymind/dradar` and `SECURITYMIND/DRADAR` reach exactly the
    # same place and carry exactly the same hazard. A case-sensitive check would
    # wave them through while looking just as green as a real pass.
    return [line.strip() for line in text.split("\n")
            if DEPRECATED in line.lower()]


def test_the_detector_can_still_find_one():
    # "No occurrences" reads identically whether the tree is clean or the detector
    # is blind -- wrong string, wrong file, empty input all score zero. Prove it
    # can still see, in the same run as the assertions that rely on it.
    assert len(deprecated_refs(
        "uvx --from git+https://github.com/SecurityMind/dradar dradar")) == 1

    for variant in ("securitymind/dradar", "SECURITYMIND/DRADAR",
                    "SecurityMind/Dradar"):
        assert len(deprecated_refs(f"https://github.com/{variant}")) == 1, variant

    # ...and that it stays quiet about the sibling repositories that genuinely do
    # live under SecurityMind. GitHub answers for both of those with 200 and no
    # redirect, so they are a different situation and must keep their names.
    for real in ("https://github.com/SecurityMind/deep-swe",
                 "git+https://github.com/SecurityMind/pier.git@abc"):
        assert deprecated_refs(real) == []


def test_readme_installs_from_the_canonical_repository():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert deprecated_refs(readme) == []
    # Absence of the old path is not presence of the new one: an empty or truncated
    # README would satisfy the assertion above without helping anybody.
    assert f"git+https://github.com/{CANONICAL}" in readme


def test_the_trusted_install_allowlist_still_names_both_namespaces():
    """Pins a deliberate exception, so that removing it has to be deliberate too.

    `_TRUSTED_GIT_INSTALL_PATHS` lists BOTH namespaces on purpose: it gates the
    `argv_prefix` rebuilt from whatever source a volunteer actually installed
    from, and volunteers who installed via the old path are still out there. The
    website carries a prose copy of this same list, so the two must move together.

    Dropping `/SecurityMind/dradar` is registered as separate work (it needs the
    website sentences changed in the same breath, and the existing-installs
    population measured first). When that work happens this test will fail, and
    the right response is to update it together with the website copy -- not to
    delete it, which would quietly remove the only thing keeping the two copies
    of this list in step.
    """
    source = (ROOT / "src" / "dradar" / "run_plans.py").read_text(encoding="utf-8")
    assert '"/SecurityMind/dradar",' in source
    assert f'"/{CANONICAL}",' in source
