# devops

Cross-repository overview for the wshno and hwinther repos.

## Repository report

A daily page with one row per repository in [repos.yaml](repos.yaml):

| Column | Source |
|---|---|
| Latest release | the latest GitHub release and its age |
| Unreleased on main | commits and changed files/lines from that release to the default branch, without `.github/` (workflow-only changes need no release) |
| Open PRs | open pull requests, with Dependabot and Renovate counted separately |
| Frameworks | `TargetFramework` from `*.csproj` / `Directory.Build.props` against [endoflife.date](https://endoflife.date/dotnet), and the npm packages in `npm_defaults` against npm's latest: green current, yellow behind on minor, red behind on major or EOL |
| Tests, coverage, warnings, vulnerabilities | `summary.json` from the repo's newest `quality-summary` workflow artifact on the default branch (see [hwinther/reusable-workflows docs/quality-artifact.md](https://github.com/hwinther/reusable-workflows/blob/main/docs/quality-artifact.md)) |

[report/generate.py](report/generate.py) builds `index.html` and `data.json`.
[.github/workflows/report.yml](.github/workflows/report.yml) runs it every morning and on changes
here. The result goes out as a ConfigMap in the signed OCI artifact
`ghcr.io/wshno/devops/report:latest`. The homelab cluster reconciles it with Flux and serves it
behind Authelia (hwinther/proxmox, `clusters/production/apps/devops-report`).

### Access

The workflow reads the repos through the automation GitHub App: org variable `AUTOMATION_APP_ID`
and secret `AUTOMATION_APP_PRIVATE_KEY`. The app is installed on both owners. Each run mints one
token per owner, limited to the repos in repos.yaml and to read access (contents, pull requests,
actions, metadata).

### Running it locally

```sh
python -m pip install pyyaml
GITHUB_TOKEN=$(gh auth token) python report/generate.py repos.yaml out
```

`GITHUB_TOKEN_<OWNER>` (e.g. `GITHUB_TOKEN_WSHNO`) overrides the token for one owner.
