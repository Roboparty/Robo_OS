#!/usr/bin/env python3

# SPDX-License-Identifier: GPL-3.0
# Copyright (C) 2026 RoboParty

"""Fail when a routed deb from manifest.json is missing from the APT repo."""

import argparse
import json
import subprocess
import sys


def list_suite(reprepro, apt_dir, suite):
    result = subprocess.run(
        [reprepro, "-b", apt_dir, "list", suite],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None, (result.stderr or result.stdout).strip()
    entries = set()
    for line in result.stdout.splitlines():
        prefix, separator, rest = line.partition(": ")
        if not separator:
            continue
        parts = prefix.split("|")
        arch = parts[2] if len(parts) >= 3 else ""
        fields = rest.split()
        if len(fields) >= 2:
            entries.add((fields[0], fields[1], arch))
    return entries, None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", nargs="?", default="/srv/apt-incoming/manifest.json")
    parser.add_argument("--apt-dir", default="/srv/apt")
    parser.add_argument("--reprepro", default="reprepro")
    args = parser.parse_args()

    try:
        with open(args.manifest, encoding="utf-8") as stream:
            manifest = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"❌ 无法读取 manifest: {args.manifest}: {exc}", file=sys.stderr)
        return 1
    if not isinstance(manifest, list):
        print("❌ manifest 格式错误: 期望数组", file=sys.stderr)
        return 1

    cache = {}
    missing = []
    for item in manifest:
        package = item.get("package")
        version = item.get("version")
        architecture = item.get("architecture")
        if not package or not version or not architecture:
            missing.append(f"{item.get('deb', '?')}: 缺少 package/version/architecture")
            continue
        for suite in item.get("suites", []):
            if suite not in cache:
                entries, error = list_suite(args.reprepro, args.apt_dir, suite)
                if error:
                    print(f"❌ reprepro list {suite} 失败: {error}", file=sys.stderr)
                    return 1
                cache[suite] = entries
            found = any(
                entry_package == package
                and entry_version == version
                and (architecture == "all" or entry_arch == architecture)
                for entry_package, entry_version, entry_arch in cache[suite]
            )
            if not found:
                missing.append(f"{suite}: {package} {version} {architecture}")

    if missing:
        print(f"❌ APT 缺少 {len(missing)} 个应入库的包:")
        for entry in missing:
            print(f"  - {entry}")
        return 1

    print(f"✅ 已验证 {len(manifest)} 个 deb，覆盖 {len(cache)} 个 suite")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
