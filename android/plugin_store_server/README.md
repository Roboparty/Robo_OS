# plugin_store_server

轻量 HTTP 插件目录服务，供 Android App 的插件商城读取 catalog、下载 ZIP、读取 skill 与 URDF 装配模板。服务端只读本地目录和已缓存的上游制品，不执行包内文件、不操作硬件。

本文描述 2026-11-12 的 `server.py` 实际行为。目标 schema、v2 统一目录与兼容矩阵尚未实现，见仓库顶层 [插件包格式规范.md](../插件包格式规范.md)。

## 启动

```bash
cd plugin_store_server
python3 server.py --host 127.0.0.1 --port 8765
```

- 默认 `--host 0.0.0.0 --port 8765`。服务无鉴权，建议只绑定本机或可信网段。
- 需要 Python 3.10+（类型标注使用 `Path | None`）。PyYAML 在 `load_bundle_meta()` 中使用，但当前路由未调用它；缺失 PyYAML 不影响现有接口启动。
- `build_plugins()` 在进程启动时执行一次：读取 `data/catalog.json`、`robopoly/sensor_bundles`、URDF 根目录和本地模型目录。运行中增删这些内容需要重启进程。
- 启动后打印监听地址和各商城条目数，并在后台线程执行 `prewarm()`。

目录关系：

```text
<repo>/android/plugin_store_server/server.py
<repo>/android/plugin_store_server/data/catalog.json
<repo>/android/robopoly/sensor_bundles/
<repo>/test/tmp/plugin_store/cache/          # 生成包与上游包缓存
```

`TMP` 由 `REPO = ANDROID.parent` 推导，因此缓存位于 `android` 的父目录下，而不是 `plugin_store_server` 内。

## 路由

只实现 GET；其他方法由 `BaseHTTPRequestHandler` 返回 501。

| 方法 | 路径 | 返回 |
|---|---|---|
| GET | `/health` | `{"ok":true,"malls":{mall:count}}` |
| GET | `/api/v1/malls` | `{"malls":[{id,name,description,count}]}` |
| GET | `/api/v1/catalog` | `{"malls":{mall:{name,description,plugins:[...]}}}` |
| GET | `/api/v1/malls/{mall}?q=` | `{"mall":...,"plugins":[...]}`；`q` 对 id/name/description 做子串过滤 |
| GET | `/api/v1/malls/{mall}/plugins` | 完整列表；当前不应用 `q` |
| GET | `/api/v1/malls/{mall}/plugins/{id}` | `{"plugin":{...}}` |
| GET | `/api/v1/malls/{mall}/plugins/{id}/skill` | `text/markdown`，附件名 `<id>-skill.md` |
| GET | `/api/v1/malls/{mall}/plugins/{id}/download` | ZIP 字节流或 302 |
| GET | `/api/v1/malls/urdf/plugins/{id}/assembly` | JSON 装配模板（仅 urdf） |

路径段经过 `unquote`，但不会用包 id 拼接服务器本地路径；catalog 公共响应隐藏 `local_path`、`skill` 和 `expected_sha256`。

公共插件字段包括 `mall`、`id`、`name`、`version`、`kind`、`description`、`install_level`、`class`、`source`、`repo`、`archive_format`、`download`；本地条目可带 `size`，catalog 条目可带 `target`/`install`；`archive_format=zip` 时附 `checksums`；HF 条目附 `external_url`/`external`；URDF 条目附 `assembly_template`。

上游 XIAO 条目额外带 `upstream_url` 与 `upstream_sha256` 作为来源信息。由于下载包会被重新打包，其 ZIP 级 `sha256` 固定为 `null`，不会把上游哈希冒充成品哈希；实际成品哈希在下载响应头 `X-Archive-SHA256` 中返回。

## 下载语义

`download()` 按以下顺序处理：

1. **本地目录或 URDF/Qwiic 条目**：调用 `plugin_zip()` 现场生成 ZIP，返回 `application/zip`，`Content-Disposition: attachment; filename="<id>.zip"`。本地路径不存在返回 404 `local asset missing`；打包异常返回 500 `package failed: ...`。
2. **`external` 条目（HuggingFace 等）**：返回 302，`Location` 指向 `external_url`。服务端不下载、不校验、不重打包。
3. **上游 GitHub Release ZIP（XIAO 固件）**：先调用 `ensure_remote()` 获取上游 ZIP；命中缓存且校验值匹配则复用，否则下载并按 catalog `sha256` 校验后写入缓存。随后调用 `repack_xiao()` 重新打包并返回成品。失败返回 502 `upstream failed: ...`。
4. 以上都不满足：404 `no downloadable asset`。

`plugin_zip()` 生成的包：

- 根目录必须有非空 `skill.md`，否则抛错（实际由调用方处理为 500）。
- ONNX 条目：`model.yaml`、非空 `skill.md`、`SHA256SUMS`、原 `.onnx`；下载名固定 `.zip`。
- URDF 条目：原 URDF/网格文件、`urdf.yaml`、`assembly.template.yaml`、非空 `skill.md`、`SHA256SUMS`。
- 其他（Qwiic/sensor bundle）：原 bundle 文件、`sensor.yaml`（已有则复制，否则生成 JSON）、非空 `skill.md`、`SHA256SUMS`。
- 载荷中任何层级名为 `skill.md`/`SHA256SUMS`/`SHA256SUMS.txt` 的文件都会被跳过，确保成品只有一个根目录 `skill.md`。
- `SHA256SUMS` 覆盖除自身外所有写入文件；载荷文件按路径排序，extras 按写入顺序。`package_files()` 跳过 `.git`、`__pycache__`、`.DS_Store`。

生成的 ZIP 文件名带内容指纹：对 extras 字节和 `package_fingerprint()`（相对路径、大小、mtime_ns）取 sha256 前 20 位；同一指纹命中缓存时直接复用。`PACKAGE_LOCK` 串行化生成，先写 `.part` 再 `os.replace`。

### XIAO 上游重打包

`repack_xiao()` 只读取上游 ZIP 条目并写入新的标准 ZIP，不把上游解压到目录。处理后成品满足 Android 安装器要求：

- 根目录恰好一个非空 `skill.md`，按板卡（`target.board`）、传输/刷写方式、安装等级和 `install` 命令生成；上游任何层级的旧 `skill.md` 都会被丢弃。
- 根目录生成 `plugin.yaml`（JSON 内容，同时是合法 YAML），包含 `id`、`version`、`target`、`kind`、`checksums` 等；安装器会校验其 `id`/`version` 与目录一致。上游已有的其他 manifest（如 `sensor.yaml`）会保留，但必须先通过同样的 `id`/`version` 校验。
- 原上游载荷（`README.md`、`board.txt`、`firmware/**.bin|hex` 等）字节保持不变；只重新压缩，不改写固件内容。
- 生成 `provenance.json`，记录上游 URL、上游 `sha256`、上游字节数与重打包版本。
- 重新生成 `SHA256SUMS`，覆盖除自身外的全部文件；上游旧的校验和文件（任意层级）会被丢弃。

安全约束在写入前执行：拒绝绝对路径、`..`、反斜杠、控制字符、大小写重复路径、符号链接、特殊文件、加密条目，以及超过条目数/单文件/总解压大小限制的上游包。重打包前还会再次核对 catalog 中的上游 `sha256`，不匹配即拒绝，不会生成或复用成品。

重打包缓存名为 `xiao-<safe-id>-<fingerprint>.zip`，fingerprint 覆盖上游 `sha256`、`WRAPPER_VERSION` 与生成的 `skill.md`/`plugin.yaml`/`provenance.json` 字节；上游字节或插件元数据变化都会自然生成新文件。输出使用固定 ZIP 时间戳与稳定排序，重复构建得到相同字节。

## 错误码

| 状态 | 响应 | 场景 |
|---:|---|---|
| 404 | `{"error":"mall not found"}` | 未知商城 |
| 404 | `{"error":"plugin not found"}` | 未知插件 id |
| 404 | `{"error":"not found"}` | 未匹配的 GET 路径 |
| 404 | `{"error":"local asset missing"}` | 本地条目登记的路径不存在 |
| 404 | `{"error":"no downloadable asset"}` | 既无本地路径、external，也无 upstream |
| 500 | `{"error":"package failed: ..."}` | skill 为空、ONNX 缺失、ZIP/URDF 解析失败 |
| 502 | `{"error":"upstream failed: ..."}` | 上游下载失败、sha256 不匹配、不安全 ZIP、manifest 不一致、重打包失败 |

JSON 响应带 `Access-Control-Allow-Origin: *`；流式 ZIP 额外带 `Cache-Control: public, max-age=60`。

## 缓存与预热

- `prewarm()` 在启动线程中为所有带 `local_path` 的条目预生成包；目录或 `archive_format=zip` 的条目都会调用 `plugin_zip()`。
- 设置 `GITHUB_TOKEN` 时，远程 XIAO 条目会下载并重打包（3 个工作线程）；没有 token 时只重打包已存在的上游缓存，不发起网络请求。失败只记录日志，不影响服务启动。
- 上游缓存文件名为 `remote-<sha256(url)[:16]>-<id>.zip`；若 catalog 提供 `expected_sha256`，命中或下载后都会校验，不匹配则删除并重新下载。
- 重打包缓存文件名为 `xiao-<safe-id>-<fingerprint>.zip`；fingerprint 同时包含上游哈希与生成内容，不会命中旧元数据或旧上游的缓存。
- 本地生成包与重打包成品在 catalog 中的 ZIP 级 `sha256` 均为 `null`，以包内 `SHA256SUMS` 为准；上游 XIAO 下载响应提供 `X-Archive-SHA256` 与 `ETag` 作为成品哈希。

## GITHUB_TOKEN 与权限

- 服务只读取环境变量 `GITHUB_TOKEN`，不会写入响应或日志。
- 仅当条目存在 `upstream_url` 时使用。设置 token 后，所有上游 URL 都会走 GitHub Release 资产 API；非 GitHub Release 资产 URL 会失败并返回 502。不要给非 GitHub 上游配置 token。
- 公开仓库通常无需 token；私有仓库需要可读取 Releases 资产的权限（classic token 的 `repo`，或 fine-grained token 的 Contents: Read）。token 不足时表现为 `upstream failed`。
- 服务端 HTTP API 没有 Bearer/Token 鉴权；App 的 MCP token 属于 App 内置 MCP 服务器，不作用于本服务。

## Catalog 组成与数量

`build_plugins()` 的输入：

- `data/catalog.json` 的 `modules`：`class=xiao && kind=firmware` → xiao；`class=xiao` 其他 → qwiic；`class=dev` → robotd；`class=huggingface` → model（external）。
- 静态 `QWIIC` 列表：`nst175`、`atecc608a`、`ch224a`、`ch338f`、`i2c-scan`；已有 bundle 的同 id 条目优先。
- `robopoly/sensor_bundles`：`adc-48v`、`hipnuc-imu`、`nst175`、`tws-bms`。
- `URDF_ROOTS` 四路径：`rpo-description`、`rp1-description`、`rp-hand-left`、`rp-hand-right`；不存在的路径跳过。
- `robopoly/build/*/assets/rpo/models/*.onnx` 中最新目录的 ONNX。

2026-11-12 本机运行中的 `/health`：`urdf 4`、`model 25`、`xiao 14`、`qwiic 7`、`robotd 1`。数量依赖本地路径和 catalog，不是固定值。

## 测试

```bash
cd plugin_store_server
python3 -m unittest -v test_server.py
```

测试在 `127.0.0.1:0` 启动临时 `ThreadingHTTPServer`，临时文件位于 `test/tmp/plugin_store/`，覆盖：

- catalog/skill 路由与非空 skill；
- URDF/Qwiic ZIP 的单一非空 `skill.md`、清单、`SHA256SUMS` 全覆盖与 `assembly.template.yaml`；
- 本地 ONNX 标准 ZIP；
- 未知商城/插件与路径穿越返回 404；
- XIAO 固件重打包：固件字节保持不变、单一 `skill.md`、`plugin.yaml` 的 id/version、`SHA256SUMS` 全覆盖、`provenance.json`、成品哈希不等于上游哈希；
- 上游校验和不匹配拒绝、元数据/上游变化导致缓存失效、同一输入缓存命中；
- 上游 ZIP 的路径穿越、绝对路径、反斜杠、大小写重复、符号链接、超限条目全部拒绝；
- 上游 manifest 的 id/version 不一致或 JSON 无效时拒绝；
- 下载响应头 `X-Archive-SHA256` 与实际响应体哈希一致。

2026-11-12 本机结果：9 个测试通过。测试只使用本地 fixture ZIP，不访问外部网络，也不验证上游私有仓库。

## 边界

- 未实现 `/api/v2/*`、profile 兼容矩阵、catalog revision、sensor/transport 目标 schema。
- `external` 条目只做 302；上游 XIAO ZIP 会重打包为标准包，但只校验归档级 `sha256`，不会验证上游 ZIP 内部旧的 `SHA256SUMS` 或固件签名。
- 路由层只做路径段匹配；上游 ZIP 的路径穿越、符号链接、重复路径与大小限制在重打包阶段拒绝，返回 502。服务不把上游内容解压到磁盘。
- 服务无 TLS、无鉴权；生产部署应置于反向代理或可信网络内。

