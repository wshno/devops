#!/usr/bin/env python3
"""Applies the repository settings the bulk action can't manage, in every repo of one owner.

Usage: sync-extras.py <owner> <retention-days> [--dry-run]   (GH_TOKEN: the wshno-repo-settings installation token)

The Repository settings sync workflow runs this after the bulk action. Each setting is read first and
only written when it differs.

- Issues on. Scheduled workflows report failures as issues (e2e runs, for one), and the wshno/devops
  report counts the open ones; a repo with issues off loses both.
- Actions artifact and log retention. The repo setting is both the default and the ceiling: an upload
  asking for more (quality-artifact asks for 90) gets this many days. GitHub's default is 90; on a
  private repo on the free plan, artifacts count against the 500 MB Actions storage quota, and 90
  days of reports and SBOMs filled it.
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


def change(before, after):
    return "unchanged" if before == after else f"{before} -> {after}"


def main():
    owner, days = sys.argv[1], int(sys.argv[2])
    dry_run = "--dry-run" in sys.argv
    repos_file = (ROOT / f"repos-{owner}.yml").read_text(encoding="utf-8")
    repos = re.findall(rf"^ *- repo: ({re.escape(owner)}/[A-Za-z0-9._-]+) *$", repos_file, re.M)
    if not repos:
        sys.exit(f"No repos for {owner}")

    summary = [f"### Issues and retention: {owner}{' (dry run)' if dry_run else ''}", "",
               "| repo | issues | artifact and log retention |", "|---|---|---|"]
    failed = False
    for repo in repos:
        try:
            issues = call("GET", f"/repos/{repo}")["has_issues"]
            if not issues and not dry_run:
                call("PATCH", f"/repos/{repo}", {"has_issues": True})
            retention = call("GET", f"/repos/{repo}/actions/permissions/artifact-and-log-retention")["days"]
            if retention != days and not dry_run:
                call("PUT", f"/repos/{repo}/actions/permissions/artifact-and-log-retention", {"days": days})
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
        issues_change = change("off" if not issues else "on", "on")
        retention_change = change(retention, days)
        print(f"{repo}: issues {issues_change}, retention {retention_change}")
        summary.append(f"| {repo} | {issues_change} | {retention_change} |")

    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("\n".join(summary) + "\n")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
