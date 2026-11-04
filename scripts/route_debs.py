#!/usr/bin/env python3

# SPDX-License-Identifier: GPL-3.0
# Copyright (C) 2026 wentywenty

"""route_debs.py - 下载所有 repo 的 deb，集中后按 routing.yaml 分拣到 incoming/<suite>/"""

import fnmatch
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

import yaml

TMP_DIR = 'tmp_debs'
MANIFEST_FILE = 'manifest.json'
DOWNLOAD_TIMEOUT = int(os.environ.get('ROUTE_DEBS_TIMEOUT', '900'))
DOWNLOAD_RETRIES = max(1, int(os.environ.get('ROUTE_DEBS_RETRIES', '3')))
# Optional release-asset mirror, e.g. https://ghfast.top. The GitHub API still
# lists assets; only the asset host is rewritten.
ASSET_MIRROR = os.environ.get('ROUTE_DEBS_ASSET_MIRROR', '').rstrip('/')


def _deb_field(deb_path: str, field: str) -> str:
    try:
        r = subprocess.run(
            ['dpkg-deb', '-f', deb_path, field],
            capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip()
    except Exception:
        return "?"


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _is_missing(text):
    lower = text.lower()
    return (
        'http 404' in lower
        or 'release not found' in lower
        or 'no assets match' in lower
        or 'no assets to download' in lower
    )


def _gh_json(args, env):
    detail = ''
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            ret = subprocess.run(['gh', *args], capture_output=True, text=True,
                                 timeout=60, env=env)
        except subprocess.TimeoutExpired:
            detail = 'gh api timeout'
        else:
            if ret.returncode == 0:
                try:
                    return json.loads(ret.stdout), None
                except json.JSONDecodeError as exc:
                    detail = f'bad JSON: {exc}'
            else:
                detail = (ret.stderr or ret.stdout).strip()
                if _is_missing(detail):
                    return None, detail
        if attempt < DOWNLOAD_RETRIES:
            time.sleep(2 * attempt)
    return None, detail


def _download_with_mirror(repo, env):
    failures = []
    data, error = _gh_json(['api', f'repos/{repo}/releases/latest'], env)
    if error:
        if _is_missing(error):
            print('   ⏭️ 无 release，跳过。')
        else:
            print(f'   ❌ API 失败: {error[:500]}')
            failures.append(f'{repo}: API: {error[:200]}')
        return failures
    assets = data.get('assets', []) if isinstance(data, dict) else []
    debs = [item for item in assets if str(item.get('name', '')).endswith('.deb')]
    if not debs:
        print('   ⏭️ release 无 .deb 资产，跳过。')
        return failures
    for asset in debs:
        name = asset['name']
        url = f"{ASSET_MIRROR}/{asset['browser_download_url']}"
        dest = os.path.join(TMP_DIR, name)
        command = [
            'curl', '-L', '--fail', '--silent', '--show-error',
            '--retry', str(DOWNLOAD_RETRIES), '--retry-delay', '3',
            '--connect-timeout', '20', '--max-time', str(DOWNLOAD_TIMEOUT),
            '-o', dest, url,
        ]
        try:
            ret = subprocess.run(command, capture_output=True, text=True,
                                 timeout=DOWNLOAD_TIMEOUT + 60, env=env)
        except subprocess.TimeoutExpired:
            print(f'   ❌ 下载超时（{DOWNLOAD_TIMEOUT}s）: {name}')
            failures.append(f'{repo}/{name}: download timeout')
            continue
        if ret.returncode != 0:
            detail = (ret.stderr or ret.stdout).strip()
            print(f'   ❌ 下载失败: {name}: {detail[:300]}')
            failures.append(f'{repo}/{name}: download failed')
            continue
        digest = str(asset.get('digest') or '')
        if digest.startswith('sha256:') and _sha256(dest) != digest[7:]:
            print(f'   ❌ 校验失败: {name}')
            os.remove(dest)
            failures.append(f'{repo}/{name}: digest mismatch')
            continue
        print(f'   ✅ {name}')
    return failures


def _download_with_gh(repo, env):
    detail = ''
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            ret = subprocess.run(
                ['gh', 'release', 'download', '-R', repo, '-p', '*.deb', '-D', TMP_DIR],
                capture_output=True, text=True, timeout=DOWNLOAD_TIMEOUT, env=env,
            )
        except subprocess.TimeoutExpired:
            detail = f'timeout after {DOWNLOAD_TIMEOUT}s'
            print(f'   ❌ 下载超时（{DOWNLOAD_TIMEOUT}s），第 {attempt} 次')
        else:
            if ret.returncode == 0:
                return []
            detail = (ret.stderr or ret.stdout).strip()
            if _is_missing(detail):
                print('   ⏭️ 无 release/deb 资产，跳过。')
                return []
            print(f'   ❌ 第 {attempt} 次下载失败: {detail[:300]}')
        if attempt < DOWNLOAD_RETRIES:
            time.sleep(3 * attempt)
    print('   ❌ 跳过该仓库')
    return [f'{repo}: download failed: {detail[:200]}']


def download_all(repos):
    if os.path.exists(TMP_DIR):
        shutil.rmtree(TMP_DIR)
    os.makedirs(TMP_DIR)

    # Do not inject a default localhost proxy: CI servers and the APT host may
    # have direct GitHub access. Set ROUTE_DEBS_PROXY (or HTTPS_PROXY) only when
    # an outbound proxy is actually required.
    env = dict(os.environ)
    proxy = os.environ.get('ROUTE_DEBS_PROXY')
    if proxy:
        env['HTTPS_PROXY'] = proxy
        env['https_proxy'] = proxy

    if ASSET_MIRROR:
        print(f'ℹ️ 使用 Release 资产镜像: {ASSET_MIRROR}')

    failures = []
    for repo in repos:
        print(f'📥 拉取 {repo} ...')
        if ASSET_MIRROR:
            failures.extend(_download_with_mirror(repo, env))
        else:
            failures.extend(_download_with_gh(repo, env))
    return failures


def route_all(routing, incoming):
    suites = list(routing.keys())
    for s in suites:
        os.makedirs(os.path.join(incoming, s), exist_ok=True)

    debs = sorted(f for f in os.listdir(TMP_DIR) if f.endswith('.deb'))
    if not debs:
        print('(无 .deb 包)')
        return []

    manifest = []

    for basename in debs:
        src = os.path.join(TMP_DIR, basename)
        dests = []
        matched_pattern = False

        for suite in suites:
            for entry in routing[suite]:
                p = entry['pattern']
                ver = entry.get('version', 'auto')
                if fnmatch.fnmatch(basename, p):
                    matched_pattern = True
                    if ver != 'auto' and ver not in basename:
                        continue
                    if suite not in dests:
                        dests.append(suite)
                    break

        if not dests:
            if matched_pattern:
                print(f'   ⏭️ 版本不符合 routing 规则，跳过: {basename}')
            else:
                print(f'   ⏭️ 无匹配规则，跳过: {basename}')
            continue

        for s in dests:
            print(f'   📍 {basename} -> {s}')
            shutil.copy2(src, os.path.join(incoming, s, basename))

        manifest.append({
            'deb': basename,
            'suites': dests,
            'package': _deb_field(src, 'Package'),
            'version': _deb_field(src, 'Version'),
            'architecture': _deb_field(src, 'Architecture'),
        })

    return manifest


def print_version_summary(manifest, incoming):
    """GitHub Actions log-friendly version table."""
    print()
    print('=' * 80)
    print('  阶段 3: 版本汇总')
    print('=' * 80)
    print(f'{"Package":<32} {"Version":<18} {"Arch":<8} {"Suite":<12}')
    print('-' * 80)
    for item in manifest:
        basename = item['deb']
        suite = ','.join(item['suites'])
        for s in item['suites']:
            deb_path = os.path.join(incoming, s, basename)
            if os.path.isfile(deb_path):
                pkg = _deb_field(deb_path, 'Package')
                ver = _deb_field(deb_path, 'Version')
                arch = _deb_field(deb_path, 'Architecture')
                print(f'{pkg:<32} {ver:<18} {arch:<8} {suite:<12}')
                break

    print('-' * 80)
    print(f'  共 {len(manifest)} 个 deb 包')
    print('=' * 80)


def main():
    argv = [arg for arg in sys.argv[1:] if arg != '--strict']
    strict = len(argv) != len(sys.argv) - 1

    if '--print-expected' in argv:
        idx = argv.index('--print-expected')
        if idx + 2 >= len(argv):
            print('Usage: route_debs.py --print-expected <routing.yaml> <suite>', file=sys.stderr)
            return 1
        config_path = argv[idx + 1]
        suite = argv[idx + 2]
        with open(config_path) as f:
            config = yaml.safe_load(f)
        routing = config.get('routing', {})
        if suite in routing:
            packages = set()
            for entry in routing[suite]:
                pkg = entry['pattern'].split('_*')[0]
                packages.add(pkg)
            for pkg in sorted(packages):
                print(pkg)
        return 0

    if len(argv) < 2:
        print('Usage: route_debs.py [--strict] <routing.yaml> <incoming_dir>')
        return 1

    config_path = argv[0]
    incoming = argv[1]

    with open(config_path) as f:
        config = yaml.safe_load(f)

    repos = config['repos']
    routing = config['routing']

    print('=' * 50)
    print('  阶段 1: 下载所有仓库的 release .deb')
    print('=' * 50)
    failures = download_all(repos)

    print()
    print('=' * 50)
    print('  阶段 2: 按 routing.yaml 分拣')
    print('=' * 50)
    manifest = route_all(routing, incoming)

    print_version_summary(manifest, incoming)

    shutil.rmtree(TMP_DIR, ignore_errors=True)

    manifest_path = os.path.join(incoming, MANIFEST_FILE)
    with open(manifest_path, 'w') as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    print(f'\n✅ 分拣完成，共 {len(manifest)} 个包，清单: {manifest_path}')

    if failures:
        print(f'\n❌ {len(failures)} 个下载/校验失败:')
        for failure in failures:
            print(f'  - {failure}')
        if strict:
            print('❌ strict 模式：下载或校验失败，退出非零')
            return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())