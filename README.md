# devops

Cross-repository overview and shared standards for the wshno and hwinther repos:

- [Repository standards sync](#repository-standards-sync): one baseline of settings, rulesets, labels and files, pushed
  to every repo.
- [Repository report](#repository-report): a page with one row per repository.

## Repository standards sync

[.github/workflows/repo-settings.yml](.github/workflows/repo-settings.yml) applies what is merged here to the repos
listed in [settings-sync/repos-hwinther.yml](settings-sync/repos-hwinther.yml) and
[settings-sync/repos-wshno.yml](settings-sync/repos-wshno.yml) (one file per owner, because the GitHub App token is
minted per owner). The header of each file explains which repo gets what and why.

### What it manages

| What                                                                           | Where it's defined                                                                                       | How it reaches the repo                                                                                                             |
| ------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| Merge options, auto-merge, branch deletion, secret scanning, Dependabot alerts | inputs of the _Sync settings_ step in `repo-settings.yml`                                                | applied directly by [bulk-github-repo-settings-sync-action](https://github.com/joshjohanning/bulk-github-repo-settings-sync-action) |
| Rulesets (`main baseline`, `pr-build-required`, `e2e-required`)                | [settings-sync/files/rulesets/](settings-sync/files/rulesets/), per repo `rulesets-file`                 | applied directly, next to the repo's own rulesets                                                                                   |
| Environments (`devops-bot`)                                                    | [settings-sync/files/environments/](settings-sync/files/environments/)                                   | applied directly                                                                                                                    |
| Labels                                                                         | [settings-sync/labels.yml](settings-sync/labels.yml)                                                     | [sync-labels.py](settings-sync/sync-labels.py): created or updated, never deleted                                                   |
| Issues on, 30 days of artifact and log retention, `RUNS_ON` (runs-on.yml)      | [sync-extras.py](settings-sync/sync-extras.py)                                                           | applied directly                                                                                                                    |
| Files: workflows, scanner configs, CODEOWNERS, release notes config            | [settings-sync/files/](settings-sync/files/), per repo `file-sync`                                       | one pull request per repo, `chore: sync repository standards`                                                                       |
| `.github/dependabot.yml`                                                       | built into [settings-sync/files/dependabot/](settings-sync/files/dependabot/), per repo `dependabot-yml` | a pull request of its own                                                                                                           |

The synced files start with `# Synced from wshno/devops …: change it there`. A change made in a target repo is
overwritten by the next sync, so change the file here instead. A repo that needs something different keeps its own file
and is left out of that entry (skylens keeps its own `tag-and-release.yml`, for example).

Some Dependabot settings have no API, so they can't be synced as settings:

- **Grouped security updates:** the synced `dependabot.yml` gives every ecosystem a `security-updates` group
  (`applies-to: security-updates`), which gives one security PR per ecosystem across its directories.
- **Malware alerts:** turned on by hand in each repo (Settings → Advanced Security → Dependabot → Malware alerts).

### When it runs

- **Daily at 05:17 UTC** it applies everything: what is merged here reaches the repos within a day.
- **By hand** (Actions → _Repository settings sync_ → _Run workflow_) `dry_run` is on by default: the job summary lists
  what would change. Run it again with `dry_run` off to apply.

After the sync, the **merge** job merges the open sync PRs ([merge-sync-prs.py](settings-sync/merge-sync-prs.py)):

- A PR is merged only when every check run and commit status on its head commit has finished, none failed, and at least
  one exists. The merge is pinned to that commit, so a newer push is never merged unchecked.
- The sync PRs carry only what was already reviewed and merged here, so they don't wait for another review. The App is a
  bypass actor on `main baseline (synced)`, which lets the merge past code-owner review.
- PRs still running after 30 minutes are left for the next run. **Failing PRs are left for a human** and make the merge
  job (and so the run) fail. Typical causes: the repo's CI is broken on main too, or a private repo has hit its Actions
  storage quota (`Artifact storage quota has been hit` in the log; it clears as artifacts expire). Fix the cause and
  re-run the failed checks; the next sync run merges the PR. A PR can also be merged by hand.

### Changing something

1. Edit the file under [settings-sync/files/](settings-sync/files/), or the repo's entry in `repos-<owner>.yml`.
   To give a file to another repo, add its YAML anchor (`- *format`, for example) to that repo's `file-sync` list.
2. Dependabot configs: edit the blocks in [settings-sync/dependabot/build.py](settings-sync/dependabot/build.py), run
   `python settings-sync/dependabot/build.py` and commit the built files too
   ([dependabot-templates.yml](.github/workflows/dependabot-templates.yml) fails when they don't match).
3. Run `npx prettier --write` on what you changed. The synced files land in repos that run `prettier --check` over
   `.github/`, so [format.yml](.github/workflows/format.yml) checks them here first.
4. Open a PR with a Conventional Commits title (`feat(settings-sync): …`, `chore(settings-sync): …`), merge it, then
   wait for the daily run or run the workflow by hand.

A new repo: install the wshno-repo-settings App on it, add an entry to `repos-<owner>.yml`, and add it to rulesets that
depend on its workflows only once those workflows exist (see the notes on `e2e-required` in the file header).

### Keeping the pins current

The synced workflows pin actions and `hwinther/reusable-workflows` by commit SHA.
[Renovate](renovate.json) bumps those pins here, in `settings-sync/files/workflows/` as well as this repo's own workflows,
so the next sync carries them to every repo:

- **hwinther/reusable-workflows** right away: the app repos move to a new release as soon as it is out, and a template
  that lagged behind would roll them back on the next sync.
- **Third-party actions** once a week (Fridays), after a 7-day release age.

The target repos' Dependabot waits 14 days for GitHub Actions, so the bump normally arrives through the sync first.

### Access

The workflow uses the **wshno-repo-settings** GitHub App (Administration, Contents, Pull requests and Workflows: write;
Checks: read). It can change rulesets and branch protection, so:

- its key (secret `REPO_SETTINGS_PRIVATE_KEY`, variable `REPO_SETTINGS_CLIENT_ID`) lives only in the `repo-settings`
  environment, which only `main` can use;
- each run mints one token per owner, limited to the listed repos;
- the GitOps repos (hwinther/proxmox, hwinther/private-apps) and hwinther/reusable-workflows are not managed, and the App
  isn't installed there.

## Repository report

A page, refreshed hourly in the daytime, with one row per repository in [repos.yaml](repos.yaml):

| Column                                     | Source                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| ------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Latest release                             | the latest GitHub release and its age                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| Unreleased on main                         | commits and changed files/lines from that release to the default branch, without `.github/` and YAML files in the root, such as `.poutine.yml` (CI and tool config needs no release)                                                                                                                                                                                                                                                                                                                                                                 |
| Open PRs                                   | open pull requests, with Dependabot and Renovate counted separately                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| Open issues                                | open issues without Renovate's Dependency Dashboard; the ones a workflow opened (a scheduled run reporting a failure) in red. Without Issues: read on the automation app only the count (open_issues_count minus open PRs). Also in the open-PRs table.                                                                                                                                                                                                                                                                                              |
| Branches without PR                        | branches with no open pull request: **no PR** (commits of their own, never a PR: open one or delete it), **after merge** (pushed to after its PR merged, which recreates the deleted branch: check nothing was lost), **closed PR** (closed unmerged) and **to delete** (PR merged, or nothing missing from the default branch; squash merges leave merged branches ahead, so the PR state decides). Links to a per-repo page; `ignore_branches` in repos.yaml leaves long-lived branches out. Also in the open-PRs table.                           |
| Frameworks                                 | Node, Python, .NET and npm packages (React, TypeScript, React Router, Vite, Expo, React Native) against the `minimums` in repos.yaml: green at or above, red below. Python and .NET are also red once [endoflife.date](https://endoflife.date) marks them EOL. Node and Python are read from every place they're set (package.json `engines`, `.nvmrc`, pyproject `requires-python`, `.python-version`, Dockerfile `FROM`, workflow `node_version` / `python_version`), and the lowest one counts. ↑ marks an npm package with a newer major on npm. |
| Tests, coverage, warnings, vulnerabilities | `summary.json` from the repo's newest `quality-summary` workflow artifact on the default branch; line coverage per component plus **combined** (unit + e2e, a line covered by either) when the summary has it (see [hwinther/reusable-workflows docs/quality-artifact.md](https://github.com/hwinther/reusable-workflows/blob/main/docs/quality-artifact.md))                                                                                                                                                                                        |

[report/generate.py](report/generate.py) builds `index.html` and `data.json`.
[.github/workflows/report.yml](.github/workflows/report.yml) runs it hourly from 06:17 to 20:17 UTC, once at 01:17 UTC and on changes
here. The result goes out as a ConfigMap in the signed OCI artifact
`ghcr.io/wshno/devops/report:latest`. The homelab cluster reconciles it with Flux and serves it
behind Authelia (hwinther/proxmox, `clusters/production/apps/devops-report`).

### Access

The workflow reads the repos through the automation GitHub App: repository variable
`AUTOMATION_APP_CLIENT_ID` (the app's Client ID, `Iv23…`, not its numeric App ID) and secret
`AUTOMATION_APP_PRIVATE_KEY`. The variable is set on this repo; the secret lives in the `report`
environment, which only `main` can use. The app is installed on both owners. Each run mints one
token per owner, limited to the repos in repos.yaml and to read access (contents, pull requests,
actions, metadata, issues).

### Running it locally

```sh
python -m pip install pyyaml
GITHUB_TOKEN=$(gh auth token) python report/generate.py repos.yaml out
```

`GITHUB_TOKEN_<OWNER>` (e.g. `GITHUB_TOKEN_WSHNO`) overrides the token for one owner.
