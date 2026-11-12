#!/usr/bin/env python3
import argparse
import hashlib
import json
import mimetypes
import os
import re
import shutil
import tempfile
import threading
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

try:
    import yaml
except Exception:
    yaml = None

ROOT = Path(__file__).resolve().parent
ANDROID = ROOT.parent
REPO = ANDROID.parent
CATALOG_PATH = ROOT / "data" / "catalog.json"
BUNDLES = ANDROID / "robopoly" / "sensor_bundles"
TMP = REPO / "test" / "tmp" / "plugin_store"
CACHE = TMP / "cache"
URDF_ROOTS = {
    "rpo-description": Path("/home/robo/roboto/roboparty_description/rpo_description"),
    "rp1-description": Path("/home/robo/roboparty_repo/deb/roboparty_description/rp1_description"),
    "rp-hand-left": Path("/home/robo/tret_repo/hand_demo/vendor/models/rp_hand_left.urdf"),
    "rp-hand-right": Path("/home/robo/tret_repo/hand_demo/vendor/models/rp_hand_right.urdf"),
}
QWIIC = [
    ("nst175", "NST175 温度", "I2C 0x48", "novosense,nst175", "gw temp"),
    ("atecc608a", "ATECC608A", "I2C 0x60", "microchip,atecc608a", "gw atecc"),
    ("ch224a", "CH224A USB-PD", "I2C 0x23", "wch,ch224a", "gw ch224"),
    ("ch338f", "CH338F USB Hub", "I2C 0x2C", "wch,ch338f", "gw ch338"),
    ("i2c-scan", "I2C 总线扫描", "I2C D4/D5", "xiao-peripherals", "gw scan"),
]
MALLS = {
    "urdf": {"name": "URDF", "description": "机器人 URDF / 网格 / 描述文件"},
    "model": {"name": "Model", "description": "ONNX 策略、HuggingFace 动作与模型"},
    "xiao": {"name": "XIAO", "description": "XIAO 固件、传感器与外设插件"},
    "qwiic": {"name": "Qwiic", "description": "连接在 XIAO 上的 I2C 模块"},
    "robotd": {"name": "RobotD", "description": "robotd / 主机侧插件与配置"},
}

# 标准包补充层版本。修改生成规则时必须递增，使重打包缓存失效。
WRAPPER_VERSION = "xiao-wrap-v1"
MANIFEST_NAMES = ("urdf.yaml", "model.yaml", "plugin.yaml", "sensor.yaml", "action.yaml")
GENERATED_ROOT_NAMES = {"skill.md", "plugin.yaml", "provenance.json", "plugin-install.json"}
MAX_ARCHIVE_FILE_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_ENTRY_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 2048
MAX_ARCHIVE_PATH = 240
MAX_MANIFEST_BYTES = 64 * 1024
MAX_SKILL_BYTES = 256 * 1024
MAX_SUM_LINES = 4096
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def package_files(path: Path):
    if path.is_file():
        return [path]
    files = []
    for p in path.rglob("*"):
        if not p.is_file():
            continue
        if any(part in {".git", "__pycache__", ".DS_Store"} for part in p.parts):
            continue
        files.append(p)
    return files


def dir_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in package_files(path))


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def load_catalog():
    if not CATALOG_PATH.is_file():
        return []
    raw = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    return raw.get("modules", raw)


def load_bundle_meta(bundle: Path):
    if not bundle.is_dir() or not (bundle / "sensor.yaml").is_file():
        return None
    if yaml is None:
        raise RuntimeError("PyYAML is required to read sensor.yaml")
    return yaml.safe_load((bundle / "sensor.yaml").read_text(encoding="utf-8")) or {}


def bundle_skill(bundle: Path | None, fallback: str) -> str:
    if bundle is None:
        return fallback
    skill = bundle / "skill.md"
    if skill.is_file():
        text = read_text(skill)
        if text.strip():
            return text
    return fallback


def clean_text(value, limit=4000) -> str:
    """去除 NUL/控制字符，避免生成的 manifest/skill 被安装器判为坏文件。"""
    if value is None:
        return ""
    text = str(value).replace("\x00", "")
    text = "".join(
        ch for ch in text
        if ch in "\n\t" or not (ord(ch) < 32 or ord(ch) == 127)
    )
    return text.strip()[:limit]


def target_board(target, pid: str) -> str:
    board = clean_text((target or {}).get("board") if isinstance(target, dict) else "")
    return board or clean_text(pid)


def xiao_skill_markdown(pid, name, version, target=None, install=None,
                        install_level="D1", upstream_url=None,
                        upstream_sha256=None) -> str:
    """为上游 XIAO 固件生成按板卡定制的非空 skill.md。"""
    target = target if isinstance(target, dict) else {}
    board = target_board(target, pid)
    transport = clean_text(target.get("transport")) or "serial"
    flash = clean_text(target.get("flash")) or "serial,dfu"
    command = clean_text(install)
    if not command:
        command = (
            f"robopoly plugin install {pid}@v{version} --allow-danger\n"
            f"robopoly plugin flash {pid} --board {board} --port <serial> --yes"
        )
    lines = [
        f"# {clean_text(name) or pid}",
        "",
        f"- 插件 id: `{clean_text(pid)}`",
        f"- 版本: `{clean_text(version)}`",
        f"- 板卡: `{board}`",
        "- 类型: XIAO 固件 (firmware)",
        f"- 传输: `{transport}`",
        f"- 刷写: `{flash}`",
        f"- 安装等级: {clean_text(install_level) or 'D1'}",
        "",
        "固件文件位于包内 `firmware/` 目录，字节与上游发布包一致；本包只补充 "
        "`skill.md`、`plugin.yaml`、`provenance.json` 与 `SHA256SUMS`，不会改写固件。",
        "",
        "## 安装 / 刷写",
        "",
        "```bash",
        command,
        "```",
    ]
    if upstream_url:
        lines += ["", "## 上游来源", "", f"- URL: {clean_text(upstream_url)}"]
        if upstream_sha256:
            lines.append(f"- sha256: `{clean_text(upstream_sha256)}`")
    return "\n".join(lines).rstrip() + "\n"


def make_plugin(mall, pid, name, version="1.0.0", kind="plugin", description="",
                install_level="D1", plugin_class="", source="local",
                local_path=None, upstream_url=None, external_url=None, skill="", repo=None,
                archive_format=None, expected_sha256=None, target=None, install=None):
    p = {
        "mall": mall,
        "id": pid,
        "name": name or pid,
        "version": version or "1.0.0",
        "kind": kind,
        "description": description,
        "install_level": install_level,
        "class": plugin_class,
        "source": source,
        "repo": repo,
        "skill": skill,
        "archive_format": archive_format,
    }
    if target:
        p["target"] = target
    if install:
        p["install"] = install
    if archive_format == "zip":
        p["checksums"] = {"algorithm": "sha256", "file": "SHA256SUMS"}
    if local_path:
        path = Path(local_path)
        p["local_path"] = str(path)
        p["size"] = dir_size(path) if path.exists() else 0
        p["sha256"] = sha256(path) if path.is_file() and archive_format != "zip" else None
        p["download"] = f"/api/v1/malls/{mall}/plugins/{pid}/download"
    if upstream_url:
        p["upstream_url"] = upstream_url
        p["expected_sha256"] = expected_sha256.lower() if expected_sha256 else None
        p["upstream_sha256"] = p["expected_sha256"]
        # 上游 ZIP 会被重打包以补充标准文件，成品字节已变化；
        # 目录里的 sha256 不能继续冒充成品校验值。
        p["sha256"] = None
        p["download"] = f"/api/v1/malls/{mall}/plugins/{pid}/download"
    if external_url:
        p["external_url"] = external_url
        p["external"] = True
        if not local_path and not upstream_url:
            p["download"] = external_url
    return p


def build_plugins():
    catalog = load_catalog()
    bundles = {p.name: p for p in BUNDLES.iterdir() if p.is_dir()} if BUNDLES.is_dir() else {}
    plugins = {m: [] for m in MALLS}

    for item in catalog:
        pid = item.get("id", "")
        pclass = item.get("class", "")
        kind = item.get("kind", "plugin")
        bundle = bundles.get(pid)
        local_path = bundle if bundle else None
        skill = ""
        if bundle:
            skill = bundle_skill(bundle, f"# {item.get('name', pid)}\n\n{pid}")
        else:
            skill = f"# {item.get('name', pid)}\n\n- id: {pid}\n- class: {pclass}\n- kind: {kind}\n\n{item.get('description', '')}\n"
        versions = item.get("versions") or []
        latest = versions[0] if versions else {}
        upstream = None
        if pclass != "huggingface" and not bundle:
            upstream = latest.get("zipUrl")
        expected_sha = latest.get("sha256") if upstream else None
        if pclass == "xiao" and kind == "firmware" and upstream:
            # 上游固件 ZIP 没有 skill.md；目录与下载包共用同一份按板卡生成的说明。
            skill = xiao_skill_markdown(
                pid, item.get("name", pid), item.get("version", "1.0.0"),
                target=item.get("target"), install=item.get("install"),
                install_level=item.get("installLevel", "D1"),
                upstream_url=upstream, upstream_sha256=expected_sha,
            )
        archive_format = item.get("archive_format") or (
            "external" if pclass == "huggingface" else ("zip" if (local_path or upstream) else None)
        )
        common = dict(
            version=item.get("version", "1.0.0"),
            kind=kind,
            description=item.get("description", ""),
            install_level=item.get("installLevel", "D1"),
            plugin_class=pclass,
            local_path=local_path,
            upstream_url=upstream,
            skill=skill,
            repo=item.get("repo"),
            archive_format=archive_format,
            expected_sha256=expected_sha,
            target=item.get("target") or {},
            install=item.get("install"),
        )
        if pclass == "xiao":
            if kind == "firmware":
                plugins["xiao"].append(make_plugin("xiao", pid, item.get("name", pid), **common))
            else:
                # XIAO 传感器/外设归入 qwiic 分类，但保留 class=xiao 兼容旧 sensor。
                plugins["qwiic"].append(make_plugin("qwiic", pid, item.get("name", pid), **common))
        elif pclass == "dev":
            plugins["robotd"].append(make_plugin("robotd", pid, item.get("name", pid), **common))
        elif pclass == "huggingface":
            repo = item.get("repo") or pid
            plugins["model"].append(make_plugin(
                "model", pid, item.get("name", pid), source="huggingface",
                external_url=f"https://huggingface.co/{repo}", **common,
            ))

    qwiic_ids = {p["id"] for p in plugins["qwiic"]}
    for pid, name, iface, driver, command in QWIIC:
        if pid in qwiic_ids:
            # 已由 sensor bundle 元数据提供（如 NST175），优先保留 bundle 条目。
            continue
        bundle = bundles.get(pid)
        skill = bundle_skill(bundle, f"# {name}\n\n- bus: {iface}\n- driver: {driver}\n- command: `{command}`\n")
        plugins["qwiic"].append(make_plugin(
            "qwiic", pid, name, kind="i2c", description=f"XIAO I2C 模块 · {iface}",
            install_level="D1", plugin_class="qwiic", local_path=bundle,
            skill=skill, archive_format="zip",
            target={"board": "xiao", "transport": "i2c", "interface": iface},
        ))
        qwiic_ids.add(pid)

    for pid, path in URDF_ROOTS.items():
        if not path.exists():
            continue
        plugins["urdf"].append(make_plugin(
            "urdf", pid, path.stem, version="local", kind="urdf",
            description="机器人 URDF / 网格描述包", install_level="D0",
            plugin_class="urdf", local_path=path, archive_format="zip",
            skill=f"# {path.stem}\n\n- URDF: {path.name}\n- 用于机器人运动学、可视化和仿真。\n",
        ))

    model_dirs = sorted((ANDROID / "robopoly" / "build").glob("*/assets/rpo/models"))
    if model_dirs:
        model_dir = max(model_dirs, key=lambda p: p.stat().st_mtime)
        for path in sorted(model_dir.glob("*.onnx")):
            plugins["model"].append(make_plugin(
                "model", f"local-{path.stem}", path.stem, version="local",
                kind="onnx", description="RPO 本地 ONNX 策略", install_level="D1",
                plugin_class="model", local_path=path, archive_format="zip",
                skill=f"# {path.stem}\n\n本地 ONNX 策略模型，以标准 ZIP 包分发。\n",
            ))

    for mall in plugins:
        plugins[mall].sort(key=lambda p: (p["name"].lower(), p["id"]))
    return plugins


PLUGINS = build_plugins()


def public_plugin(p):
    result = {k: v for k, v in p.items() if k not in ("local_path", "skill", "expected_sha256")}
    if p["mall"] in ("urdf", "qwiic"):
        result["download"] = f"/api/v1/malls/{p['mall']}/plugins/{p['id']}/download"
        result["sha256"] = None
    if p.get("upstream_url"):
        # 上游 ZIP 会被重新打包；成品哈希只有下载时才知道，目录返回 null，
        # 上游哈希仅以 upstream_sha256 作为来源信息保留。
        result["sha256"] = None
    result.setdefault("archive_format", None)
    if p["mall"] == "urdf":
        result["assembly_template"] = f"/api/v1/malls/urdf/plugins/{p['id']}/assembly"
    return result


def assembly_template(p):
    path = Path(p["local_path"])
    urdfs = sorted(path.rglob("*.urdf")) if path.is_dir() else [path]
    return {
        "schema_version": 1, "robot": p["id"], "version": p["version"],
        "descriptions": [{
            "file": str(f.relative_to(path)) if path.is_dir() else f.name,
            "joints": [{"name": j.get("name"), "type": j.get("type")}
                       for j in ET.parse(f).getroot().findall("joint")],
        } for f in urdfs],
        "calibration": [], "parts": [], "workpiece": None,
    }


PACKAGE_LOCK = threading.Lock()


def plugin_zip(p):
    """Build a self-contained package with metadata and checksums at its root."""
    path = Path(p["local_path"]) if p.get("local_path") else None
    if not p.get("skill", "").strip():
        raise ValueError("skill.md is empty")
    metadata = {"schema_version": 1, "id": p["id"], "name": p["name"],
                "version": p["version"], "class": p["mall"], "kind": p["kind"],
                "archive_format": "zip",
                "checksums": {"algorithm": "sha256", "file": "SHA256SUMS"}}
    extras = {"skill.md": p["skill"].encode()}
    if p["kind"] == "onnx":
        if path is None or not path.is_file():
            raise ValueError("local ONNX model is missing")
        metadata["artifact"] = {"format": "onnx", "file": path.name, "packaged_as": "zip"}
        extras["model.yaml"] = json.dumps(metadata, ensure_ascii=False, indent=2).encode()
    elif p["mall"] == "urdf":
        template = assembly_template(p)
        metadata["urdf"] = [d["file"] for d in template["descriptions"]]
        metadata["assembly_template"] = "assembly.template.yaml"
        extras["assembly.template.yaml"] = json.dumps(template, ensure_ascii=False, indent=2).encode()
        extras["urdf.yaml"] = json.dumps(metadata, ensure_ascii=False, indent=2).encode()
    else:
        if path and (path / "sensor.yaml").is_file():
            # 已有 sensor.yaml bundle（如 adc-48v / tws-bms / nst175）直接使用，无需查 QWIIC。
            extras["sensor.yaml"] = (path / "sensor.yaml").read_bytes()
        else:
            # 仅无 bundle 的静态模块才查 QWIIC；查不到时也不报错。
            spec = next((q for q in QWIIC if q[0] == p["id"]), None)
            if spec is not None:
                metadata.update(target={"board": "xiao", "transport": "i2c", "interface": spec[2]},
                                driver={"layer": "zephyr", "name": spec[3]}, command=spec[4],
                                safety={"install_level": "D1", "auto_enable": False})
            extras["sensor.yaml"] = json.dumps(metadata, ensure_ascii=False, indent=2).encode()
    fingerprint = hashlib.sha256(b"".join(extras.values()) +
                                 (package_fingerprint(path).encode() if path else b"")).hexdigest()[:20]
    CACHE.mkdir(parents=True, exist_ok=True)
    out = CACHE / f"{p['mall']}-{p['id']}-{fingerprint}.zip"
    with PACKAGE_LOCK:
        if out.is_file():
            return out
        with tempfile.NamedTemporaryFile(dir=CACHE, suffix=".part", delete=False) as tmp:
            temp_path = Path(tmp.name)
        try:
            hashes = []
            with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
                for f in sorted(package_files(path)) if path else []:
                    name = str(f.relative_to(path)) if path.is_dir() else f.name
                    base = name.rsplit("/", 1)[-1].lower()
                    # 根目录只保留本函数生成的 skill.md/SHA256SUMS；跳过载荷中的同名文件，
                    # 保证每个包只有一个非空 skill.md。
                    if name in extras or name == "SHA256SUMS" or base in (
                        "skill.md", "sha256sums", "sha256sums.txt",
                    ):
                        continue
                    archive.write(f, name)
                    hashes.append(f"{sha256(f)}  {name}\n")
                for name, content in extras.items():
                    archive.writestr(name, content)
                    hashes.append(f"{hashlib.sha256(content).hexdigest()}  {name}\n")
                archive.writestr("SHA256SUMS", "".join(hashes))
            os.replace(temp_path, out)
        finally:
            temp_path.unlink(missing_ok=True)
    return out


def skill_for(mall, pid):
    for p in PLUGINS.get(mall, []):
        if p["id"] == pid:
            return p.get("skill", "")
    return None


def find_plugin(mall, pid):
    for p in PLUGINS.get(mall, []):
        if p["id"] == pid:
            return p
    return None


def package_fingerprint(path: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(package_files(path)):
        st = f.stat()
        h.update(str(f.relative_to(path) if path.is_dir() else f.name).encode())
        h.update(f"{st.st_size}:{st.st_mtime_ns}".encode())
    return h.hexdigest()[:16]


def remote_cache_path(upstream: str, pid: str) -> Path:
    key = hashlib.sha256(upstream.encode()).hexdigest()[:16]
    return CACHE / f"remote-{key}-{pid}.zip"


def ensure_remote(upstream: str, pid: str, expected_sha256=None) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    cached = remote_cache_path(upstream, pid)
    expected = expected_sha256.lower() if expected_sha256 else None
    if cached.is_file() and cached.stat().st_size > 0:
        if expected is None or sha256(cached) == expected:
            return cached
        cached.unlink()
    token = os.environ.get("GITHUB_TOKEN")
    tmp = cached.with_suffix(".part")
    req = github_asset_request(upstream, token) if token else urllib.request.Request(upstream)
    try:
        with urllib.request.urlopen(req, timeout=60) as r, tmp.open("wb") as out:
            total = 0
            while True:
                chunk = r.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_ARCHIVE_FILE_BYTES:
                    raise ValueError(f"upstream archive too large for {pid}")
                out.write(chunk)
        if total == 0:
            raise ValueError(f"upstream archive is empty for {pid}")
        actual = sha256(tmp)
        if expected is not None and actual != expected:
            raise ValueError(
                f"upstream sha256 mismatch for {pid}: expected {expected}, got {actual}"
            )
        os.replace(tmp, cached)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return cached


def safe_member_name(raw: str):
    """规范化 ZIP 条目名；返回 None 表示不允许（穿越/绝对路径/控制字符）。"""
    if not raw or len(raw) > MAX_ARCHIVE_PATH:
        return None
    if raw.startswith("/") or "\\" in raw:
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        return None
    name = raw.rstrip("/")
    if not name:
        return None
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    if re.match(r"^[A-Za-z]:", parts[0]):
        return None
    return "/".join(parts)


def scan_upstream_members(archive: zipfile.ZipFile, pid: str):
    """只校验并枚举上游条目，不解压到磁盘；有风险的包直接拒绝。"""
    infos = archive.infolist()
    if len(infos) > MAX_ARCHIVE_ENTRIES:
        raise ValueError(f"上游 ZIP 条目过多 for {pid}")
    seen = set()
    payload = []
    total = 0
    for info in infos:
        name = safe_member_name(info.filename)
        if name is None:
            raise ValueError(f"上游 ZIP 包含不安全路径 for {pid}: {info.filename!r}")
        key = name.lower()
        if key in seen:
            raise ValueError(f"上游 ZIP 包含重复路径 for {pid}: {name}")
        seen.add(key)
        if info.flag_bits & 0x1:
            raise ValueError(f"上游 ZIP 含加密条目 for {pid}: {name}")
        mode = (info.external_attr >> 16) & 0o170000
        if mode == 0o120000:
            raise ValueError(f"上游 ZIP 含符号链接 for {pid}: {name}")
        if mode not in (0, 0o100000, 0o040000):
            raise ValueError(f"上游 ZIP 含特殊文件 for {pid}: {name}")
        if info.is_dir() or mode == 0o040000:
            continue
        if info.file_size > MAX_ARCHIVE_ENTRY_BYTES:
            raise ValueError(f"上游 ZIP 文件超过大小限制 for {pid}: {name}")
        total += info.file_size
        if total > MAX_ARCHIVE_TOTAL_BYTES:
            raise ValueError(f"上游 ZIP 解压后超过大小限制 for {pid}")
        payload.append((name, info))
    return payload


def read_member_limited(archive: zipfile.ZipFile, info: zipfile.ZipInfo,
                        limit: int, label: str) -> bytes:
    if info.file_size > limit:
        raise ValueError(f"{label} 超过大小限制")
    data = archive.read(info)
    if len(data) > limit:
        raise ValueError(f"{label} 超过大小限制")
    return data


def unquote_scalar(raw: str) -> str:
    value = raw.strip()
    if (value.startswith('"') or value.startswith("'")) and value.endswith(","):
        value = value[:-1].rstrip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_manifest_id_version(text: str, name: str):
    """与 Android 安装器一致：JSON 文档严格解析，否则只读顶层简单 YAML。"""
    trimmed = text.removeprefix("\ufeff").strip()
    if not trimmed:
        return None, None
    if trimmed.startswith("{") or trimmed.startswith("["):
        try:
            doc = json.loads(trimmed)
        except Exception as exc:
            raise ValueError(f"{name} JSON 格式无效: {exc}")
        if not isinstance(doc, dict):
            raise ValueError(f"{name} JSON 格式无效")

        def scalar(key):
            value = doc.get(key)
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                return str(value).strip() or None
            return None

        return scalar("id"), scalar("version")
    declared_id = None
    declared_version = None
    for raw in trimmed.splitlines()[:256]:
        if not raw or raw[0].isspace():
            continue
        line = raw.strip()
        if not line or line.startswith("#") or line in ("---", "..."):
            continue
        index = line.find(":")
        if index <= 0:
            continue
        key = line[:index].strip().strip("\"'").lower()
        if key not in ("id", "version"):
            continue
        value = unquote_scalar(line[index + 1:].split(" #", 1)[0].strip())
        if not value:
            continue
        if key == "id":
            declared_id = value
        else:
            declared_version = value
    return declared_id, declared_version


def validate_upstream_manifests(archive: zipfile.ZipFile, payload, plugin):
    declared_id = None
    declared_version = None
    for name, info in payload:
        if "/" in name or name.lower() not in MANIFEST_NAMES:
            continue
        data = read_member_limited(archive, info, MAX_MANIFEST_BYTES, name)
        mid, mversion = parse_manifest_id_version(data.decode("utf-8", "replace"), name)
        if mid is not None:
            if declared_id is not None and declared_id != mid:
                raise ValueError("上游 ZIP 包内 id 不一致")
            declared_id = mid
        if mversion is not None:
            if declared_version is not None and declared_version != mversion:
                raise ValueError("上游 ZIP 包内版本不一致")
            declared_version = mversion
    if declared_id is not None and declared_id != plugin["id"]:
        raise ValueError(f"上游 ZIP 包内 id 与目录不一致: {declared_id}")
    expected_version = (plugin.get("version") or "").strip()
    if declared_version is not None and expected_version and declared_version != expected_version:
        raise ValueError(f"上游 ZIP 包内版本与目录不一致: {declared_version}")


def skip_upstream_member(name: str) -> bool:
    lower = name.lower()
    base = lower.rsplit("/", 1)[-1]
    # 任何层级的 skill.md 都丢弃，保证成品只有一个根 skill.md。
    if base == "skill.md":
        return True
    # 旧的校验和文件不再适用，由本服务重新生成。
    if base in ("sha256sums", "sha256sums.txt"):
        return True
    # 安装器在任意层级都拒绝保留元数据文件。
    if base == "plugin-install.json":
        return True
    # 生成文件优先；保留其他 manifest（如 sensor.yaml）。
    if lower in GENERATED_ROOT_NAMES:
        return True
    return False


def xiao_manifest(plugin, upstream_url: str, upstream_sha256: str) -> bytes:
    target = plugin.get("target") if isinstance(plugin.get("target"), dict) else {}
    doc = {
        "schema_version": 1,
        "id": plugin["id"],
        "name": plugin.get("name", plugin["id"]),
        "version": plugin.get("version", "1.0.0"),
        "class": "xiao",
        "kind": plugin.get("kind", "firmware"),
        "description": plugin.get("description", ""),
        "archive_format": "zip",
        "target": target or {"board": target_board(target, plugin["id"])},
        "install_level": plugin.get("install_level", "D1"),
        "checksums": {"algorithm": "sha256", "file": "SHA256SUMS"},
        "provenance": {
            "upstream_url": upstream_url,
            "upstream_sha256": upstream_sha256,
        },
    }
    if plugin.get("install"):
        doc["install"] = plugin["install"]
    return (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def provenance_doc(plugin, upstream_url: str, upstream_sha256: str,
                   upstream_size: int) -> bytes:
    doc = {
        "schema_version": 1,
        "wrapper": "plugin_store_server/xiao-repack",
        "wrapper_version": WRAPPER_VERSION,
        "id": plugin["id"],
        "version": plugin.get("version", "1.0.0"),
        "class": "xiao",
        "kind": plugin.get("kind", "firmware"),
        "upstream_url": upstream_url,
        "upstream_sha256": upstream_sha256,
        "upstream_bytes": upstream_size,
        "note": "firmware bytes are preserved; only skill.md/plugin.yaml/provenance.json/SHA256SUMS are added",
    }
    return (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def generated_wrapper_files(plugin, upstream_url: str, upstream_sha256: str,
                            upstream_size: int):
    skill = xiao_skill_markdown(
        plugin["id"], plugin.get("name", plugin["id"]),
        plugin.get("version", "1.0.0"), target=plugin.get("target"),
        install=plugin.get("install"),
        install_level=plugin.get("install_level", "D1"),
        upstream_url=upstream_url, upstream_sha256=upstream_sha256,
    ).encode("utf-8")
    if not skill.strip() or len(skill) > MAX_SKILL_BYTES:
        raise ValueError("生成的 skill.md 为空或超过大小限制")
    files = {
        "skill.md": skill,
        "plugin.yaml": xiao_manifest(plugin, upstream_url, upstream_sha256),
        "provenance.json": provenance_doc(
            plugin, upstream_url, upstream_sha256, upstream_size,
        ),
    }
    for name, data in files.items():
        if not data:
            raise ValueError(f"生成的 {name} 为空")
    return files


def repack_fingerprint(upstream_sha256: str, generated) -> str:
    h = hashlib.sha256()
    h.update(WRAPPER_VERSION.encode())
    h.update(b"\0" + upstream_sha256.encode())
    for name in sorted(generated):
        data = generated[name]
        h.update(b"\0" + name.encode())
        h.update(len(data).to_bytes(8, "big"))
        h.update(data)
    return h.hexdigest()[:24]


def zip_entry(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=ZIP_EPOCH)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def write_repacked(archive: zipfile.ZipFile, kept, generated, temp_path: Path):
    hashes = {}
    total = 0
    with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as out:
        for name, info in sorted(kept, key=lambda item: item[0]):
            if name in hashes:
                raise ValueError(f"重打包出现重复路径: {name}")
            digest = hashlib.sha256()
            written = 0
            with archive.open(info) as src, out.open(zip_entry(name), "w") as dst:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    total += len(chunk)
                    if written > MAX_ARCHIVE_ENTRY_BYTES:
                        raise ValueError(f"上游 ZIP 文件超过大小限制: {name}")
                    if total > MAX_ARCHIVE_TOTAL_BYTES:
                        raise ValueError("上游 ZIP 解压后超过大小限制")
                    digest.update(chunk)
                    dst.write(chunk)
            hashes[name] = digest.hexdigest()
        for name in sorted(generated):
            if name in hashes:
                raise ValueError(f"重打包出现重复路径: {name}")
            data = generated[name]
            out.writestr(zip_entry(name), data)
            hashes[name] = hashlib.sha256(data).hexdigest()
        if len(hashes) > MAX_SUM_LINES:
            raise ValueError("重打包条目过多")
        lines = "".join(f"{hashes[name]}  {name}\n" for name in sorted(hashes))
        out.writestr(zip_entry("SHA256SUMS"), lines.encode("utf-8"))


def repack_xiao(plugin, upstream: Path, expected_sha256=None) -> Path:
    """把上游 XIAO 固件 ZIP 包装成安装器可接受的标准包。

    只读取并重新压缩条目，不写到临时解压目录；固件字节保持不变。上游校验和必须
    在重打包前通过，否则拒绝。缓存键包含上游哈希与全部生成内容，元数据或上游
    变化会自动生成新文件。
    """
    upstream = Path(upstream)
    if not upstream.is_file():
        raise ValueError("上游 ZIP 不存在")
    if upstream.stat().st_size > MAX_ARCHIVE_FILE_BYTES:
        raise ValueError("上游 ZIP 超过大小限制")
    actual = sha256(upstream)
    expected = expected_sha256.lower() if expected_sha256 else None
    if expected is not None and actual != expected:
        raise ValueError(
            f"upstream sha256 mismatch for {plugin['id']}: expected {expected}, got {actual}"
        )
    upstream_url = plugin.get("upstream_url") or ""
    generated = generated_wrapper_files(plugin, upstream_url, actual, upstream.stat().st_size)
    fingerprint = repack_fingerprint(actual, generated)
    safe_id = re.sub(r"[^A-Za-z0-9._-]+", "_", plugin["id"])[:80] or "plugin"
    CACHE.mkdir(parents=True, exist_ok=True)
    out = CACHE / f"xiao-{safe_id}-{fingerprint}.zip"
    with PACKAGE_LOCK:
        if out.is_file() and out.stat().st_size > 0:
            return out
        try:
            with zipfile.ZipFile(upstream) as archive:
                payload = scan_upstream_members(archive, plugin["id"])
                validate_upstream_manifests(archive, payload, plugin)
                kept = [
                    (name, info) for name, info in payload
                    if not skip_upstream_member(name)
                ]
                # 安装器上限包含生成的 skill.md/plugin.yaml/provenance.json/SHA256SUMS。
                if len(kept) + 4 > MAX_ARCHIVE_ENTRIES:
                    raise ValueError(f"重打包条目过多 for {plugin['id']}")
                with tempfile.NamedTemporaryFile(
                    dir=CACHE, suffix=".part", delete=False,
                ) as tmp:
                    temp_path = Path(tmp.name)
                try:
                    write_repacked(archive, kept, generated, temp_path)
                    if temp_path.stat().st_size > MAX_ARCHIVE_FILE_BYTES:
                        raise ValueError(f"重打包 ZIP 超过大小限制 for {plugin['id']}")
                    os.replace(temp_path, out)
                finally:
                    temp_path.unlink(missing_ok=True)
        except zipfile.BadZipFile as exc:
            raise ValueError(f"上游 ZIP 无效 for {plugin['id']}: {exc}")
    return out


def prewarm():
    for mall, plugins in PLUGINS.items():
        for p in plugins:
            local = p.get("local_path")
            if not local:
                continue
            path = Path(local)
            if path.is_dir() or p.get("archive_format") == "zip":
                try:
                    plugin_zip(p)
                    print(f"cached {mall}/{p['id']}", flush=True)
                except Exception as exc:
                    print(f"cache failed {mall}/{p['id']}: {exc}", flush=True)
    remote = [
        p for p in PLUGINS.get("xiao", [])
        if p.get("upstream_url") and not p.get("local_path")
    ]
    if not remote:
        return

    def prewarm_remote(plugin):
        cached = ensure_remote(
            plugin["upstream_url"], plugin["id"], plugin.get("expected_sha256"),
        )
        return repack_xiao(plugin, cached, plugin.get("expected_sha256"))

    if not os.environ.get("GITHUB_TOKEN"):
        # 无 token 时不发起网络请求，只重打包已经存在的上游缓存。
        for plugin in remote:
            cached = remote_cache_path(plugin["upstream_url"], plugin["id"])
            if not cached.is_file():
                continue
            try:
                prewarm_remote(plugin)
                print(f"repacked cached remote {plugin['id']}", flush=True)
            except Exception as exc:
                print(f"repack cached remote failed {plugin['id']}: {exc}", flush=True)
        return
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(prewarm_remote, plugin): plugin["id"] for plugin in remote}
        for fut in as_completed(futures):
            pid = futures[fut]
            try:
                fut.result()
                print(f"cached and repacked remote {pid}", flush=True)
            except Exception as exc:
                print(f"remote cache failed {pid}: {exc}", flush=True)


def github_asset_request(upstream: str, token: str):
    parsed = urlparse(upstream)
    parts = [x for x in parsed.path.strip("/").split("/") if x]
    if len(parts) < 6 or parts[2] != "releases" or parts[3] != "download":
        raise ValueError("not a GitHub release asset URL")
    owner, repo, tag, asset_name = parts[0], parts[1], parts[4], parts[5]
    api = f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}"
    req = urllib.request.Request(api, headers={
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "roboparty-plugin-store",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        release = json.load(r)
    asset = next((a for a in release.get("assets", []) if a.get("name") == asset_name), None)
    if asset is None:
        raise FileNotFoundError(f"asset not found: {asset_name}")
    return urllib.request.Request(asset["url"], headers={
        "Authorization": f"token {token}",
        "Accept": "application/octet-stream",
        "User-Agent": "roboparty-plugin-store",
    })


class Handler(BaseHTTPRequestHandler):
    server_version = "plugin-store/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}", flush=True)

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, body, content_type, filename=None, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def send_stream(self, stream, content_type, length, filename=None,
                    archive_sha256=None):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "public, max-age=60")
        if archive_sha256:
            # 上游 ZIP 重打包后字节已变化，这里给出实际响应体的哈希。
            self.send_header("X-Archive-SHA256", archive_sha256)
            self.send_header("ETag", f'"{archive_sha256}"')
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        shutil.copyfileobj(stream, self.wfile, length=1024 * 1024)

    def do_GET(self):
        parsed = urlparse(self.path)
        parts = [unquote(x) for x in parsed.path.strip("/").split("/") if x]
        if parsed.path == "/health":
            return self.send_json({"ok": True, "malls": {k: len(v) for k, v in PLUGINS.items()}})
        if parsed.path == "/api/v1/malls":
            return self.send_json({
                "malls": [
                    {"id": k, "name": v["name"], "description": v["description"], "count": len(PLUGINS[k])}
                    for k, v in MALLS.items()
                ]
            })
        if parsed.path == "/api/v1/catalog":
            return self.send_json({
                "malls": {
                    k: {"name": v["name"], "description": v["description"], "plugins": [public_plugin(p) for p in PLUGINS[k]]}
                    for k, v in MALLS.items()
                }
            })
        if len(parts) >= 4 and parts[:3] == ["api", "v1", "malls"]:
            mall = parts[3]
            if mall not in PLUGINS:
                return self.send_json({"error": "mall not found"}, 404)
            if len(parts) == 4:
                q = parse_qs(parsed.query).get("q", [""])[0].strip().lower()
                items = PLUGINS[mall]
                if q:
                    items = [p for p in items if q in p["id"].lower() or q in p["name"].lower() or q in p["description"].lower()]
                return self.send_json({"mall": mall, "plugins": [public_plugin(p) for p in items]})
            if len(parts) < 5 or parts[4] != "plugins":
                return self.send_json({"error": "not found"}, 404)
            if len(parts) == 5:
                return self.send_json({"mall": mall, "plugins": [public_plugin(p) for p in PLUGINS[mall]]})
            pid = parts[5]
            p = find_plugin(mall, pid)
            if not p:
                return self.send_json({"error": "plugin not found"}, 404)
            if len(parts) == 6:
                return self.send_json({"plugin": public_plugin(p)})
            if len(parts) == 7 and parts[6] == "skill":
                return self.send_bytes(skill_for(mall, pid).encode("utf-8"), "text/markdown; charset=utf-8", f"{pid}-skill.md")
            if len(parts) == 7 and parts[6] == "assembly" and mall == "urdf":
                return self.send_json(assembly_template(p))
            if len(parts) == 7 and parts[6] == "download":
                return self.download(p)
        return self.send_json({"error": "not found"}, 404)

    def download(self, p):
        local = p.get("local_path")
        if local or p["mall"] in ("urdf", "qwiic"):
            if local:
                path = Path(local)
                if not path.exists():
                    return self.send_json({"error": "local asset missing"}, 404)
            try:
                archive = plugin_zip(p)
                with archive.open("rb") as stream:
                    return self.send_stream(stream, "application/zip", archive.stat().st_size, f"{p['id']}.zip")
            except (OSError, ValueError, ET.ParseError) as exc:
                return self.send_json({"error": f"package failed: {exc}"}, 500)
        external = p.get("external_url")
        if external:
            self.send_response(302)
            self.send_header("Location", external)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        upstream = p.get("upstream_url")
        if upstream:
            try:
                cached = ensure_remote(upstream, p["id"], p.get("expected_sha256"))
                archive = repack_xiao(p, cached, p.get("expected_sha256"))
                digest = sha256(archive)
                with archive.open("rb") as stream:
                    return self.send_stream(
                        stream, "application/zip", archive.stat().st_size,
                        f"{p['id']}.zip", archive_sha256=digest,
                    )
            except Exception as exc:
                self.send_json({"error": f"upstream failed: {exc}"}, 502)
            return
        self.send_json({"error": "no downloadable asset"}, 404)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    TMP.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    server.request_queue_size = 128
    print(f"plugin store listening on http://{args.host}:{args.port}", flush=True)
    for mall, items in PLUGINS.items():
        print(f"  {mall}: {len(items)}", flush=True)
    threading.Thread(target=prewarm, daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
