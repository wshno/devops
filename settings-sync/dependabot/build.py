#!/usr/bin/env python3
"""Builds the synced Dependabot configs (settings-sync/files/dependabot/<stack>.yml).

One file per combination of ecosystems; every repo with that combination gets the same file, synced
as .github/dependabot.yml (`dependabot-yml:` in settings-sync/repos-<owner>.yml). Directories don't
vary per repo: single-level wildcards, one per depth, find the manifests ("/**" is NOT recursive;
directories without a manifest are skipped, but a block that finds none at all fails). Tested on
hwinther/test 2026-11-10 (#1325, #1326).

The output is written the way Prettier formats YAML with @wshno/prettier-config (single quotes,
indented sequences): repos run `prettier --check` over .github/ too.

Edit the blocks here, run `python settings-sync/dependabot/build.py`, commit both. The Dependabot
templates workflow fails when the committed files differ from what this script writes (--check).
"""
import pathlib
import sys

OUT = pathlib.Path(__file__).resolve().parents[1] / "files" / "dependabot"

HEADER = """# Synced from wshno/devops settings-sync/files/dependabot/{name}.yml (built by
# settings-sync/dependabot/build.py): change it there. Stack: {stack}.
#
# Directories are single-level wildcards, one per depth ('/', '/*', '/*/*'); directories without a
# manifest are skipped. Cooldown: 7 days, 14 for GitHub Actions, so the synced workflows are updated
# in wshno/devops (Renovate, 7 days) and synced before Dependabot here proposes the same bump.
# Groups: a dependency goes to the FIRST group that matches; groups that match nothing are harmless.
# Commit messages and PR titles are Conventional Commits, chore(deps): / chore(deps-dev):, so the
# Type label workflow labels them and tag-and-release counts them as patch.

version: 2
updates:
"""


def q(value):
    """A single-quoted YAML scalar."""
    return "'" + value.replace("'", "''") + "'"


def seq(items, indent):
    return "".join(f"{' ' * indent}- {q(i)}\n" for i in items)


DEEP = ["/", "/*", "/*/*"]
# npm workspaces: the members (apps/*, packages/*) are at depth 2 and must not become projects of
# their own (no lockfile there; the root's covers them).
SHALLOW = ["/", "/*"]


def block(ecosystem, dirs, cooldown=7, extra="", interval="weekly"):
    schedule = "      interval: 'daily'\n" if interval == "daily" else "      interval: 'weekly'\n      day: 'sunday'\n"
    return (f"  - package-ecosystem: {q(ecosystem)}\n"
            "    directories:\n" + seq(dirs, 6)
            + "    open-pull-requests-limit: 20\n"
            "    schedule:\n" + schedule
            + "    cooldown:\n"
            f"      default-days: {cooldown}\n"
            "    commit-message:\n"
            "      prefix: 'chore'\n"
            "      include: 'scope'\n"
            "    assignees:\n" + seq(["hwinther"], 6)
            + extra)


def groups(spec):
    out = "    groups:\n"
    for name, patterns, *excl in spec:
        out += f"      {name}:\n        patterns:\n" + seq(patterns, 10)
        if excl:
            out += "        exclude-patterns:\n" + seq(excl[0], 10)
    return out


def ignore(entries, comment=""):
    """entries: (name, None | ('update-types', [...]) | ('versions', [...]), optional comment line)."""
    out = "    ignore:\n"
    for name, cond, *note in entries:
        if note:
            out += "".join(f"      # {line}\n" for line in note[0])
        out += f"      - dependency-name: {q(name)}\n"
        if cond:
            key, values = cond
            out += f"        {key}: [{', '.join(q(v) for v in values)}]\n"
    return out


ACTIONS = block("github-actions", ["/", "/.github/actions/*"], cooldown=14, extra=groups([
    ("reusable-workflows", ["hwinther/reusable-workflows*"]),
]))

DOCKER_GROUPS = groups([("base-images", ["*"])])
DOCKER = block("docker", DEEP + ["/.github/actions/*"], extra=DOCKER_GROUPS)

NUGET = block("nuget", DEEP, extra=(
    "    # update-dotnet-lockfiles triggers the synced dependabot-update-dotnet-lockfiles.yml, which\n"
    "    # regenerates every project's packages.lock.json (Dependabot only rewrites the bumped project's,\n"
    "    # so projects referencing it fail the locked-mode restore, NU1004). Setting labels replaces\n"
    "    # Dependabot's defaults, so those are listed too.\n"
    "    labels:\n" + seq(["dependencies", ".NET", "update-dotnet-lockfiles"], 6)
    + groups([
        ("roslyn", ["Microsoft.CodeAnalysis.*"]),
        ("testing", ["xunit", "xunit.*", "nunit", "nunit.*", "NUnit*", "coverlet.*", "Microsoft.Testing.*",
                     "Microsoft.NET.Test.Sdk"]),
        ("microsoft", ["Microsoft.*", "System.*"], ["Microsoft.OpenApi"]),
        ("swashbuckle", ["Swashbuckle.*"]),
        ("opentelemetry", ["OpenTelemetry.*"]),
    ])))

NPM_IGNORES = [
    ("*", ("update-types", ["version-update:semver-patch"])),
    ("typescript", ("versions", [">=6.1"]),
     ["typescript-eslint, which @wshno/eslint-config builds on, supports TypeScript <6.1. Drop this",
      "once a typescript-eslint release widens that range."]),
]

NPM_GROUPS = [
    # The shared configs peer on eslint and prettier, so they move together.
    ("wshno", ["@wshno/*", "eslint", "prettier"]),
    ("react-router", ["react-router", "react-router-*", "@react-router/*"]),
    ("react", ["react", "react-*", "@types/react*"]),
    ("vite", ["vite", "vite-*", "@vitejs/*"]),
    ("testing", ["vitest", "@vitest/*", "@testing-library/*", "jsdom", "msw"]),
    ("playwright", ["@playwright/*", "playwright*"]),
    ("tanstack", ["@tanstack/*"]),
    ("opentelemetry", ["@opentelemetry/*"]),
    ("stryker", ["@stryker-mutator/*"]),
    ("astro", ["astro", "@astrojs/*"]),
    ("ui-libraries", ["bootstrap*", "d3*", "@visx/*", "@icon/*", "jquery*", "datatables*"]),
    ("eslint", ["eslint-*", "@eslint/*", "typescript-eslint", "@typescript-eslint/*", "@eslint-react/*",
                "*/eslint-plugin", "*/eslint-plugin-*", "globals"]),
    ("types", ["@types/*"]),
    ("build-tools", ["typescript", "typescript-*", "orval", "openapi-typescript-codegen"]),
    ("code-quality", ["@cspell/*"]),
    ("utilities", ["dotenv"]),
]


def npm(dirs, extra_ignores=(), extra_groups=()):
    return block("npm", dirs, extra=ignore(NPM_IGNORES + list(extra_ignores)) + groups(list(extra_groups) + NPM_GROUPS))


PIP_GROUPS = groups([("python", ["*"])])
PIP = block("pip", DEEP, extra=PIP_GROUPS)
GOMOD = block("gomod", DEEP)

# skylens: the Expo SDK pins React Native and its libraries; only an SDK upgrade
# (`npx expo install --fix`) may move them.
MINOR_MAJOR = ("update-types", ["version-update:semver-minor", "version-update:semver-major"])
EXPO_PINNED = ["expo", "expo-*", "@expo/*", "react", "react-dom", "react-native", "react-native-*",
               "@react-native/*", "@types/react", "jest-expo", "eslint-config-expo", "typescript"]
SKYLENS_IGNORES = (
    [(EXPO_PINNED[0], MINOR_MAJOR, ["Expo SDK pins: minor/major moves only with an Expo SDK upgrade (`npx expo install --fix`)."])]
    + [(n, MINOR_MAJOR) for n in EXPO_PINNED[1:]]
    + [("eslint", ("update-types", ["version-update:semver-major"]))])

RTL_SDR_PIP = block("pip", DEEP, extra=ignore([
    ("aioprometheus", None, ["aioprometheus stays on the version dump1090-exporter is written against."]),
]) + PIP_GROUPS)

STACKS = {
    "dotnet-node": ("GitHub Actions, Docker, NuGet, npm", [ACTIONS, DOCKER, NUGET, npm(DEEP)]),
    "node": ("GitHub Actions, Docker, npm", [ACTIONS, DOCKER, npm(DEEP)]),
    "node-workspaces": ("GitHub Actions, Docker, npm workspaces (root package.json)",
                        [ACTIONS, DOCKER, npm(SHALLOW)]),
    "python-node": ("GitHub Actions, Docker, pip, npm", [ACTIONS, DOCKER, PIP, npm(DEEP)]),
    "go": ("GitHub Actions, Docker, Go modules", [ACTIONS, DOCKER, GOMOD]),
    "npm-library": ("GitHub Actions, npm workspaces (root package.json)", [ACTIONS, npm(SHALLOW)]),
    # Exceptions, one repo each.
    "skylens": ("hwinther/skylens: GitHub Actions, Docker, NuGet, npm with the Expo SDK pins", [
        ACTIONS, DOCKER, NUGET,
        npm(DEEP, extra_ignores=SKYLENS_IGNORES, extra_groups=[("expo", ["expo", "expo-*", "@expo/*"])])]),
    "wsh-rtl-sdr": ("hwinther/wsh-rtl-sdr: GitHub Actions, Docker (daily: the feeder images track upstream), pip", [
        ACTIONS,
        block("docker", DEEP, extra=DOCKER_GROUPS, interval="daily"),
        RTL_SDR_PIP,
    ]),
}


def render(name):
    stack, blocks = STACKS[name]
    return HEADER.format(name=name, stack=stack) + "\n".join(blocks)


def main():
    check = "--check" in sys.argv
    OUT.mkdir(parents=True, exist_ok=True)
    stale = []
    for name in STACKS:
        path = OUT / f"{name}.yml"
        text = render(name)
        if check:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                stale.append(path.name)
        else:
            path.write_text(text, encoding="utf-8", newline="\n")
    extra = sorted(p.name for p in OUT.glob("*.yml") if p.stem not in STACKS)
    if check and (stale or extra):
        print("Out of date; run python settings-sync/dependabot/build.py and commit:", ", ".join(stale + extra))
        sys.exit(1)
    print("ok")


if __name__ == "__main__":
    main()
