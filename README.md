# devops

Cross-repository overview for the wshno and hwinther repos.

## Repository report

A page, refreshed every 6 hours, with one row per repository in [repos.yaml](repos.yaml):

| Column                                     | Source                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| ------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Latest release                             | the latest GitHub release and its age                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| Unreleased on main                         | commits and changed files/lines from that release to the default branch, without `.github/` (workflow-only changes need no release)                                                                                                                                                                                                                                                                                                                                                                                                                  |
| Open PRs                                   | open pull requests, with Dependabot and Renovate counted separately                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| Branches without PR                        | branches with no open pull request: **no PR** (commits of their own, never a PR: open one or delete it), **closed PR** (closed unmerged) and **to delete** (PR merged, or nothing missing from the default branch; squash merges leave merged branches ahead, so the PR state decides). Links to a per-repo page; `ignore_branches` in repos.yaml leaves long-lived branches out. Also in the open-PRs table.                                                                                                                                        |
| Frameworks                                 | Node, Python, .NET and npm packages (React, TypeScript, React Router, Vite, Expo, React Native) against the `minimums` in repos.yaml: green at or above, red below. Python and .NET are also red once [endoflife.date](https://endoflife.date) marks them EOL. Node and Python are read from every place they're set (package.json `engines`, `.nvmrc`, pyproject `requires-python`, `.python-version`, Dockerfile `FROM`, workflow `node_version` / `python_version`), and the lowest one counts. ↑ marks an npm package with a newer major on npm. |
| Tests, coverage, warnings, vulnerabilities | `summary.json` from the repo's newest `quality-summary` workflow artifact on the default branch (see [hwinther/reusable-workflows docs/quality-artifact.md](https://github.com/hwinther/reusable-workflows/blob/main/docs/quality-artifact.md))                                                                                                                                                                                                                                                                                                      |

[report/generate.py](report/generate.py) builds `index.html` and `data.json`.
[.github/workflows/report.yml](.github/workflows/report.yml) runs it every 6 hours (04:30, 10:30, 16:30, 22:30 UTC) and on changes
here. The result goes out as a ConfigMap in the signed OCI artifact
`ghcr.io/wshno/devops/report:latest`. The homelab cluster reconciles it with Flux and serves it
behind Authelia (hwinther/proxmox, `clusters/production/apps/devops-report`).

### Access

The workflow reads the repos through the automation GitHub App: repository variable
`AUTOMATION_APP_CLIENT_ID` (the app's Client ID, `Iv23…`, not its numeric App ID) and secret
`AUTOMATION_APP_PRIVATE_KEY`. Both are set on this repo, because a private repo on the free plan
can't read organization secrets. The app is installed on both owners. Each run mints one
token per owner, limited to the repos in repos.yaml and to read access (contents, pull requests,
actions, metadata).

### Running it locally

```sh
python -m pip install pyyaml
GITHUB_TOKEN=$(gh auth token) python report/generate.py repos.yaml out
```

`GITHUB_TOKEN_<OWNER>` (e.g. `GITHUB_TOKEN_WSHNO`) overrides the token for one owner.
