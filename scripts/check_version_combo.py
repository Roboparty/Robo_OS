#!/usr/bin/env python3
"""Static checks for the T01/T17 fixed delivery combination.

This does not install packages or touch hardware. It verifies that the
workspace metadata agrees on paths, the start/all dependency, and the rp_server
launch delegation.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
CHECKS = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append({"name": name, "ok": bool(ok), "detail": detail})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true",
                        help="fail when manifest projects are not synced locally")
    args = parser.parse_args(argv)

    manifest = ROOT / "manifest.xml"
    if manifest.is_file():
        try:
            tree = ET.parse(manifest)
            projects = tree.findall(".//project")
            missing = [p.get("path") for p in projects
                       if not (ROOT / (p.get("path") or "")).is_dir()]
            if missing and not args.strict:
                check("manifest project paths", True,
                      f"not synced locally: {missing} (use --strict to fail)")
            else:
                check("manifest project paths", not missing,
                      f"missing={missing}" if missing else f"{len(projects)} projects")
        except ET.ParseError as exc:
            check("manifest parse", False, str(exc))
    else:
        check("manifest exists", False)

    start_control = ROOT / "deb/roboparty_start/debian/control"
    changelog = ROOT / "deb/roboparty_start/debian/changelog"
    check("start package", start_control.is_file() and
          "Package: roboparty-start" in start_control.read_text(encoding="utf-8"))
    if changelog.is_file():
        first = changelog.read_text(encoding="utf-8").splitlines()[0]
        match = re.search(r"\(([^)]+)\)", first)
        check("start version", bool(match), first)
    else:
        check("start changelog", False)

    all_control = ROOT / "deb/roboparty_all/debian/control"
    check("all depends on start", all_control.is_file() and
          "roboparty-start" in all_control.read_text(encoding="utf-8"))

    routing = ROOT / "routing.yaml"
    check("routing start package", routing.is_file() and
          "roboparty-start_*_all.deb" in routing.read_text(encoding="utf-8"))

    server_cfg = ROOT / "deb/roboparty_rp_server/config/server.yaml"
    cfg = server_cfg.read_text(encoding="utf-8") if server_cfg.is_file() else ""
    check("rpserver delegates to start",
          "start_robot.sh --foreground" in cfg and "{policy}" in cfg)

    for item in CHECKS:
        if args.json:
            continue
        status = "ok" if item["ok"] else "FAIL"
        print(f"[{status}] {item['name']}: {item['detail']}")
    if args.json:
        print(json.dumps(CHECKS, ensure_ascii=False, indent=2))
    return 0 if all(item["ok"] for item in CHECKS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
