#!/usr/bin/env python3
"""Prepare exact original per-task contexts; never fetch, publish or solve.

Only the sealed public input allowlist is read from the original fixed bundle.
Tests, hidden gold, solutions, unrelated files and host credentials cannot enter
these contexts. Publishing the rendered data requires the source rights grant.
"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import tarfile

SOURCE_SHA = "f7bad307ac3127e63f17acf2cb59555a057e9afb616216365aa30217e0e733a3"
BASE = "ghcr.io/codex-radar/dradar-pompeii-runtime@sha256:6fcfa035ff6d70316ca74f44d2444e694e46dd367b44731cb5d036aead825738"
DOCKERFILE = f"""FROM {BASE}
WORKDIR /app
COPY app/ /app/
RUN git init -b main && git config user.name dradar-task && git config user.email task@dradar.invalid \\
 && git add . && GIT_AUTHOR_DATE=2026-01-01T00:00:00Z GIT_COMMITTER_DATE=2026-01-01T00:00:00Z git commit -m base \\
 && git tag pompeii-base && git config core.hooksPath /dev/null
CMD ["/bin/bash"]
"""


def prepare(bundle, manifest_path, output):
    raw = Path(bundle).read_bytes()
    if sha256(raw).hexdigest() != SOURCE_SHA:
        raise ValueError("original fixed16 bundle differs")
    manifest = json.loads(Path(manifest_path).read_bytes())
    if manifest["source_bundle_sha256"] != SOURCE_SHA or len(manifest["tasks"]) != 16:
        raise ValueError("fixed16 manifest differs")
    output = Path(output)
    if output.exists():
        raise FileExistsError("refusing to overwrite a context")
    # Validate all bytes and exact archive membership before writing anything.
    checked = []
    with tarfile.open(bundle, "r:gz") as archive:
        members = archive.getmembers()
        names = {member.name: member for member in members}
        if len(names) != len(members):
            raise ValueError("duplicate archive member")
        for task in manifest["tasks"]:
            tid = task["task_id"]
            files = []
            for fact in task["files"]:
                name = tid + "/" + fact["path"]
                member = names[name]
                if not member.isfile() or member.size != fact["bytes"]:
                    raise ValueError("original input is not a regular file")
                data = archive.extractfile(member).read()
                if sha256(data).hexdigest() != fact["sha256"]:
                    raise ValueError("original input hash differs: " + name)
                if fact["path"].startswith("environment/app/"):
                    relative = fact["path"].removeprefix("environment/app/")
                    if Path(relative).is_absolute() or ".." in Path(relative).parts or ".git" in Path(relative).parts:
                        raise ValueError("invalid original app path")
                    files.append((relative, data, fact))
            checked.append((task, files))
    output.mkdir(parents=True)
    rows = []
    for task, files in checked:
        folder = output / task["task_id"]
        (folder / "app").mkdir(parents=True)
        for name, data, fact in files:
            target = folder / "app" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(0o644)
        (folder / "Dockerfile").write_text(DOCKERFILE)
        (folder / ".dockerignore").write_text("*\n!Dockerfile\n!app/\n!app/**\n")
        rows.append({"task_id":task["task_id"],"task_content_hash":task["task_content_hash"],
            "source_bundle_sha256":SOURCE_SHA,"base_image":BASE,
            "files":[dict(path=n,bytes=f["bytes"],sha256=f["sha256"]) for n,_,f in files],
            "context_files":sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file()),
            "dockerfile_sha256":sha256(DOCKERFILE.encode()).hexdigest()})
    receipt={"schema":"dradar.pompeii-image-contexts/1","source_bundle_sha256":SOURCE_SHA,
             "task_count":len(rows),"rows":rows,"built":False,"published":False}
    (output / "contexts.json").write_text(json.dumps(receipt,indent=2)+"\n")
    return receipt


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle",required=True,type=Path)
    parser.add_argument("--manifest",required=True,type=Path)
    parser.add_argument("--output",required=True,type=Path)
    args=parser.parse_args()
    result=prepare(args.bundle,args.manifest,args.output)
    print(json.dumps({"tasks":result["task_count"],"output":str(args.output),"published":False}))
