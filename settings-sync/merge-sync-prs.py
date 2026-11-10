#!/usr/bin/env python3
"""Merges the open sync PRs (author wshno-repo-settings[bot]) of one owner's repos once they're green.

Usage: merge-sync-prs.py <owner> [--wait-minutes N]   (GH_TOKEN: the wshno-repo-settings token)

The sync PRs only carry what was already reviewed and merged in wshno/devops, so they don't wait for
another review: the App is a bypass actor (pull_request mode) on "main baseline (synced)", which is
what lets the merge past code-owner review. Everything else still applies: a PR is merged only when
every check run and commit status on its head commit has finished, none failed, and at least one
exists; the merge is pinned to that commit (sha), so a newer push is never merged unchecked. PRs that
are still running when the wait ends are left for the next run; failing ones are left for a human.
"""
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent
API = "https://api.github.com"
BOT = "wshno-repo-settings[bot]"
OK = {"success", "skipped", "neutral"}


def call(method, path, body=None):
    req = urllib.request.Request(
        API + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read() or b"null")


def state(repo, sha):
    """'green', 'pending' or 'failed: <names>' for every check run and status on sha."""
    runs = call("GET", f"/repos/{repo}/commits/{sha}/check-runs?per_page=100")["check_runs"]
    statuses = call("GET", f"/repos/{repo}/commits/{sha}/status")["statuses"]
    if not runs and not statuses:
        return "pending"  # nothing reported yet (or nothing ran): never merge on zero checks
    if any(r["status"] != "completed" for r in runs) or any(s["state"] == "pending" for s in statuses):
        return "pending"
    bad = [r["name"] for r in runs if r["conclusion"] not in OK] + [s["context"] for s in statuses if s["state"] != "success"]
    return f"failed: {', '.join(bad)}" if bad else "green"


def main():
    owner = sys.argv[1]
    wait = int(sys.argv[sys.argv.index("--wait-minutes") + 1]) if "--wait-minutes" in sys.argv else 30
    repos_file = (ROOT / f"repos-{owner}.yml").read_text(encoding="utf-8")
    repos = re.findall(rf"^ *- repo: ({re.escape(owner)}/[A-Za-z0-9._-]+) *$", repos_file, re.M)

    todo = {}
    for repo in repos:
        for pr in call("GET", f"/repos/{repo}/pulls?state=open&per_page=100"):
            if pr["user"]["login"] == BOT:
                todo[(repo, pr["number"])] = pr["head"]["sha"]
    print(f"Open sync PRs: {', '.join(f'{r}#{n}' for r, n in todo) or 'none'}")

    results = {}
    deadline = time.time() + wait * 60
    while todo:
        for key, sha in list(todo.items()):
            repo, number = key
            try:
                s = state(repo, sha)
                if s == "green":
                    call("PUT", f"/repos/{repo}/pulls/{number}/merge", {"merge_method": "squash", "sha": sha})
                    results[key] = "merged"
                elif s.startswith("failed"):
                    results[key] = s
                else:
                    continue
            except urllib.error.HTTPError as e:
                results[key] = f"error {e.code}: {e.read().decode(errors='replace')[:200]}"
            print(f"{repo}#{number}: {results[key]}")
            del todo[key]
        if not todo or time.time() > deadline:
            break
        time.sleep(30)
    for key in todo:
        results[key] = "still running; next run"
        print(f"{key[0]}#{key[1]}: {results[key]}")

    if os.environ.get("GITHUB_STEP_SUMMARY"):
        lines = [f"### Sync PRs: {owner}", "", "| PR | result |", "|---|---|"]
        lines += [f"| {r}#{n} | {res} |" for (r, n), res in sorted(results.items())] or ["| none | |"]
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    sys.exit(1 if any(v.startswith(("failed", "error")) for v in results.values()) else 0)


if __name__ == "__main__":
    main()
