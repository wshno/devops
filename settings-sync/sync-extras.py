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
- The Actions variable RUNS_ON, for the repos listed in runs-on.yml. The synced workflows run on
  `${{ vars.RUNS_ON || 'ubuntu-latest' }}`, so a private repo out of GitHub-hosted minutes can move
  them to its own runners. Only private repos may list a self-hosted label: on a public repo a pull
  request from a fork would run on the machine. Repos not in the file are left alone.
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


def runs_on_map(owner):
    """`owner/repo: label` lines from runs-on.yml (comments and blank lines ignored)."""
    wanted = {}
    for line in (ROOT / "runs-on.yml").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.fullmatch(r"([A-Za-z0-9._-]+/[A-Za-z0-9._-]+):\s*([A-Za-z0-9._-]+)", line)
        if not m:
            sys.exit(f"runs-on.yml: cannot parse {line!r}")
        if m.group(1).split("/")[0] == owner:
            wanted[m.group(1)] = m.group(2)
    return wanted


def sync_runs_on(repo, label, private, dry_run):
    """Sets the RUNS_ON Actions variable to label; returns the change for the summary."""
    if not private:
        raise ValueError(f"{repo} is public; refusing RUNS_ON={label}")
    try:
        before = call("GET", f"/repos/{repo}/actions/variables/RUNS_ON")["value"]
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        before = None
        if not dry_run:
            call("POST", f"/repos/{repo}/actions/variables", {"name": "RUNS_ON", "value": label})
    else:
        if before != label and not dry_run:
            call("PATCH", f"/repos/{repo}/actions/variables/RUNS_ON", {"name": "RUNS_ON", "value": label})
    return change(before or "unset", label)


def change(before, after):
    return "unchanged" if before == after else f"{before} -> {after}"


def main():
    owner, days = sys.argv[1], int(sys.argv[2])
    dry_run = "--dry-run" in sys.argv
    repos_file = (ROOT / f"repos-{owner}.yml").read_text(encoding="utf-8")
    repos = re.findall(rf"^ *- repo: ({re.escape(owner)}/[A-Za-z0-9._-]+) *$", repos_file, re.M)
    if not repos:
        sys.exit(f"No repos for {owner}")

    runs_on = runs_on_map(owner)
    unknown = set(runs_on) - set(repos)
    if unknown:
        sys.exit(f"runs-on.yml lists repos that repos-{owner}.yml does not manage: {sorted(unknown)}")
    summary = [f"### Issues, retention and RUNS_ON: {owner}{' (dry run)' if dry_run else ''}", "",
               "| repo | issues | artifact and log retention | RUNS_ON |", "|---|---|---|---|"]
    failed = False
    for repo in repos:
        try:
            info = call("GET", f"/repos/{repo}")
            issues = info["has_issues"]
            if not issues and not dry_run:
                call("PATCH", f"/repos/{repo}", {"has_issues": True})
            retention = call("GET", f"/repos/{repo}/actions/permissions/artifact-and-log-retention")["days"]
            if retention != days and not dry_run:
                call("PUT", f"/repos/{repo}/actions/permissions/artifact-and-log-retention", {"days": days})
            runs_on_change = (sync_runs_on(repo, runs_on[repo], info["private"], dry_run)
                              if repo in runs_on else "not managed")
        except ValueError as e:
            failed = True
            print(f"::error::{e}")
            summary.append(f"| {repo} | | | refused: public repo |")
            continue
        except urllib.error.HTTPError as e:
            failed = True
            print(f"::error::{repo}: {e.code} {e.reason}: {e.read().decode(errors='replace')[:300]}")
            summary.append(f"| {repo} | failed: {e.code} | | |")
            continue
        except urllib.error.URLError as e:
            failed = True
            print(f"::error::{repo}: {e.reason}")
            summary.append(f"| {repo} | failed: {e.reason} | | |")
            continue
        issues_change = change("off" if not issues else "on", "on")
        retention_change = change(retention, days)
        print(f"{repo}: issues {issues_change}, retention {retention_change}, RUNS_ON {runs_on_change}")
        summary.append(f"| {repo} | {issues_change} | {retention_change} | {runs_on_change} |")

    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("\n".join(summary) + "\n")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
