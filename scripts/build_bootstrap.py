"""Build a deterministic, dependency-free bootstrap wheel; never publish it."""

import argparse
import base64
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import zipfile


def build(source_root: Path, output: Path):
    module = (source_root / "release/bootstrap/dradar_bootstrap.py").read_bytes()
    shared = (source_root / "src/dradar/bootstrap_receipt.py").read_bytes()
    version = re.search(rb'^VERSION = "([0-9]+\.[0-9]+\.[0-9]+)"$', module, re.M).group(1).decode()
    info = f"dradar_bootstrap-{version}.dist-info"
    files = {
        "dradar_bootstrap.py": module, "dradar_bootstrap_receipt.py": shared,
        info + "/METADATA": f"Metadata-Version: 2.1\nName: dradar-bootstrap\nVersion: {version}\nRequires-Python: >=3.11\n".encode(),
        info + "/WHEEL": b"Wheel-Version: 1.0\nGenerator: dradar-bootstrap-builder\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        info + "/entry_points.txt": b"[console_scripts]\ndradar-start = dradar_bootstrap:main\n",
    }
    record = io.StringIO()
    writer = csv.writer(record, lineterminator="\n")
    for name, data in sorted(files.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
        writer.writerow([name, "sha256=" + digest, str(len(data))])
    writer.writerow([info + "/RECORD", "", ""])
    files[info + "/RECORD"] = record.getvalue().encode()
    output.mkdir(parents=True, exist_ok=True)
    path = output / f"dradar_bootstrap-{version}-py3-none-any.whl"
    with path.open("xb") as handle, zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(files.items()):
            entry = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, data)
    manifest = {"version": version, "filename": path.name, "size": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "source_sha256": {name: hashlib.sha256(data).hexdigest() for name, data in files.items() if name.endswith(".py")}}
    with (output / "bootstrap-build.json").open("x") as handle:
        json.dump(manifest, handle, indent=2)
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.source_root, args.output_dir), indent=2))
