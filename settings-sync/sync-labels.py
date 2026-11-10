#!/usr/bin/env python3
"""Creates or updates the labels in settings-sync/labels.yml in every repo of one owner.

Usage: sync-labels.py <owner> [--dry-run]   (GH_TOKEN: the wshno-repo-settings installation token)

The bulk settings action can't manage labels, so the Repository settings sync workflow runs this
after it. A label is created when missing and patched when its colour or description differs (name
matched case-insensitively, as GitHub does). Labels not in the file are left alone.
"""
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

import yaml

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


def existing_labels(repo):
    labels, page = [], 1
    while True:
        batch = call("GET", f"/repos/{repo}/labels?per_page=100&page={page}")
        labels += batch
        if len(batch) < 100:
            return {label["name"].lower(): label for label in labels}
        page += 1


def main():
    owner = sys.argv[1]
    dry_run = "--dry-run" in sys.argv
    wanted = yaml.safe_load((ROOT / "labels.yml").read_text(encoding="utf-8"))["labels"]
    repos_file = (ROOT / f"repos-{owner}.yml").read_text(encoding="utf-8")
    repos = re.findall(rf"^ *- repo: ({re.escape(owner)}/[A-Za-z0-9._-]+) *$", repos_file, re.M)
    if not repos:
        sys.exit(f"No repos for {owner}")

    summary = [f"### Labels: {owner}{' (dry run)' if dry_run else ''}", "", "| repo | created | updated |", "|---|---|---|"]
    failed = False
    for repo in repos:
        created, updated = [], []
        try:
            have = existing_labels(repo)
            for label in wanted:
                name, color = label["name"], str(label["color"]).lower()
                description = label.get("description", "")
                cur = have.get(name.lower())
                if cur is None:
                    created.append(name)
                    if not dry_run:
                        call("POST", f"/repos/{repo}/labels", {"name": name, "color": color, "description": description})
                elif cur["color"].lower() != color or (cur.get("description") or "") != description or cur["name"] != name:
                    updated.append(name)
                    if not dry_run:
                        call("PATCH", f"/repos/{repo}/labels/{urllib.parse.quote(cur['name'], safe='')}",
                             {"new_name": name, "color": color, "description": description})
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
        print(f"{repo}: created {created or '-'}, updated {updated or '-'}")
        summary.append(f"| {repo} | {', '.join(created) or '-'} | {', '.join(updated) or '-'} |")

    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("\n".join(summary) + "\n")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
