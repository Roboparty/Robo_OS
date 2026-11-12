import contextlib
import hashlib
import io
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import warnings
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

import server


def make_fixture_zip(path, pid, version="1.1.0", board="fixture/board",
                     firmware=b"\x00\x01fixture-firmware" * 32, manifest=True,
                     extra=None):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README.md", f"# {pid}\n\nupstream fixture\n")
        archive.writestr("board.txt", board + "\n")
        archive.writestr(f"firmware/{board.replace('/', '_')}/zephyr.bin", firmware)
        if manifest:
            archive.writestr(
                "sensor.yaml",
                f"schema_version: 1\nid: {pid}\nversion: {version}\n"
                "class: xiao\nkind: firmware\n",
            )
        archive.writestr("SHA256SUMS", "deadbeef  stale-entry\n")
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return Path(path)


def make_xiao_plugin(pid, upstream_sha, version="1.1.0", board="fixture/board"):
    return server.make_plugin(
        "xiao", pid, "Fixture Firmware", version=version, kind="firmware",
        description="fixture firmware", install_level="D3", plugin_class="xiao",
        upstream_url=f"https://example.invalid/{pid}.zip",
        expected_sha256=upstream_sha,
        target={"board": board, "transport": "serial", "flash": "serial,dfu"},
        install=f"robopoly plugin flash {pid} --board {board} --yes",
    )


class StoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.TMP.mkdir(parents=True, exist_ok=True)
        cls.old_tempdir = tempfile.tempdir
        cls.old_tmpdir_env = os.environ.get("TMPDIR")
        tempfile.tempdir = str(server.TMP)
        os.environ["TMPDIR"] = str(server.TMP)
        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.http.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.thread.join()
        tempfile.tempdir = cls.old_tempdir
        if cls.old_tmpdir_env is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = cls.old_tmpdir_env

    @contextlib.contextmanager
    def isolated_cache(self):
        old = server.CACHE
        root = Path(tempfile.mkdtemp(prefix="repack-", dir=str(server.TMP)))
        server.CACHE = root
        try:
            yield root
        finally:
            server.CACHE = old
            shutil.rmtree(root, ignore_errors=True)

    def test_catalog_and_skills(self):
        with urllib.request.urlopen(self.base + "/api/v1/catalog") as response:
            malls = json.load(response)["malls"]
        self.assertEqual(set(malls), set(server.MALLS))
        for mall, value in malls.items():
            self.assertTrue(value["plugins"])
            for plugin in value["plugins"]:
                self.assertIn(plugin["archive_format"], ("zip", "external"))
                url = f"{self.base}/api/v1/malls/{mall}/plugins/{plugin['id']}/skill"
                with urllib.request.urlopen(url) as response:
                    self.assertTrue(response.read().strip())
                if plugin["source"] == "huggingface":
                    self.assertTrue(plugin["external"])
                    self.assertEqual(plugin["archive_format"], "external")
                    self.assertTrue(plugin["download"].startswith("https://huggingface.co/"))

    def test_packages_and_templates(self):
        for mall in ("urdf", "qwiic"):
            for plugin in server.PLUGINS[mall]:
                path = server.plugin_zip(plugin)
                self.assertEqual(path, server.plugin_zip(plugin))
                with zipfile.ZipFile(path) as archive:
                    names = set(archive.namelist())
                    self.assertIn("skill.md", names)
                    self.assertEqual(
                        sum(1 for n in names if n.rsplit("/", 1)[-1].lower() == "skill.md"),
                        1,
                    )
                    self.assertIn("urdf.yaml" if mall == "urdf" else "sensor.yaml", names)
                    sums = archive.read("SHA256SUMS").decode().splitlines()
                    self.assertEqual(len(sums), len(names) - 1)
                    for line in sums:
                        digest, name = line.split("  ", 1)
                        self.assertEqual(digest, hashlib.sha256(archive.read(name)).hexdigest())
                    if mall == "urdf":
                        template = json.loads(archive.read("assembly.template.yaml"))
                        self.assertEqual(template["robot"], plugin["id"])
                        for description in template["descriptions"]:
                            self.assertIn(description["file"], names)
                        with urllib.request.urlopen(f"{self.base}/api/v1/malls/urdf/plugins/{plugin['id']}/assembly") as response:
                            self.assertEqual(template, json.load(response))

    def test_local_onnx_is_standard_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / "policy.onnx"
            model.write_bytes(b"\x08\x07" + b"\x00" * 64)
            plugin = server.make_plugin(
                "model", "local-policy", "policy", version="local", kind="onnx",
                plugin_class="model", local_path=model, archive_format="zip",
                skill="# policy\n\n本地 ONNX 策略模型。\n",
            )
            archive_path = server.plugin_zip(plugin)
            self.assertTrue(archive_path.name.endswith(".zip"))
            with zipfile.ZipFile(archive_path) as archive:
                names = set(archive.namelist())
                self.assertIn("skill.md", names)
                self.assertEqual(
                    sum(1 for n in names if n.rsplit("/", 1)[-1].lower() == "skill.md"),
                    1,
                )
                self.assertIn("model.yaml", names)
                self.assertIn("SHA256SUMS", names)
                self.assertIn("policy.onnx", names)
                sums = archive.read("SHA256SUMS").decode().splitlines()
                self.assertEqual(len(sums), len(names) - 1)
                for line in sums:
                    digest, name = line.split("  ", 1)
                    self.assertEqual(digest, hashlib.sha256(archive.read(name)).hexdigest())
                metadata = json.loads(archive.read("model.yaml"))
                self.assertEqual(metadata["archive_format"], "zip")
                self.assertEqual(metadata["artifact"]["format"], "onnx")

    def test_missing_and_traversal(self):
        for path in ("/api/v1/malls/nope", "/api/v1/malls/qwiic/plugins/missing/download",
                     "/api/v1/malls/urdf/plugins/%2e%2e%2fserver.py/download"):
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(self.base + path)
            self.assertEqual(error.exception.code, 404)

    def test_repacked_xiao_is_installable_and_preserves_firmware(self):
        with tempfile.TemporaryDirectory(dir=str(server.TMP)) as tmp:
            tmp = Path(tmp)
            firmware = b"\x99\x88firmware-bytes" * 64
            upstream = make_fixture_zip(
                tmp / "upstream.zip", "fixture-board", firmware=firmware,
                extra={"nested/docs/skill.md": b"old nested skill"},
            )
            upstream_sha = server.sha256(upstream)
            plugin = make_xiao_plugin("fixture-board", upstream_sha)
            with self.isolated_cache() as cache:
                archive_path = server.repack_xiao(plugin, upstream, upstream_sha)
                self.assertTrue(archive_path.is_file())
                self.assertEqual(archive_path.parent, cache)
                self.assertNotEqual(server.sha256(archive_path), upstream_sha)
                with zipfile.ZipFile(archive_path) as archive:
                    names = archive.namelist()
                    self.assertEqual(len(names), len(set(n.lower() for n in names)))
                    self.assertEqual(
                        sum(1 for n in names if n.rsplit("/", 1)[-1].lower() == "skill.md"),
                        1,
                    )
                    self.assertTrue(archive.read("skill.md").strip())
                    self.assertIn(b"fixture/board", archive.read("skill.md"))
                    manifest = json.loads(archive.read("plugin.yaml"))
                    self.assertEqual(manifest["id"], "fixture-board")
                    self.assertEqual(manifest["version"], "1.1.0")
                    self.assertEqual(manifest["target"]["board"], "fixture/board")
                    self.assertEqual(
                        archive.read("firmware/fixture_board/zephyr.bin"), firmware,
                    )
                    provenance = json.loads(archive.read("provenance.json"))
                    self.assertEqual(provenance["upstream_sha256"], upstream_sha)
                    self.assertIn("https://example.invalid/fixture-board.zip",
                                  provenance["upstream_url"])
                    # skill/SHA256SUMS 均由本服务生成；旧的和嵌套的 skill 被替换。
                    self.assertNotIn("nested/docs/skill.md", names)
                    self.assertEqual(archive.read("SHA256SUMS").count(b"\n"), len(names) - 1)
                    for line in archive.read("SHA256SUMS").decode().splitlines():
                        digest, name = line.split("  ", 1)
                        self.assertEqual(digest, hashlib.sha256(archive.read(name)).hexdigest())
                public = server.public_plugin(plugin)
                self.assertIsNone(public["sha256"])
                self.assertEqual(public["upstream_sha256"], upstream_sha)

    def test_repack_checksum_mismatch_and_cache_invalidation(self):
        with tempfile.TemporaryDirectory(dir=str(server.TMP)) as tmp:
            tmp = Path(tmp)
            upstream = make_fixture_zip(tmp / "upstream.zip", "fixture-board")
            upstream_sha = server.sha256(upstream)
            plugin = make_xiao_plugin("fixture-board", upstream_sha)
            with self.isolated_cache() as cache:
                with self.assertRaises(ValueError):
                    server.repack_xiao(plugin, upstream, "0" * 64)
                self.assertEqual(list(cache.glob("xiao-*.zip")), [])

                first = server.repack_xiao(plugin, upstream, upstream_sha)
                first_hash = server.sha256(first)
                first.unlink()
                rebuilt = server.repack_xiao(plugin, upstream, upstream_sha)
                self.assertEqual(rebuilt, first)
                self.assertEqual(server.sha256(rebuilt), first_hash)
                again = server.repack_xiao(plugin, upstream, upstream_sha)
                self.assertEqual(first, again)
                self.assertEqual(first_hash, server.sha256(again))

                # 元数据变化必须生成新缓存。
                changed = dict(plugin)
                changed["description"] = "changed metadata"
                second = server.repack_xiao(changed, upstream, upstream_sha)
                self.assertNotEqual(second, first)
                self.assertNotEqual(server.sha256(second), first_hash)

                # 上游字节变化（校验值随之变化）也必须生成新缓存。
                upstream2 = make_fixture_zip(
                    tmp / "upstream2.zip", "fixture-board", firmware=b"different",
                )
                upstream2_sha = server.sha256(upstream2)
                plugin2 = make_xiao_plugin("fixture-board", upstream2_sha)
                third = server.repack_xiao(plugin2, upstream2, upstream2_sha)
                self.assertNotEqual(third, first)
                self.assertNotEqual(server.sha256(third), first_hash)

    def test_repack_rejects_unsafe_upstream(self):
        with tempfile.TemporaryDirectory(dir=str(server.TMP)) as tmp:
            tmp = Path(tmp)
            cases = {}
            for label, entries in {
                "traversal": [("../evil.bin", b"x")],
                "absolute": [("/etc/passwd", b"x")],
                "backslash": [("dir\\evil.bin", b"x")],
                "duplicate": [("dup.bin", b"1"), ("dup.bin", b"2")],
                "case-duplicate": [("Dup.bin", b"1"), ("dup.bin", b"2")],
            }.items():
                path = tmp / f"{label}.zip"
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    with zipfile.ZipFile(path, "w") as archive:
                        for name, data in entries:
                            archive.writestr(name, data)
                cases[label] = path
            symlink = tmp / "symlink.zip"
            with zipfile.ZipFile(symlink, "w") as archive:
                info = zipfile.ZipInfo("link.bin")
                info.create_system = 3
                info.external_attr = 0o120777 << 16
                archive.writestr(info, b"target")
            cases["symlink"] = symlink

            for label, upstream in cases.items():
                sha = server.sha256(upstream)
                plugin = make_xiao_plugin(f"fixture-{label}", sha)
                with self.assertRaises(ValueError, msg=label):
                    server.repack_xiao(plugin, upstream, sha)

            oversize = make_fixture_zip(tmp / "oversize.zip", "fixture-oversize",
                                        firmware=b"0123456789")
            sha = server.sha256(oversize)
            plugin = make_xiao_plugin("fixture-oversize", sha)
            old = server.MAX_ARCHIVE_ENTRY_BYTES
            server.MAX_ARCHIVE_ENTRY_BYTES = 4
            try:
                with self.assertRaises(ValueError):
                    server.repack_xiao(plugin, oversize, sha)
            finally:
                server.MAX_ARCHIVE_ENTRY_BYTES = old

    def test_repack_rejects_invalid_manifest(self):
        with tempfile.TemporaryDirectory(dir=str(server.TMP)) as tmp:
            tmp = Path(tmp)
            mismatch = make_fixture_zip(
                tmp / "mismatch.zip", "fixture-board", manifest=False,
                extra={"sensor.yaml": "id: other-plugin\nversion: 1.1.0\n"},
            )
            sha = server.sha256(mismatch)
            plugin = make_xiao_plugin("fixture-board", sha)
            with self.assertRaises(ValueError):
                server.repack_xiao(plugin, mismatch, sha)

            invalid = make_fixture_zip(
                tmp / "invalid.zip", "fixture-board", manifest=False,
                extra={"plugin.yaml": "{not-json"},
            )
            sha = server.sha256(invalid)
            plugin = make_xiao_plugin("fixture-board", sha)
            with self.assertRaises(ValueError):
                server.repack_xiao(plugin, invalid, sha)

    def test_upstream_download_exposes_repacked_hash(self):
        with tempfile.TemporaryDirectory(dir=str(server.TMP)) as tmp:
            tmp = Path(tmp)
            upstream = make_fixture_zip(tmp / "upstream.zip", "fixture-http")
            upstream_sha = server.sha256(upstream)
            plugin = make_xiao_plugin("fixture-http", upstream_sha)
            with self.isolated_cache():
                old_plugins = server.PLUGINS["xiao"]
                old_ensure = server.ensure_remote
                server.PLUGINS["xiao"] = old_plugins + [plugin]
                server.ensure_remote = lambda url, pid, expected=None: upstream
                try:
                    url = f"{self.base}/api/v1/malls/xiao/plugins/fixture-http/download"
                    with urllib.request.urlopen(url) as response:
                        body = response.read()
                        header = response.headers.get("X-Archive-SHA256")
                    self.assertEqual(header, hashlib.sha256(body).hexdigest())
                    self.assertNotEqual(header, upstream_sha)
                    expected = server.repack_xiao(plugin, upstream, upstream_sha)
                    self.assertEqual(header, server.sha256(expected))
                    with zipfile.ZipFile(io.BytesIO(body)) as archive:
                        self.assertTrue(archive.read("skill.md").strip())
                        self.assertIn("plugin.yaml", archive.namelist())
                        self.assertIn("SHA256SUMS", archive.namelist())
                finally:
                    server.PLUGINS["xiao"] = old_plugins
                    server.ensure_remote = old_ensure


if __name__ == "__main__":
    unittest.main()
