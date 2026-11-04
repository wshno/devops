#!/usr/bin/env python3
"""Wrap the generated report in a ConfigMap for the Flux artifact.

Usage: configmap.py <report-dir> <out.yaml>

Writes ConfigMap `devops-report` with index.html and data.json as keys. The cluster mounts it
into nginx (no subPath, so kubelet swaps in new content without a restart). Written as JSON, which
is valid YAML and needs no quoting of the HTML. A ConfigMap holds at most 1 MiB.
"""

import json
import sys
from pathlib import Path

LIMIT = 1024 * 1024 - 4096  # leave room for metadata


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    report, out = Path(sys.argv[1]), Path(sys.argv[2])
    data = {name: (report / name).read_text(encoding="utf-8") for name in ("index.html", "data.json")}
    size = sum(len(v.encode("utf-8")) for v in data.values())
    if size > LIMIT:
        sys.exit(f"report is {size} bytes, over the ConfigMap limit")
    configmap = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "devops-report", "labels": {"app.kubernetes.io/name": "devops-report"}},
        "data": data,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(configmap, indent=2) + "\n", encoding="utf-8")
    print(f"{out}: {size} bytes")


if __name__ == "__main__":
    main()
