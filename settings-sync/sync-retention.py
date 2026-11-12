#!/usr/bin/env python3
"""Sets the Actions artifact and log retention in every repo of one owner.

Usage: sync-retention.py <owner> <days> [--dry-run]   (GH_TOKEN: the wshno-repo-settings installation token)

The bulk settings action can't manage it, so the Repository settings sync workflow runs this after
it. The repo setting is both the default and the ceiling: an upload asking for more (quality-artifact
asks for 90) gets this many days. GitHub's default is 90; on a private repo on the free plan, artifacts
count against the 500 MB Actions storage quota, and 90 days of reports and SBOMs filled it.
"""
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent
API = "https://api.github.com"


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


def main():
    owner, days = sys.argv[1], int(sys.argv[2])
    dry_run = "--dry-run" in sys.argv
    repos_file = (ROOT / f"repos-{owner}.yml").read_text(encoding="utf-8")
    repos = re.findall(rf"^ *- repo: ({re.escape(owner)}/[A-Za-z0-9._-]+) *$", repos_file, re.M)
    if not repos:
        sys.exit(f"No repos for {owner}")

    summary = [f"### Artifact and log retention: {owner}{' (dry run)' if dry_run else ''}", "",
               "| repo | before | after |", "|---|---|---|"]
    failed = False
    for repo in repos:
        try:
            current = call("GET", f"/repos/{repo}/actions/permissions/artifact-and-log-retention")["days"]
            if current != days and not dry_run:
                call("PUT", f"/repos/{repo}/actions/permissions/artifact-and-log-retention", {"days": days})
            summary.append(f"| {repo} | {current} | {days if current != days else 'unchanged'} |")
        except urllib.error.HTTPError as e:
            failed = True
            print(f"::error::{repo}: {e.code} {e.reason}: {e.read().decode(errors='replace')[:300]}")
            summary.append(f"| {repo} | failed: {e.code} | |")
            continue
        except urllib.error.URLError as e:
            failed = True
            print(f"::error::{repo}: {e.reason}")
            summary.append(f"| {repo} | failed: {e.reason} | |")
            continue
        print(f"{repo}: {current} -> {days if current != days else 'unchanged'}")

    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("\n".join(summary) + "\n")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
