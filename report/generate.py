#!/usr/bin/env python3
"""Daily repository report: release drift, open PRs, framework versions and quality per repo.

Usage: generate.py <repos.yaml> <out-dir>

Writes <out-dir>/index.html and <out-dir>/data.json. Per repo, from the GitHub API:

  release     latest GitHub release (tag, date)
  unreleased  commits and changed files/lines from that tag to the default branch, files under
              .github/ left out (workflow-only changes need no release)
  prs         open pull requests: Dependabot, Renovate and the rest
  frameworks  Node (package.json engines, .nvmrc/.node-version, Dockerfile `FROM node:N`, workflow
              node_version / node-version), Python (pyproject.toml requires-python, .python-version,
              Dockerfile `FROM python:X.Y`, workflow python_version / python-version; red once
              endoflife.date marks it EOL), .NET TargetFramework(s) from *.csproj /
              Directory.Build.props, and the npm packages in `minimums` from every package.json,
              each against its minimum in repos.yaml: below is red, at or above green. .NET is
              also red once endoflife.date marks it EOL. npm's latest shows in the tooltip.
  quality     summary.json (hwinther/reusable-workflows docs/quality-artifact.md) from the newest
              `quality-summary` workflow artifact on the default branch

Auth: GITHUB_TOKEN_<OWNER> (owner upper-cased, `-` as `_`) per owner, else GITHUB_TOKEN. A GitHub
App installation token per owner needs contents, pull requests, actions and metadata: read.
"""

import html
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
import zipfile
from base64 import b64decode
from datetime import datetime, timezone
from pathlib import Path

import yaml

API = os.environ.get("GITHUB_API_URL", "https://api.github.com")
NOW = datetime.now(timezone.utc)
PROJECT_FILE = re.compile(
    r"(^|/)([^/]+\.csproj|Directory\.Build\.props|package\.json|\.nvmrc|\.node-version|Dockerfile[^/]*"
    r"|pyproject\.toml|\.python-version)$"
    r"|^\.github/workflows/[^/]+\.ya?ml$"
)
DOCKER_NODE = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?(?:\S+/)?node:(\d+)", re.MULTILINE | re.IGNORECASE)
WORKFLOW_NODE = re.compile(r"node[_-]version:\s*[\"']?(\d+)")
DOCKER_PYTHON = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?(?:\S+/)?python:(\d+\.\d+)", re.MULTILINE | re.IGNORECASE)
WORKFLOW_PYTHON = re.compile(r"python[_-]version:\s*[\"']?(\d+\.\d+)")
REQUIRES_PYTHON = re.compile(r"^\s*requires-python\s*=\s*[\"']([^\"']+)", re.MULTILINE)
TARGET_FRAMEWORK = re.compile(r"<TargetFrameworks?>([^<]+)</TargetFrameworks?>")
NET_TFM = re.compile(r"^net(\d+)\.(\d+)")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def token_for(owner):
    return os.environ.get("GITHUB_TOKEN_" + owner.upper().replace("-", "_")) or os.environ.get("GITHUB_TOKEN", "")


def get(url, owner=None, raw=False):
    headers = {"Accept": "application/json", "User-Agent": "wshno-devops-report"}
    if url.startswith(API):
        headers.update({"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    if owner:
        headers["Authorization"] = f"Bearer {token_for(owner)}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
        body = response.read()
    return body if raw else json.loads(body)


def gh(path, owner):
    return get(f"{API}{path}", owner)


def try_gh(path, owner):
    try:
        return gh(path, owner)
    except urllib.error.HTTPError as error:
        if error.code in (404, 409):  # no release / empty repo
            return None
        raise


def days_since(timestamp):
    return (NOW - datetime.fromisoformat(timestamp.replace("Z", "+00:00"))).days


def release_and_drift(repo, owner, branch):
    release = try_gh(f"/repos/{repo}/releases/latest", owner)
    if not release:
        return None, None
    info = {"tag": release["tag_name"], "url": release["html_url"], "publishedAt": release["published_at"], "ageDays": days_since(release["published_at"])}
    compare = try_gh(f"/repos/{repo}/compare/{release['tag_name']}...{branch}?per_page=100", owner)
    if not compare:
        return info, None
    files = [f for f in compare.get("files", []) if not f["filename"].startswith(".github/")]
    drift = {
        "commits": compare["ahead_by"],
        "files": len(files),
        "additions": sum(f["additions"] for f in files),
        "deletions": sum(f["deletions"] for f in files),
        "url": compare["html_url"],
        "truncated": len(compare.get("files", [])) >= 300,
    }
    return info, drift


def open_prs(repo, owner):
    prs, page = [], 1
    while True:
        batch = gh(f"/repos/{repo}/pulls?state=open&per_page=100&page={page}", owner)
        prs += batch
        if len(batch) < 100:
            break
        page += 1
    counts = {"total": len(prs), "dependabot": 0, "renovate": 0, "other": 0}
    for pr in prs:
        login = pr["user"]["login"]
        key = "dependabot" if login.startswith("dependabot") else "renovate" if login.startswith("renovate") else "other"
        counts[key] += 1
    return counts


def dotnet_cycles():
    cycles = get("https://endoflife.date/api/dotnet.json")
    ga = [c for c in cycles if c.get("releaseDate", "9999") <= NOW.strftime("%Y-%m-%d")]
    latest = max(int(c["cycle"].split(".")[0]) for c in ga)
    return {c["cycle"].split(".")[0]: c for c in cycles}, latest


def python_cycles():
    return {c["cycle"]: c for c in get("https://endoflife.date/api/python.json")}


def past_eol(cycle):
    return bool(cycle and cycle.get("eol") not in (None, False, True) and cycle["eol"] < NOW.strftime("%Y-%m-%d"))


def npm_latest(package, cache):
    if package not in cache:
        try:
            cache[package] = get(f"https://registry.npmjs.org/{package.replace('/', '%2F')}/latest")["version"]
        except (urllib.error.URLError, KeyError, ValueError):
            cache[package] = None
    return cache[package]


def version_tuple(spec):
    match = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", str(spec or ""))
    return tuple(int(x or 0) for x in match.groups()) if match else None


def meets(version, minimum):
    """version >= minimum, comparing as many parts as the minimum gives ("19", "0.88")."""
    parts = len(str(minimum).split("."))
    return version_tuple(version)[:parts] >= version_tuple(minimum)[:parts]


def frameworks(repo, owner, branch, minimums, dotnet, pythons, npm_cache):
    tree = try_gh(f"/repos/{repo}/git/trees/{branch}?recursive=1", owner)
    paths = [t["path"] for t in (tree or {}).get("tree", []) if t["type"] == "blob" and PROJECT_FILE.search(t["path"])]
    npm_packages = [p for p in minimums if p not in ("node", "dotnet", "python")]
    tfms, npm, node, python = set(), {}, {}, {}
    for path in paths[:80]:
        content = try_gh(f"/repos/{repo}/contents/{path}?ref={branch}", owner)
        if not content or "content" not in content:
            continue
        text = b64decode(content["content"]).decode("utf-8", "replace")
        name = path.rsplit("/", 1)[-1]
        if name == "package.json":
            try:
                manifest = json.loads(text)
            except ValueError:
                continue
            engine = (manifest.get("engines") or {}).get("node")
            if version_tuple(engine):
                node[path] = version_tuple(engine)[0]
            deps = {**manifest.get("devDependencies", {}), **manifest.get("dependencies", {})}
            for package in npm_packages:
                if package in deps and version_tuple(deps[package]):
                    npm.setdefault(package, {}).setdefault(deps[package].lstrip("^~="), []).append(path)
        elif name in (".nvmrc", ".node-version"):
            if version_tuple(text.strip()):
                node[path] = version_tuple(text.strip())[0]
        elif name in ("pyproject.toml", ".python-version"):
            found = REQUIRES_PYTHON.findall(text) if name == "pyproject.toml" else [text.strip()]
            versions = [v for v in found if version_tuple(v)]
            if versions:
                python[path] = min(versions, key=version_tuple)
        elif name.startswith("Dockerfile"):
            majors = [int(m) for m in DOCKER_NODE.findall(text)]
            if majors:
                node[path] = min(majors)
            if DOCKER_PYTHON.findall(text):
                python[path] = min(DOCKER_PYTHON.findall(text), key=version_tuple)
        elif path.startswith(".github/workflows/"):
            majors = [int(m) for m in WORKFLOW_NODE.findall(text)]
            if majors:
                node[path] = min(majors)
            if WORKFLOW_PYTHON.findall(text):
                python[path] = min(WORKFLOW_PYTHON.findall(text), key=version_tuple)
        else:
            for value in TARGET_FRAMEWORK.findall(text):
                tfms.update(t.strip() for t in value.split(";") if NET_TFM.match(t.strip()))

    node_out = None
    if node:
        lowest, highest = min(node.values()), max(node.values())
        minimum = minimums.get("node")
        node_out = {
            "version": str(lowest) if lowest == highest else f"{lowest}-{highest}",
            "minimum": minimum,
            "status": "unknown" if minimum is None else "ok" if meets(lowest, minimum) else "below",
            "sources": dict(sorted(node.items())),
        }

    python_out = None
    if python:
        cycles_by_minor = {path: ".".join(map(str, version_tuple(v)[:2])) for path, v in python.items()}
        lowest = min(cycles_by_minor.values(), key=version_tuple)
        highest = max(cycles_by_minor.values(), key=version_tuple)
        minimum = minimums.get("python")
        below = minimum is not None and not meets(lowest, minimum)
        cycle = pythons.get(lowest)
        python_out = {
            "version": lowest if lowest == highest else f"{lowest}-{highest}",
            "minimum": minimum,
            "eol": cycle.get("eol") if cycle else None,
            "status": "below" if below or past_eol(cycle) else "unknown" if minimum is None else "ok",
            "sources": dict(sorted(cycles_by_minor.items())),
        }

    cycles, latest_major = dotnet
    dotnet_out = []
    for tfm in sorted(tfms, key=lambda t: version_tuple(t[3:])):
        major = NET_TFM.match(tfm).group(1)
        cycle = cycles.get(major)
        eol = past_eol(cycle)
        minimum = minimums.get("dotnet")
        below = minimum is not None and not meets(major, minimum)
        dotnet_out.append({
            "tfm": tfm, "minimum": minimum, "eol": cycle.get("eol") if cycle else None, "latestMajor": latest_major,
            "status": "below" if below or eol or not cycle else "ok",
        })

    npm_out = []
    for package in npm_packages:
        for version, used_in in sorted(npm.get(package, {}).items()):
            npm_out.append({
                "name": package, "version": version, "minimum": minimums[package],
                "latest": npm_latest(package, npm_cache),
                "status": "ok" if meets(version, minimums[package]) else "below", "usedIn": sorted(used_in),
            })
    return {"node": node_out, "python": python_out, "dotnet": dotnet_out, "npm": npm_out}


def quality(repo, owner, branch):
    artifacts = try_gh(f"/repos/{repo}/actions/artifacts?name=quality-summary&per_page=20", owner)
    for artifact in (artifacts or {}).get("artifacts", []):
        if artifact.get("expired") or (artifact.get("workflow_run") or {}).get("head_branch") != branch:
            continue
        request = urllib.request.Request(
            artifact["archive_download_url"],
            headers={"Authorization": f"Bearer {token_for(owner)}", "User-Agent": "wshno-devops-report"},
        )
        try:
            urllib.request.build_opener(NoRedirect).open(request, timeout=30)
            return None
        except urllib.error.HTTPError as redirect:
            if redirect.code not in (301, 302, 303, 307):
                raise
            blob = get(redirect.headers["Location"], raw=True)
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            summary = json.loads(archive.read("summary.json"))
        summary["ageDays"] = days_since(artifact["created_at"])
        return summary
    return None


def collect(config):
    dotnet = dotnet_cycles()
    pythons = python_cycles()
    npm_cache = {}
    rows = []
    for entry in config["repos"]:
        repo = entry["repo"]
        owner = repo.split("/")[0]
        print(f"{repo} ...", file=sys.stderr)
        meta = gh(f"/repos/{repo}", owner)
        branch = meta["default_branch"]
        release, drift = release_and_drift(repo, owner, branch)
        rows.append({
            "repo": repo,
            "url": meta["html_url"],
            "kind": entry.get("kind", "app"),
            "private": meta["private"],
            "defaultBranch": branch,
            "release": release,
            "unreleased": drift,
            "prs": open_prs(repo, owner),
            "frameworks": frameworks(repo, owner, branch, {**config["minimums"], **entry.get("minimums", {})}, dotnet, pythons, npm_cache),
            "quality": quality(repo, owner, branch),
        })
    return {"generatedAt": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "dotnetLatestMajor": dotnet[1], "minimums": config["minimums"], "repos": rows}


# --- HTML -----------------------------------------------------------------------------------------

STYLE = """
:root { --bg:#f7f7f5; --card:#fff; --text:#1d1d1b; --muted:#6b6b66; --line:#e3e2dc;
  --ok:#1f7a3a; --ok-bg:#e3f3e8; --warn:#8a5a00; --warn-bg:#fbefd5; --bad:#b42318; --bad-bg:#fde7e4; --link:#1d5fbf; }
@media (prefers-color-scheme: dark) { :root { --bg:#151514; --card:#1e1e1c; --text:#ecebe6; --muted:#9c9b94; --line:#33322e;
  --ok:#6fd08c; --ok-bg:#16301f; --warn:#f0c46a; --warn-bg:#3a2d10; --bad:#ff8a7a; --bad-bg:#3d1a16; --link:#8ab4ff; } }
* { box-sizing:border-box }
body { margin:0; background:var(--bg); color:var(--text); font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif }
main { max-width:1400px; margin:0 auto; padding:24px 16px }
h1 { font-size:20px; margin:0 0 4px } .sub { color:var(--muted); margin:0 0 20px }
.wrap { overflow-x:auto; background:var(--card); border:1px solid var(--line); border-radius:10px }
table { border-collapse:collapse; width:100%; min-width:1100px }
th, td { text-align:left; vertical-align:top; padding:10px 12px; border-bottom:1px solid var(--line) }
th { font-size:12px; font-weight:600; color:var(--muted); text-transform:uppercase; letter-spacing:.03em; white-space:nowrap }
tr:last-child td { border-bottom:0 }
a { color:var(--link); text-decoration:none } a:hover { text-decoration:underline }
.muted { color:var(--muted) } .num { font-variant-numeric:tabular-nums }
.chip { display:inline-block; padding:1px 7px; margin:0 4px 4px 0; border-radius:999px; font-size:12px; white-space:nowrap }
.chip.ok { color:var(--ok); background:var(--ok-bg) } .chip.below { color:var(--bad); background:var(--bad-bg) }
.chip.unknown { color:var(--muted); background:var(--line) } .chip sup { font-size:10px; margin-left:2px }
.ok { color:var(--ok) } .warn { color:var(--warn) } .bad { color:var(--bad); font-weight:600 }
.kind { font-size:11px; color:var(--muted); margin-left:6px }
"""


def esc(value):
    return html.escape(str(value))


def cell_release(r):
    rel = r["release"]
    if not rel:
        return '<span class="muted">no release</span>'
    age = rel["ageDays"]
    cls = "ok" if age <= 30 else "warn" if age <= 90 else "bad"
    return f'<a href="{esc(rel["url"])}">{esc(rel["tag"])}</a><br><span class="{cls}">{age} d ago</span>'


def cell_drift(r):
    d = r["unreleased"]
    if not d:
        return '<span class="muted">–</span>'
    if d["files"] == 0:
        return f'<span class="ok">nothing to release</span><br><span class="muted">{d["commits"]} commits (.github only)</span>' if d["commits"] else '<span class="ok">up to date</span>'
    cls = "warn" if d["commits"] < 20 else "bad"
    more = "+" if d["truncated"] else ""
    return (f'<a href="{esc(d["url"])}" class="{cls}">{d["commits"]} commits</a><br>'
            f'<span class="num muted">{d["files"]}{more} files, +{d["additions"]} −{d["deletions"]}</span>')


def cell_prs(r):
    p = r["prs"]
    parts = [f'<span class="num">{p["other"]}</span> open']
    if p["dependabot"]:
        parts.append(f'<span class="num">{p["dependabot"]}</span> Dependabot')
    if p["renovate"]:
        parts.append(f'<span class="num">{p["renovate"]}</span> Renovate')
    return "<br>".join(parts) if p["total"] else '<span class="muted">none</span>'


def chip(status, label, title, newer=False):
    mark = '<sup title="newer major on npm">\u2191</sup>' if newer else ""
    return f'<span class="chip {status}" title="{esc(title)}">{esc(label)}{mark}</span>'


def cell_frameworks(r):
    f = r["frameworks"]
    chips = []
    if f["node"]:
        n = f["node"]
        sources = ", ".join(f"{path}: {major}" for path, major in n["sources"].items())
        chips.append(chip(n["status"], f"Node {n['version']}", f"minimum {n['minimum']} | {sources}"))
    if f.get("python"):
        py = f["python"]
        sources = ", ".join(f"{path}: {v}" for path, v in py["sources"].items())
        chips.append(chip(py["status"], f"Python {py['version']}", f"minimum {py['minimum']} | EOL {py['eol']} | {sources}"))
    for d in f["dotnet"]:
        chips.append(chip(d["status"], f".NET {d['tfm'][3:]}", f"minimum {d['minimum']} | EOL {d['eol']} | newest {d['latestMajor']}"))
    for p in f["npm"]:
        latest = version_tuple(p["latest"])
        newer = bool(latest and latest[0] > version_tuple(p["version"])[0])
        title = f"minimum {p['minimum']} | latest {p['latest']} | {', '.join(p['usedIn'])}"
        chips.append(chip(p["status"], f"{p['name']} {p['version']}", title, newer))
    return "".join(chips) or '<span class="muted">-</span>'


def cell_tests(q):
    if not q:
        return '<span class="muted">no quality data</span>'
    out = []
    for kind, t in q["totals"]["tests"].items():
        cls = "bad" if t["failed"] else "ok"
        out.append(f'<span class="{cls} num">{t["passed"]}/{t["total"]}</span> <span class="muted">{esc(kind)}</span>')
    age = q.get("ageDays", 0)
    out.append(f'<span class="{"muted" if age <= 7 else "warn"}">{age} d old</span>')
    return "<br>".join(out)


def cell_coverage(q):
    if not q:
        return ""
    out = []
    for name, c in q["components"].items():
        pct = (c.get("coverage") or {}).get("lines", {}).get("percent")
        if pct is not None:
            cls = "ok" if pct >= 80 else "warn" if pct >= 60 else "bad"
            out.append(f'<span class="{cls} num">{pct:.0f} %</span> <span class="muted">{esc(name)}</span>')
    return "<br>".join(out)


def cell_warnings(q):
    if not q or not any("warnings" in c for c in q["components"].values()):
        return ""
    total = q["totals"].get("warnings", 0)
    return f'<span class="{"ok" if total == 0 else "warn" if total < 20 else "bad"} num">{total}</span>'


def cell_vulns(q):
    if not q or not any("vulnerabilities" in c for c in q["components"].values()):
        return ""
    v = q["totals"]["vulnerabilities"]
    crit, high = v["bySeverity"]["critical"], v["bySeverity"]["high"]
    cls = "bad" if crit else "warn" if high else "ok"
    return f'<span class="{cls} num">{crit} crit, {high} high</span><br><span class="muted num">{v["total"]} total, {v["fixable"]} fixable</span>'


def render(data):
    rows = []
    for r in data["repos"]:
        q = r["quality"]
        rows.append(
            "<tr>"
            f'<td><a href="{esc(r["url"])}">{esc(r["repo"])}</a><span class="kind">{esc(r["kind"])}{" · private" if r["private"] else ""}</span></td>'
            f"<td>{cell_release(r)}</td><td>{cell_drift(r)}</td><td>{cell_prs(r)}</td><td>{cell_frameworks(r)}</td>"
            f"<td>{cell_tests(q)}</td><td>{cell_coverage(q)}</td><td>{cell_warnings(q)}</td><td>{cell_vulns(q)}</td>"
            "</tr>"
        )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Repository report</title><style>{STYLE}</style></head>
<body><main>
<h1>Repository report</h1>
<p class="sub">Generated {esc(data["generatedAt"])} · newest .NET: {data["dotnetLatestMajor"]} ·
<span class="chip ok">at or above minimum</span><span class="chip below">below minimum / EOL</span>
<span class="muted">\u2191 newer major on npm</span> ·
<a href="data.json">data.json</a></p>
<div class="wrap"><table>
<thead><tr><th>Repository</th><th>Latest release</th><th>Unreleased on main</th><th>Open PRs</th><th>Frameworks</th>
<th>Tests</th><th>Line coverage</th><th>Warnings</th><th>Vulnerabilities</th></tr></thead>
<tbody>
{chr(10).join(rows)}
</tbody></table></div>
</main></body></html>
"""


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    config = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    data = collect(config)
    (out / "data.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    (out / "index.html").write_text(render(data), encoding="utf-8")


if __name__ == "__main__":
    main()
