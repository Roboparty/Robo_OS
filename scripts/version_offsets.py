#!/usr/bin/env python3

# SPDX-License-Identifier: GPL-3.0
# Copyright (C) 2026 wentywenty
"""Report tag/commit and release/public-APT version offsets for routing.yaml repos."""

import gzip
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

import yaml

API = "https://api.github.com"
APT = "https://apt.roboparty.com"
SUITES = ("common", "robopi0", "robopi1", "robopi2", "robopi3", "robopi4", "x86")
ARCHES = ("arm64", "amd64", "all")
DEB_RE = re.compile(r"^([^_]+)_([^_]+)_(all|arm64|amd64|armhf|i386)(?:__.*)?\.deb$")


class ApiError(RuntimeError):
    pass


def gh_api(path, token):
    url = f"{API}{path}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "roboparty-version-offsets/1.0")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise ApiError(f"{path}: HTTP {exc.code}") from exc
    except Exception as exc:
        raise ApiError(f"{path}: {exc}") from exc


def version_key(value):
    """Sort key that handles both Debian revisions and semver prereleases.

    ``1.0.0~rc1`` and ``1.0.0-rc1`` sort before ``1.0.0``, while the numeric
    Debian revision ``1.0.0-1`` sorts after it.
    """
    tokens = re.findall(r"\d+|[A-Za-z]+|[~]|[.+\-_]", value.lstrip("vV"))
    key = []
    for index, token in enumerate(tokens):
        if token.isdigit():
            key.append((3, int(token), ""))
        elif token == "~":
            key.append((0, 0, ""))
        elif token in ".+_":
            key.append((2, 0, ""))
        elif token == "-":
            following = tokens[index + 1] if index + 1 < len(tokens) else ""
            if following == "~" or following.isalpha():
                key.append((0, 0, ""))
            else:
                key.append((2, 0, "-"))
        else:
            previous = tokens[index - 1] if index else ""
            if previous in ("-", "~"):
                key.append((0, 0, token.lower()))
            else:
                key.append((3, 0, token.lower()))
    key.append((1, 0, ""))
    return key


def is_version_tag(name):
    return bool(re.match(r"^v?\d", name))


def graphql(query, variables, token):
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request("https://api.github.com/graphql", data=body, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "roboparty-version-offsets/1.0")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise ApiError(f"GraphQL: HTTP {exc.code}") from exc
    except Exception as exc:
        raise ApiError(f"GraphQL: {exc}") from exc
    if payload.get("errors"):
        raise ApiError(f"GraphQL: {payload['errors']}")
    return payload.get("data") or {}


def latest_tag_rest(repo, token):
    best = None
    for page in range(1, 11):
        tags = gh_api(f"/repos/{repo}/tags?per_page=100&page={page}", token)
        if not tags:
            break
        for tag in tags:
            name = tag.get("name", "")
            if not is_version_tag(name):
                continue
            if best is None or version_key(name) > version_key(best["name"]):
                best = tag
        if len(tags) < 100:
            break
    return best


def latest_tag(repo, token):
    owner, name = repo.split("/", 1)
    query = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    refs(refPrefix: "refs/tags/", first: 100,
         orderBy: {field: TAG_COMMIT_DATE, direction: DESC}) {
      nodes {
        name
        target {
          __typename
          ... on Commit { oid }
          ... on Tag { target { ... on Commit { oid } } }
        }
      }
    }
  }
}
"""
    try:
        data = graphql(query, {"owner": owner, "name": name}, token)
        nodes = (((data.get("repository") or {}).get("refs") or {}).get("nodes") or [])
        # GraphQL returns a single page ordered by commit date. If it is
        # truncated, use REST so both paths agree on "highest version" semantics.
        if len(nodes) < 100:
            best = None
            for node in nodes:
                tag_name = node.get("name", "")
                if not is_version_tag(tag_name):
                    continue
                target = node.get("target") or {}
                oid = target.get("oid")
                if not oid:
                    oid = (target.get("target") or {}).get("oid")
                if oid and (
                    best is None or version_key(tag_name) > version_key(best["name"])
                ):
                    best = {"name": tag_name, "commit": {"sha": oid}}
            return best
    except ApiError as exc:
        print(f"warning: {repo}: {exc}; falling back to tag-name ordering", file=sys.stderr)
    return latest_tag_rest(repo, token)


def resolve_commit(repo, ref, token):
    quoted = urllib.parse.quote(ref, safe="")
    data = gh_api(f"/repos/{repo}/commits/{quoted}", token)
    return data.get("sha") if data else None


def compare_commits(repo, base, head, token):
    if base == head:
        return (0, 0)
    data = gh_api(f"/repos/{repo}/compare/{base}...{head}", token)
    if not data:
        return None
    return (int(data.get("ahead_by", 0)), int(data.get("behind_by", 0)))


def release_packages(release):
    packages = {}
    for asset in release.get("assets", []):
        match = DEB_RE.match(asset.get("name", ""))
        if not match:
            continue
        package, version, _arch = match.groups()
        current = packages.get(package)
        if current is None or version_key(version) > version_key(current):
            packages[package] = version
    return packages


def fetch_apt_versions():
    versions = {}
    for suite in SUITES:
        for arch in ARCHES:
            url = f"{APT}/dists/{suite}/main/binary-{arch}/Packages.gz"
            try:
                with urllib.request.urlopen(url, timeout=30) as resp:
                    text = gzip.decompress(resp.read()).decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    continue
                print(f"warning: {url}: HTTP {exc.code}", file=sys.stderr)
                continue
            except Exception as exc:
                print(f"warning: {url}: {exc}", file=sys.stderr)
                continue
            for stanza in text.split("\n\n"):
                package = version = None
                for line in stanza.splitlines():
                    if line.startswith("Package: "):
                        package = line[9:].strip()
                    elif line.startswith("Version: "):
                        version = line[9:].strip()
                if package and version:
                    current = versions.get(package)
                    if current is None or compare_versions(version, current) > 0:
                        versions[package] = version
    return versions


def compare_versions(left, right):
    if left == right:
        return 0
    if subprocess.run(["sh", "-c", "command -v dpkg"], capture_output=True).returncode == 0:
        for op, result in (("lt", -1), ("eq", 0), ("gt", 1)):
            if subprocess.run(
                ["dpkg", "--compare-versions", left, op, right],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode == 0:
                return result
    left_key, right_key = version_key(left), version_key(right)
    return (left_key > right_key) - (left_key < right_key)


def apt_status(release_version, apt_version):
    if not release_version or not apt_version:
        return "-"
    result = compare_versions(apt_version, release_version)
    if result == 0:
        return "same"
    return "APT behind" if result < 0 else "APT ahead"


def format_offset(offset):
    if offset is None:
        return "?"
    ahead, behind = offset
    if ahead == 0 and behind == 0:
        return "0"
    return f"+{ahead}/-{behind}"


def format_packages(packages, versions=None):
    if not packages:
        return "-"
    items = []
    for package in sorted(packages):
        version = packages[package]
        if versions is not None:
            items.append(f"{package}={versions.get(package, '-')}")
        else:
            items.append(f"{package}={version}")
    return "<br>".join(items)


def collect(repo_full, token, apt_versions):
    name = repo_full.split("/")[-1]
    info = gh_api(f"/repos/{repo_full}", token)
    if not info:
        return {"repo": name, "error": "repo not found"}
    branch = info.get("default_branch", "main")
    head = resolve_commit(repo_full, branch, token)
    tag = latest_tag(repo_full, token)
    tag_name = tag.get("name") if tag else None
    tag_sha = tag.get("commit", {}).get("sha") if tag else None
    tag_offset = compare_commits(repo_full, tag_sha, head, token) if tag_sha else None

    release = gh_api(f"/repos/{repo_full}/releases/latest", token)
    release_name = release.get("tag_name") if release else None
    release_sha = resolve_commit(repo_full, release_name, token) if release_name else None
    release_offset = (
        compare_commits(repo_full, release_sha, head, token) if release_sha else None
    )
    packages = release_packages(release) if release else {}

    rows = []
    for package in sorted(packages):
        release_version = packages[package]
        apt_version = apt_versions.get(package)
        rows.append(
            {
                "package": package,
                "release": release_version,
                "apt": apt_version or "-",
                "status": apt_status(release_version, apt_version),
            }
        )
    return {
        "repo": name,
        "branch": branch,
        "head": (head or "?")[:9],
        "tag": tag_name or "-",
        "tag_offset": tag_offset,
        "release": release_name or "-",
        "release_offset": release_offset,
        "packages": rows,
        "error": None,
    }


def render_markdown(results,total_repos):
    lines = [
        "| Repo | 公网 HEAD | 最新 tag | tag→HEAD | 最新 release | release→HEAD | Release deb | 公网 APT | Release→APT |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    tag_drift = release_drift = apt_drift = missing = errors = 0
    for item in results:
        if item.get("error"):
            errors += 1
            lines.append(
                f"| {item['repo']} | - | - | - | - | - | - | - | {item['error']} |"
            )
            continue
        packages = item["packages"]
        release_versions = {row["package"]: row["release"] for row in packages}
        apt_versions = {row["package"]: row["apt"] for row in packages}
        statuses = []
        for row in packages:
            if row["status"] == "APT behind":
                apt_drift += 1
            elif row["status"] == "-":
                missing += 1
            statuses.append(f"{row['package']}: {row['status']}")
        offset = item["tag_offset"]
        if offset and offset[0] > 0:
            tag_drift += 1
        offset = item["release_offset"]
        if offset and offset[0] > 0:
            release_drift += 1
        lines.append(
            "| {repo} | {head} | {tag} | {tag_offset} | {release} | {release_offset} | "
            "{release_debs} | {apt_debs} | {status} |".format(
                repo=item["repo"],
                head=item["head"],
                tag=item["tag"],
                tag_offset=format_offset(item["tag_offset"]),
                release=item["release"],
                release_offset=format_offset(item["release_offset"]),
                release_debs=format_packages(release_versions),
                apt_debs=format_packages(release_versions, apt_versions),
                status="<br>".join(statuses) if statuses else "-",
            )
        )
    lines.append("")
    lines.append(
        f"Summary: {len(results)} repos, {tag_drift} with unreleased commits since tag, "
        f"{release_drift} with commits since latest release, {apt_drift} APT packages behind, "
        f"{missing} missing in APT, {errors} API errors."
    )
    return "\n".join(lines)


def main():
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token or token.startswith("${"):
        print("GH_TOKEN not set", file=sys.stderr)
        return 1

    routing_path = sys.argv[1] if len(sys.argv) > 1 else "routing.yaml"
    with open(routing_path) as stream:
        routing = yaml.safe_load(stream)
    repos = routing["repos"]

    apt_versions = fetch_apt_versions()
    results = []
    for repo in repos:
        try:
            results.append(collect(repo, token, apt_versions))
        except ApiError as exc:
            print(f"warning: {repo}: {exc}", file=sys.stderr)
            results.append({"repo": repo.split("/")[-1], "error": str(exc)})

    markdown = render_markdown(results, len(repos))
    print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
