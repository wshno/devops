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
import urllib.parse
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
            return None, None
        except urllib.error.HTTPError as redirect:
            if redirect.code not in (301, 302, 303, 307):
                raise
            blob = get(redirect.headers["Location"], raw=True)
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            summary = json.loads(archive.read("summary.json"))
            names = archive.namelist()
            details = json.loads(archive.read("details.json")) if "details.json" in names else None
        summary["ageDays"] = days_since(artifact["created_at"])
        return summary, details
    return None, None


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
        if entry.get("report") == "prs":
            rows.append({
                "repo": repo, "url": meta["html_url"], "kind": entry.get("kind", "other"), "private": meta["private"],
                "report": "prs", "prs": open_prs(repo, owner),
            })
            continue
        branch = meta["default_branch"]
        release, drift = release_and_drift(repo, owner, branch)
        rows.append({
            "repo": repo,
            "url": meta["html_url"],
            "kind": entry.get("kind", "app"),
            "private": meta["private"],
            "report": "full",
            "defaultBranch": branch,
            "release": release,
            "unreleased": drift,
            "prs": open_prs(repo, owner),
            "frameworks": frameworks(repo, owner, branch, {**config["minimums"], **entry.get("minimums", {})}, dotnet, pythons, npm_cache),
        })
        rows[-1]["quality"], rows[-1]["qualityDetails"] = quality(repo, owner, branch)
    return {"generatedAt": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "dotnetLatestMajor": dotnet[1], "minimums": config["minimums"],
            "coverageThreshold": config.get("coverage_threshold", 70), "repos": rows}


# --- HTML -----------------------------------------------------------------------------------------

# Light by default; dark when the OS/browser asks for it (theme "auto"), or when the viewer picks
# it with the toggle (data-theme on <html>, remembered in localStorage).
DARK = """--bg:#151514; --card:#1e1e1c; --text:#ecebe6; --muted:#9c9b94; --line:#33322e;
  --ok:#6fd08c; --ok-bg:#16301f; --warn:#f0c46a; --warn-bg:#3a2d10; --bad:#ff8a7a; --bad-bg:#3d1a16; --link:#8ab4ff;
  color-scheme:dark;"""

STYLE = """
:root { --bg:#f7f7f5; --card:#fff; --text:#1d1d1b; --muted:#6b6b66; --line:#e3e2dc;
  --ok:#1f7a3a; --ok-bg:#e3f3e8; --warn:#8a5a00; --warn-bg:#fbefd5; --bad:#b42318; --bad-bg:#fde7e4; --link:#1d5fbf;
  color-scheme:light; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { """ + DARK + """ } }
:root[data-theme="dark"] { """ + DARK + """ }
header { display:flex; justify-content:space-between; align-items:flex-start; gap:16px; flex-wrap:wrap }
.theme, .seg { display:inline-flex; border:1px solid var(--line); border-radius:8px; overflow:hidden; background:var(--card) }
.theme button, .seg button { font:inherit; font-size:12px; color:var(--muted); background:none; border:0; padding:5px 10px; cursor:pointer }
.theme button + button, .seg button + button { border-left:1px solid var(--line) }
.theme button[aria-pressed="true"], .seg button[aria-pressed="true"] { color:var(--text); background:var(--line) }
* { box-sizing:border-box }
body { margin:0; background:var(--bg); color:var(--text); font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif }
main { max-width:1400px; margin:0 auto; padding:24px 16px }
h1 { font-size:20px; margin:0 0 4px } h2 { font-size:16px; margin:28px 0 10px }
.wrap.fit { display:inline-block; max-width:100% } table.narrow { min-width:0; width:auto } .prs { white-space:nowrap } table.narrow td.num { text-align:right; min-width:90px } .sub { color:var(--muted); margin:0 0 20px }
.wrap { overflow-x:auto; background:var(--card); border:1px solid var(--line); border-radius:10px }
table { border-collapse:collapse; width:100%; min-width:1100px }
th, td { text-align:left; vertical-align:top; padding:10px 12px; border-bottom:1px solid var(--line) }
th { font-size:12px; font-weight:600; color:var(--muted); text-transform:uppercase; letter-spacing:.03em; white-space:nowrap }
tr:last-child td { border-bottom:0 }
a { color:var(--link); text-decoration:none } a:hover { text-decoration:underline }
a.drill { color:inherit; display:inline-block } a.drill:hover { text-decoration:none; outline:1px dashed var(--line); outline-offset:3px }
.muted { color:var(--muted) } .num { font-variant-numeric:tabular-nums }
.chip { display:inline-block; padding:1px 7px; margin:0 4px 4px 0; border-radius:999px; font-size:12px; white-space:nowrap }
.chip.ok { color:var(--ok); background:var(--ok-bg) } .chip.below { color:var(--bad); background:var(--bad-bg) }
.chip.unknown { color:var(--muted); background:var(--line) } .chip sup { font-size:10px; margin-left:2px }
.ok { color:var(--ok) } .warn { color:var(--warn) } .bad { color:var(--bad); font-weight:600 }
.kind { font-size:11px; color:var(--muted); margin-left:6px }
"""


# In <head>, so the chosen theme applies before the first paint. localStorage can be unavailable
# (private windows, blocked storage): then the page simply follows the OS.
THEME_SCRIPT = """<script>
(function () {
  var key = "devops-report-theme";
  function stored() { try { return localStorage.getItem(key) || "auto"; } catch (e) { return "auto"; } }
  function apply(theme) {
    if (theme === "auto") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", theme);
    document.querySelectorAll(".theme button").forEach(function (b) {
      b.setAttribute("aria-pressed", String(b.dataset.theme === theme));
    });
  }
  apply(stored());
  document.addEventListener("DOMContentLoaded", function () {
    apply(stored());
    document.querySelectorAll(".theme button").forEach(function (b) {
      b.addEventListener("click", function () {
        try { localStorage.setItem(key, b.dataset.theme); } catch (e) {}
        apply(b.dataset.theme);
      });
    });
  });
})();
</script>"""

THEME_TOGGLE = """<div class="theme" role="group" aria-label="Theme">
<button type="button" data-theme="auto" aria-pressed="true">Auto</button>
<button type="button" data-theme="light" aria-pressed="false">Light</button>
<button type="button" data-theme="dark" aria-pressed="false">Dark</button>
</div>"""


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


PR_QUERIES = {
    "other": "is:pr is:open -author:app/dependabot -author:app/renovate",
    "dependabot": "is:pr is:open author:app/dependabot",
    "renovate": "is:pr is:open author:app/renovate",
}


def pr_link(r, kind, label=""):
    """The count of one PR kind, linked to the repo's pull request list filtered the same way."""
    count = r["prs"][kind]
    if not count:
        return ""
    url = f'{r["url"]}/pulls?q={urllib.parse.quote_plus(PR_QUERIES[kind])}'
    return f'<a class="num" href="{esc(url)}">{count}</a>{label}'


def cell_prs(r):
    p = r["prs"]
    if not p["total"]:
        return '<span class="muted">none</span>'
    parts = [pr_link(r, "other", " open") or '<span class="num">0</span> open']
    for kind, label in (("dependabot", " Dependabot"), ("renovate", " Renovate")):
        if p[kind]:
            parts.append(pr_link(r, kind, label))
    return "<br>".join(parts)


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


def drill(r, anchor, html_):
    """Wrap a quality cell in a link to the repo's drill-down page when it has details."""
    if not r.get("qualityDetails") or not html_:
        return html_
    return f'<a class="drill" href="{esc(page_name(r["repo"]))}#{anchor}">{html_}</a>'


def cell_coverage(q, threshold):
    if not q:
        return ""
    out = []
    for name, c in q["components"].items():
        pct = (c.get("coverage") or {}).get("lines", {}).get("percent")
        if pct is not None:
            cls = "ok" if pct >= threshold else "warn" if pct >= threshold - 15 else "bad"
            out.append(f'<span class="{cls} num">{pct:.0f} %</span> <span class="muted">{esc(name)}</span>')
    return "<br>".join(out)


def cell_warnings(q):
    if not q or not any("warnings" in c for c in q["components"].values()):
        return ""
    total = q["totals"].get("warnings", 0)
    return f'<span class="{"ok" if total == 0 else "warn" if total < 20 else "bad"} num">{total}</span>'


def cell_vulns(q, details=None):
    if not q or not any("vulnerabilities" in c for c in q["components"].values()):
        return ""
    groups = cves(details) if details and details.get("vulnerabilities") is not None else None
    if groups is None:  # no details.json: package-match counts from the summary
        v = q["totals"]["vulnerabilities"]
        crit, high = v["bySeverity"]["critical"], v["bySeverity"]["high"]
        cls = "bad" if crit else "warn" if high else "ok"
        return f'<span class="{cls} num">{crit} crit, {high} high</span><br><span class="muted num">{v["total"]} total, {v["fixable"]} fixable</span>'
    crit = sum(1 for g in groups if g["severity"] == "critical")
    high = sum(1 for g in groups if g["severity"] == "high")
    severe_fixable = sum(1 for g in groups if g["severity"] in ("critical", "high") and g["fixedIn"])
    cls = "bad" if crit else "warn" if high else "ok"
    note = f"{severe_fixable} of them fixable, " if crit + high else ""
    return (f'<span class="{cls} num">{crit} crit, {high} high CVEs</span><br>'
            f'<span class="muted num">{note}{len(groups)} advisories in all</span>')


def repo_cell(r):
    return f'<td><a href="{esc(r["url"])}">{esc(r["repo"])}</a><span class="kind">{esc(r["kind"])}{" · private" if r["private"] else ""}</span></td>'


# --- Per-repo drill-down page ----------------------------------------------------------------------

PAGE_STYLE = """
.filters { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin:0 0 10px }
.filters .seg button { font-size:13px }
.filters input { font:inherit; font-size:13px; padding:5px 8px; border:1px solid var(--line); border-radius:8px;
  background:var(--card); color:var(--text); min-width:220px }
.bar { display:inline-block; width:60px; height:6px; border-radius:3px; background:var(--line); vertical-align:middle;
  margin-right:6px; overflow:hidden }
.bar i { display:block; height:100%; background:var(--ok) } .bar.low i { background:var(--bad) }
td.file { font-family:ui-monospace,SFMono-Regular,Consolas,monospace; font-size:12px; word-break:break-all }
details summary { cursor:pointer; color:var(--link) }
details ul { margin:6px 0 0; padding-left:18px } details li { margin:2px 0 }
.chip.critical, .chip.high { color:var(--bad); background:var(--bad-bg) }
.chip.medium { color:var(--warn); background:var(--warn-bg) }
.chip.low, .chip.negligible, .chip.unknown { color:var(--muted); background:var(--line) }
.empty { color:var(--muted); padding:12px 0 }
"""

# Filter buttons share the theme toggle's look. Each group filters the rows of one table by a
# data-* attribute; the search box narrows the coverage table by file name.
PAGE_SCRIPT = """<script>
document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll("[data-filter-group]").forEach(function (group) {
    var table = document.getElementById(group.dataset.filterGroup);
    var search = group.querySelector("input");
    function apply() {
      var active = group.querySelector("button[aria-pressed=true]").dataset.value;
      var text = search ? search.value.toLowerCase() : "";
      table.querySelectorAll("tbody tr").forEach(function (row) {
        var match = active === "all" || (" " + row.dataset.tags + " ").indexOf(" " + active + " ") >= 0;
        if (text && (row.dataset.file || "").indexOf(text) < 0) match = false;
        row.hidden = !match;
      });
    }
    group.querySelectorAll("button").forEach(function (b) {
      b.addEventListener("click", function () {
        group.querySelectorAll("button").forEach(function (o) { o.setAttribute("aria-pressed", String(o === b)); });
        apply();
      });
    });
    if (search) search.addEventListener("input", apply);
    apply();
  });
});
</script>"""


def page_name(repo):
    return f"repo-{repo.replace('/', '-')}.html"


def filter_buttons(table_id, options, search=None):
    buttons = "".join(
        f'<button type="button" data-value="{esc(value)}" aria-pressed="{str(i == 0).lower()}">{esc(label)}</button>'
        for i, (value, label) in enumerate(options)
    )
    box = f'<input type="search" placeholder="{esc(search)}" aria-label="{esc(search)}">' if search else ""
    return f'<div class="filters" data-filter-group="{table_id}"><div class="seg" role="group">{buttons}</div>{box}</div>'


def coverage_section(details, threshold):
    files = [dict(f, component=name) for name, c in (details.get("coverage") or {}).items() for f in c["files"]]
    if not files:
        return '<p class="empty">No per-file coverage in this run.</p>'
    files.sort(key=lambda f: (f["lines"]["percent"] if f["lines"]["percent"] is not None else 101, f["file"]))
    below = sum(1 for f in files if (f["lines"]["percent"] or 0) < threshold)
    full = sum(1 for f in files if f["lines"]["percent"] == 100)
    rows = []
    for f in files:
        pct = f["lines"]["percent"] or 0
        tags = ("below" if pct < threshold else "above") + (" full" if pct == 100 else "")
        branches = f["branches"]["percent"]
        rows.append(
            f'<tr data-tags="{tags}" data-file="{esc(f["file"].lower())}">'
            f'<td class="file">{esc(f["file"])}</td><td class="muted">{esc(f["component"])}</td>'
            f'<td class="num">{f["lines"]["covered"]}/{f["lines"]["total"]}</td>'
            f'<td class="num"><span class="bar{" low" if pct < threshold else ""}"><i style="width:{pct:.0f}%"></i></span>{pct:.1f} %</td>'
            f'<td class="num muted">{"" if branches is None else f"{branches:.0f} %"}</td></tr>'
        )
    return (
        filter_buttons("coverage-table", [("below", f"Under {threshold} % ({below})"), ("all", f"All ({len(files)})"),
                                          ("full", f"100 % ({full})")], search="Filter files")
        + '<div class="wrap fit"><table id="coverage-table" class="narrow"><thead><tr><th>File</th><th>Component</th>'
        + '<th>Lines</th><th>Line coverage</th><th>Branches</th></tr></thead><tbody>'
        + "\n".join(rows) + "</tbody></table></div>"
    )


def warnings_section(details):
    groups = [dict(g, component=name) for name, gs in (details.get("warnings") or {}).items() for g in gs]
    if not groups:
        return '<p class="empty">No warnings in this run.</p>'
    rows = []
    for g in sorted(groups, key=lambda g: -g["count"]):
        examples = "".join(
            f'<li><code>{esc(e["file"])}{":" + str(e["line"]) if e["line"] else ""}</code> {esc(e["message"])}</li>'
            for e in g["examples"]
        )
        more = f' <span class="muted">(first {len(g["examples"])})</span>' if g["count"] > len(g["examples"]) else ""
        rows.append(
            f'<tr><td><code>{esc(g["code"])}</code></td><td class="num">{g["count"]}</td>'
            f'<td class="muted">{esc(g["component"])} / {esc(g["source"])}</td>'
            f'<td><details><summary>examples{more}</summary><ul>{examples}</ul></details></td></tr>'
        )
    return ('<div class="wrap fit"><table class="narrow"><thead><tr><th>Code</th><th>Count</th><th>Where</th>'
            '<th>Examples</th></tr></thead><tbody>' + "\n".join(rows) + "</tbody></table></div>")


SEVERITY_ORDER = ["critical", "high", "medium", "low", "negligible", "unknown"]


def cves(details):
    """Vulnerability matches grouped per advisory: one CVE in five packages built from the same source
    (gcc-14-base, libgcc-s1, libstdc++6, ...) is one problem, not five."""
    groups = {}
    for v in details.get("vulnerabilities") or []:
        g = groups.setdefault(v["id"], {"id": v["id"], "severity": v["severity"], "url": v.get("url"),
                                        "packages": [], "fixedIn": set(), "components": set()})
        if SEVERITY_ORDER.index(v["severity"]) < SEVERITY_ORDER.index(g["severity"]):
            g["severity"] = v["severity"]
        g["packages"].append(v)
        g["fixedIn"].update(v["fixedIn"])
        g["components"].add(v["component"])
    return sorted(groups.values(), key=lambda g: (SEVERITY_ORDER.index(g["severity"]), g["id"] or ""))


def vulnerabilities_section(details):
    groups = cves(details)
    if not groups:
        return '<p class="empty">No vulnerabilities in this run.</p>'
    severe = sum(1 for g in groups if g["severity"] in ("critical", "high"))
    fixable = sum(1 for g in groups if g["fixedIn"])
    rows = []
    for g in groups:
        tags = ("severe " if g["severity"] in ("critical", "high") else "") + ("fixable" if g["fixedIn"] else "")
        ident = f'<a href="{esc(g["url"])}">{esc(g["id"])}</a>' if g.get("url") else esc(g["id"])
        packages = "<br>".join(
            f'<code>{esc(p["package"])}</code> <span class="muted">{esc(p["version"])} ({esc(p["type"] or "")})</span>'
            for p in sorted(g["packages"], key=lambda p: p["package"] or "")
        )
        fixed = esc(", ".join(sorted(g["fixedIn"]))) or '<span class="muted">no fix yet</span>'
        rows.append(
            f'<tr data-tags="{tags.strip()}"><td><span class="chip {esc(g["severity"])}">{esc(g["severity"])}</span></td>'
            f'<td>{ident}</td><td>{packages}</td><td class="num">{fixed}</td>'
            f'<td class="muted">{esc(", ".join(sorted(g["components"])))}</td></tr>'
        )
    matches = len(details.get("vulnerabilities") or [])
    return (
        f'<p class="muted">{len(groups)} advisories across {matches} package matches.</p>'
        + filter_buttons("vuln-table", [("severe", f"Critical & high ({severe})"), ("fixable", f"Fixable ({fixable})"),
                                        ("all", f"All ({len(groups)})")])
        + '<div class="wrap fit"><table id="vuln-table" class="narrow"><thead><tr><th>Severity</th><th>Advisory</th>'
        + '<th>Packages</th><th>Fixed in</th><th>Image</th></tr></thead><tbody>'
        + "\n".join(rows) + "</tbody></table></div>"
    )


def render_repo_page(r, data):
    q, details = r["quality"], r.get("qualityDetails") or {}
    run = q.get("run", {}).get("url", "")
    commit = (q.get("commit") or "")[:7]
    threshold = data["coverageThreshold"]
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(r["repo"])} · quality</title><style>{STYLE}{PAGE_STYLE}</style>{THEME_SCRIPT}{PAGE_SCRIPT}</head>
<body><main>
<header><h1><a href="index.html">Repository report</a> / <a href="{esc(r["url"])}">{esc(r["repo"])}</a></h1>{THEME_TOGGLE}</header>
<p class="sub">Quality from <a href="{esc(run)}">run {esc(q.get("run", {}).get("id", ""))}</a> on {esc(commit)}
({esc(q.get("version", ""))}), {q.get("ageDays", 0)} d old ·
<a href="#coverage">coverage</a> · <a href="#warnings">warnings</a> · <a href="#vulnerabilities">vulnerabilities</a></p>
<h2 id="coverage">Line coverage per file</h2>
{coverage_section(details, threshold)}
<h2 id="warnings">Warnings</h2>
{warnings_section(details)}
<h2 id="vulnerabilities">Vulnerabilities</h2>
{vulnerabilities_section(details)}
</main></body></html>
"""


def render(data):
    pr_rows = []
    for r in (r for r in data["repos"] if r["report"] == "prs"):
        pr_rows.append(
            f'<tr>{repo_cell(r)}<td class="num">{pr_link(r, "other")}</td>'
            f'<td class="num">{pr_link(r, "dependabot")}</td><td class="num">{pr_link(r, "renovate")}</td></tr>'
        )
    rows = []
    for r in (r for r in data["repos"] if r["report"] == "full"):
        q = r["quality"]
        rows.append(
            f"<tr>{repo_cell(r)}"
            f"<td>{cell_release(r)}</td><td>{cell_drift(r)}</td><td class=\"prs\">{cell_prs(r)}</td><td>{cell_frameworks(r)}</td>"
            f"<td>{cell_tests(q)}</td><td>{drill(r, 'coverage', cell_coverage(q, data['coverageThreshold']))}</td>"
            f"<td>{drill(r, 'warnings', cell_warnings(q))}</td><td>{drill(r, 'vulnerabilities', cell_vulns(q, r.get('qualityDetails')))}</td>"
            "</tr>"
        )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Repository report</title><style>{STYLE}</style>{THEME_SCRIPT}</head>
<body><main>
<header><h1>Repository report</h1>{THEME_TOGGLE}</header>
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
{f'''<h2>Open pull requests</h2>
<div class="wrap fit"><table class="narrow">
<thead><tr><th>Repository</th><th>Open</th><th>Dependabot</th><th>Renovate</th></tr></thead>
<tbody>
{chr(10).join(pr_rows)}
</tbody></table></div>''' if pr_rows else ""}
</main></body></html>
"""


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    config = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    data = collect(config)
    for r in data["repos"]:
        if r.get("qualityDetails"):
            (out / page_name(r["repo"])).write_text(render_repo_page(r, data), encoding="utf-8")
    public = {**data, "repos": [{k: v for k, v in r.items() if k != "qualityDetails"} for r in data["repos"]]}
    (out / "data.json").write_text(json.dumps(public, indent=2) + "\n", encoding="utf-8")
    (out / "index.html").write_text(render(data), encoding="utf-8")


if __name__ == "__main__":
    main()
