# roboparty APT + Winget 静态服务器指南

## 目标架构

一台 Ubuntu 24.04 主机、一个域名、一个 Nginx：

```text
https://apt.roboparty.com/                 -> APT 仓库（reprepro）
https://apt.roboparty.com/roboparty.gpg    -> APT GPG 公钥
https://apt.roboparty.com/winget/          -> Winget 内部静态目录
```

约定：

- APT 必须继续占根路径，`https://apt.roboparty.com/dists/common/Release` 不变。
- Winget 只放 `/winget/`，不做 `winget.roboparty.com`，暂不做 REST source。
- 一台机器、一张证书、一个 vhost；APT 和 Winget 分开目录、分开日志。
- 所有现场命令默认在 APT 服务器上执行，仓库路径为 `/home/robo/roboparty_repo`。

## 前提条件

- 本机就是 APT 服务器。
- 腾讯云安全组已放行 `80/443`，SSH `22` 只对管理来源开放。
- 需要一个 GitHub Token（classic `repo`/`public_repo`，或等效只读 Release 权限）。
- 服务器时间已同步。
- 不要提交私钥、Token、GPG 私钥到 Git。

先确认 DNS 和安全组：

```bash
getent ahostsv4 apt.roboparty.com
curl -I --max-time 5 http://apt.roboparty.com/
curl -I --max-time 5 https://apt.roboparty.com/
```

## 1. 安装系统依赖

```bash
sudo apt-get update
sudo apt-get install -y \
  nginx certbot python3-certbot-nginx \
  reprepro gh python3-yaml curl gnupg
```

不要使用 `pip install pyyaml`：Ubuntu 24.04 的系统 Python 有 PEP 668 限制，服务器已提供 `python3-yaml`。

检查：

```bash
nginx -v
reprepro --version
gh --version
python3 -c 'import yaml; print("pyyaml ok")'
```

## 2. 目录结构

```bash
sudo mkdir -p /srv/apt/conf
sudo mkdir -p /srv/apt-incoming/{common,robopi0,robopi1,robopi2,robopi3,robopi4,x86}
sudo mkdir -p /srv/winget/{manifests,packages}
sudo chown -R "$(id -un):$(id -gn)" /srv/apt /srv/apt-incoming /srv/winget
```

最终布局：

```text
/srv/apt/
  conf/distributions
  dists/
  pool/
  roboparty.gpg
  db/

/srv/apt-incoming/
  common/ robopi0/ robopi1/ robopi2/ robopi3/ robopi4/ x86/

/srv/winget/
  manifests/
  packages/
  index.json
  README.txt
```

## 3. 生成 GPG 密钥

> 生成新密钥意味着所有客户端都要更新公钥。先确认是否已有需要保留的旧密钥。

```bash
gpg --batch --gen-key <<'EOF'
Key-Type: RSA
Key-Length: 4096
Name-Real: RoboParty APT Repository
Name-Email: apt@roboparty.com
Expire-Date: 0
%no-protection
EOF
```

记录 **40 位 fingerprint**，不要只记示例 Key ID：

```bash
gpg --list-keys --with-colons --fingerprint "RoboParty APT Repository"
```

导出并备份私钥到离线安全位置：

```bash
mkdir -p ~/apt-key-backup
chmod 700 ~/apt-key-backup
gpg --export-secret-keys --armor "RoboParty APT Repository" \
  > ~/apt-key-backup/roboparty-apt-secret.asc
chmod 600 ~/apt-key-backup/roboparty-apt-secret.asc
```

备份完成后，尽快把私钥备份移出服务器或加密保存。服务器上的 `~/.gnupg` 也需要备份和权限检查。

## 4. reprepro 配置

编辑 `/srv/apt/conf/distributions`，把 `<FINGERPRINT>` 替换为第 3 步的 40 位 fingerprint：

```bash
sudo tee /srv/apt/conf/distributions > /dev/null <<'EOF'
Origin: RoboParty
Label: RoboParty APT Repository
Codename: common
Architectures: amd64 arm64 source
Components: main
Description: RoboParty common packages
SignWith: <FINGERPRINT>

Origin: RoboParty
Label: RoboParty APT Repository
Codename: robopi0
Architectures: amd64 arm64 source
Components: main
Description: RoboParty RoboPi0 packages
SignWith: <FINGERPRINT>

Origin: RoboParty
Label: RoboParty APT Repository
Codename: robopi1
Architectures: amd64 arm64 source
Components: main
Description: RoboParty RoboPi1 packages
SignWith: <FINGERPRINT>

Origin: RoboParty
Label: RoboParty APT Repository
Codename: robopi2
Architectures: amd64 arm64 source
Components: main
Description: RoboParty RoboPi2 packages
SignWith: <FINGERPRINT>

Origin: RoboParty
Label: RoboParty APT Repository
Codename: robopi3
Architectures: amd64 arm64 source
Components: main
Description: RoboParty RoboPi3 packages
SignWith: <FINGERPRINT>

Origin: RoboParty
Label: RoboParty APT Repository
Codename: robopi4
Architectures: amd64 arm64 source
Components: main
Description: RoboParty RoboPi4 packages
SignWith: <FINGERPRINT>

Origin: RoboParty
Label: RoboParty APT Repository
Codename: x86
Architectures: amd64 source
Components: main
Description: RoboParty x86 packages
SignWith: <FINGERPRINT>
EOF
```

注意：`<FINGERPRINT>` 是占位符，不能原样保留。

## 5. 拉取并注入 deb

### 5.1 设置 Token

```bash
export GH_TOKEN=<你的 GitHub Token>
```

不要写进 shell 历史或仓库；建议临时 `read -s` 或使用凭据管理。

### 5.2 处理代理

`scripts/route_debs.py` 默认直连 GitHub，不再注入 `127.0.0.1:7890`。只有确实需要出站代理时才设置：

```bash
export ROUTE_DEBS_PROXY=http://<proxy-host>:<port>
```

也可以使用标准的 `HTTPS_PROXY`/`https_proxy`。服务器没有代理时不要设置这些变量；直连已在本机验证可用。

### 5.3 下载并按 routing 分拣

```bash
cd /home/robo/roboparty_repo
python3 scripts/route_debs.py routing.yaml /srv/apt-incoming/
```

这会遍历 `routing.yaml` 中的仓库，从 GitHub Release 下载 `.deb`，分拣到对应 suite，并生成 `/srv/apt-incoming/manifest.json`。

### 5.4 注入 reprepro

```bash
cd /home/robo/roboparty_repo
bash scripts/bot_inject.sh
```

`bot_inject.sh` 会：

- 清理 routing 中已不存在的旧包；
- 逐 suite 执行 `reprepro remove` / `includedeb`；
- 执行 `deleteunreferenced`。

检查：

```bash
reprepro -b /srv/apt list common | head -20
reprepro -b /srv/apt list robopi2 | head -20
```

## 6. 发布 GPG 公钥

```bash
gpg --export -a "RoboParty APT Repository" | sudo tee /srv/apt/roboparty.gpg > /dev/null
sudo chmod 644 /srv/apt/roboparty.gpg
```

公钥 URL：

```text
https://apt.roboparty.com/roboparty.gpg
```

客户端安装公钥：

```bash
sudo curl -fsSL https://apt.roboparty.com/roboparty.gpg \
  | sudo gpg --dearmor -o /usr/share/keyrings/roboparty-archive-keyring.gpg
sudo chmod 644 /usr/share/keyrings/roboparty-archive-keyring.gpg
```

> 公钥必须通过 HTTPS 提供。HTTP 分发公钥可以被中间人替换。

## 7. Nginx + HTTPS

### 7.1 先配置 HTTP 站点

写入 `/etc/nginx/sites-available/apt`：

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name apt.roboparty.com;

    root /srv/apt;
    autoindex off;

    location ^~ /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    location ^~ /winget/ {
        alias /srv/winget/;
        autoindex off;
    }

    location / {
        try_files $uri $uri/ =404;
    }
}
```

启用：

```bash
sudo ln -sf /etc/nginx/sites-available/apt /etc/nginx/sites-enabled/apt
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

### 7.2 申请证书

确认 80 端口从公网可达后：

```bash
sudo certbot --nginx -d apt.roboparty.com
sudo certbot renew --dry-run
```

Certbot 会修改站点配置。完成后确认 `/winget/` location 仍然存在；如被覆盖，按下面最终片段补回。

### 7.3 最终配置参考

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name apt.roboparty.com;

    location ^~ /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    location / {
        return 301 https://$host$request_uri;
    }
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name apt.roboparty.com;

    ssl_certificate /etc/letsencrypt/live/apt.roboparty.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/apt.roboparty.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers off;

    root /srv/apt;
    autoindex off;
    server_tokens off;

    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy no-referrer always;

    location = /roboparty.gpg {
        try_files $uri =404;
        add_header Cache-Control "public, max-age=300";
    }

    location ^~ /winget/ {
        alias /srv/winget/;
        autoindex off;
        add_header Cache-Control "no-cache" always;
    }

    location / {
        try_files $uri $uri/ =404;
    }

    location ~ /\. {
        deny all;
    }
}
```

验证：

```bash
sudo nginx -t
sudo systemctl reload nginx
curl -I https://apt.roboparty.com/dists/common/Release
curl -I https://apt.roboparty.com/roboparty.gpg
curl -I https://apt.roboparty.com/winget/README.txt
```

## 8. 防火墙

先确认云安全组，再启用主机防火墙。不要把 SSH 对全世界开放：

```bash
sudo ufw allow from <管理IP> to any port 22 proto tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo ufw status
```

如果管理 IP 不固定，先保留控制台/VNC 兜底，避免锁死。

## 9. Winget 静态目录

当前只做静态，不做 REST source。

放入占位文件：

```bash
cat | sudo tee /srv/winget/README.txt > /dev/null <<'EOF'
RoboParty internal Winget repository.

URL:
  https://apt.roboparty.com/winget/

Usage:
  Download a package zip from packages/ and run:
    winget install --manifest .\manifest
EOF
```

目录约定：

```text
/srv/winget/
  manifests/<Publisher>/<Package>/<Version>/*.yaml
  packages/<Package>/<Version>.zip
  index.json
  README.txt
```

客户端流程：

```powershell
Invoke-WebRequest `
  https://apt.roboparty.com/winget/packages/MyApp/1.0.0.zip `
  -OutFile MyApp-1.0.0.zip

Expand-Archive MyApp-1.0.0.zip -DestinationPath .\MyApp-1.0.0
winget install --manifest .\MyApp-1.0.0\manifest
```

注意：

- 静态目录不能直接作为 `winget source add` 的 REST source。
- 内部使用 `winget install --manifest` 即可。
- 如果以后必须支持 `winget search` / `winget install <id>`，再在 `/winget/api/` 反代 REST 服务；当前不做。

## 10. 客户端 APT 配置

```bash
sudo curl -fsSL https://apt.roboparty.com/roboparty.gpg \
  | sudo gpg --dearmor -o /usr/share/keyrings/roboparty-archive-keyring.gpg

echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/roboparty-archive-keyring.gpg] https://apt.roboparty.com common main" \
  | sudo tee /etc/apt/sources.list.d/roboparty.list
sudo apt-get update
```

验证签名：

```bash
curl -fsSL https://apt.roboparty.com/dists/common/InRelease | gpg --verify
```

## 11. 验收

```bash
# APT
reprepro -b /srv/apt list common | head
curl -I https://apt.roboparty.com/dists/common/Release
curl -I https://apt.roboparty.com/dists/common/InRelease
curl -I https://apt.roboparty.com/roboparty.gpg
curl -I https://apt.roboparty.com/winget/README.txt

# 证书
sudo certbot renew --dry-run

# 本机模拟客户端
sudo apt-get update
```

完整验收还应在一台独立的 amd64 和一台 arm64 机器上安装至少一个包。

## 12. GPG 公钥变更

如果重新生成过密钥，所有客户端必须更新：

```bash
sudo curl -fsSL https://apt.roboparty.com/roboparty.gpg \
  | sudo gpg --dearmor -o /usr/share/keyrings/roboparty-archive-keyring.gpg
sudo apt-get update
```

旧 key 不要继续保留在客户端，否则可能出现 `NO_PUBKEY` 或签名冲突。

## 13. 后续维护

### 新包上线

1. 子仓库打 tag 并 push；
2. GitHub Actions 生成 `.deb` 和 Release；
3. 在服务器执行：

```bash
cd /home/robo/roboparty_repo
export GH_TOKEN=<token>
export HTTPS_PROXY=
export https_proxy=

python3 scripts/route_debs.py routing.yaml /srv/apt-incoming/
bash scripts/bot_inject.sh
```

### 添加新仓库

1. 编辑 `routing.yaml`，在 `repos:` 下追加仓库；
2. 在对应 suite 下添加 pattern；
3. 重新执行 `route_debs.py` 和 `bot_inject.sh`。

### 只注入已有包

```bash
cd /home/robo/roboparty_repo
bash scripts/bot_inject.sh
```

### 自动同步（systemd timer）

服务器使用 `/home/robo/roboparty_repo/scripts/apt_sync.sh` 定时拉取 GitHub
Release 并注入 APT。

```text
/etc/systemd/system/roboparty-apt-sync.service
/etc/systemd/system/roboparty-apt-sync.timer
```

- 每天 `04:30` 运行；
- 随机延迟最多 15 分钟；
- `flock` 防止并发；
- 失败时保留现有仓库，不删除旧包；
- 日志用 `journalctl` 查看。

手动触发一次：

```bash
sudo systemctl start roboparty-apt-sync.service
sudo journalctl -u roboparty-apt-sync.service -n 100 --no-pager
```

查看下一次运行：

```bash
systemctl list-timers roboparty-apt-sync.timer
```

需要代理时在 service 中设置：

```text
Environment=ROUTE_DEBS_PROXY=http://<proxy>:<port>
```

无代理时保持为空，脚本默认直连 GitHub。

### Winget 新包

1. 生成 manifest；
2. 打包 `packages/<Package>/<Version>.zip`；
3. 上传到 `/srv/winget/packages/`；
4. 更新 `/srv/winget/manifests/` 和 `index.json`；
5. 在 Windows 测试机执行 `winget install --manifest` 验证。

## 14. 安全与备份

- `/srv/apt`、`/srv/winget` 分开权限，Nginx 只读。
- GPG 私钥权限 600，备份离线保存；不要把私钥放进仓库。
- 不要通过 HTTP 分发公钥。
- 关闭 `autoindex`，拒绝隐藏文件。
- 对 `/winget/` 单独限流，避免大文件下载影响 APT。
- 定期备份：
  - `/srv/apt`
  - `/srv/winget`
  - `/etc/nginx`
  - `/etc/letsencrypt`
  - `~/.gnupg` 或离线私钥备份
- 监控：
  - `dists/common/Release` 200
  - `roboparty.gpg` 200
  - `/winget/README.txt` 200
  - 证书剩余天数
  - 磁盘使用率
  - Nginx 5xx 和 404 异常

## 15. 当前不做的事

- 不创建 `winget.roboparty.com`。
- 不部署 Winget REST source。
- 不把 APT 改成 `/apt/` 子路径。
- 不在未确认云安全组前启用 `ufw` 或申请证书。
- 不把 GPG 私钥、GitHub Token、SSH 私钥写入仓库。

## 16. 构建与发布矩阵

更新：2026-10-25。本节吸收原 `build.md` 的构建/发布信息。

### 发布范围

- 正式构建面向 Ubuntu 24.04（Noble）。
- `Architecture: all` 只构建一次。
- C/C++ `Architecture: any` 包分别构建 amd64 和 arm64。
- 板卡专用包只构建 arm64。
- `roboparty-camera`、`roboparty_rp_head`、`roboparty-start` 暂不进入本轮发布。
- `roboparty_deb_template` 是模板，不计入发布包数量。
- `robopi-config` 已废弃，不再构建；配置工具使用 `robopi-tui`。
- `adbd` 源码已加入 `manifest.xml`，APT `common` 已有 `adbd 1.0.4-1`，但暂不计入本表 15 包/22 deb 目标。

当前目标为 **15 个包、22 个 deb**。

### 当前矩阵

| 源码目录 | 包版本 | 架构 | 备注 |
|---|---:|---|---|
| `roboparty_all` | `1.3.2-1` | `all` | metapackage |
| `roboparty_base` | `1.1.8-1` | `all` | 基础系统 |
| `roboparty_description` | `1.2.1-1` | `all` | 模型/描述 |
| `roboparty_example` | `1.1.0-1` | `all` | 示例 |
| `roboparty_rp_server` | `1.4.0-1` | `all` | 运控服务 |
| `roboparty_analyze` | `1.0.14-1` | `all` | GitHub Release 仍落后；当前 APT 为手工注入并 pin 1.0.14 |
| `roboparty_motors` | `2.3.0-1` | `any` | amd64 + arm64 |
| `roboparty_imu` | `1.4.0-1` | `any` | amd64 + arm64 |
| `roboparty_bms` | `1.6.0-1` | `any` | amd64 + arm64 |
| `roboparty_dexhand` | `0.5.0-1` | `any` | amd64 + arm64 |
| `roboparty_inference` | `1.2.1-1` | `any` | amd64 + arm64 |
| `roboparty_lidar` | `0.2.0-1` | `any` | amd64 + arm64；Livox SDK2 + MID-360 |
| `Livox-SDK2` | `1.3.1-1` | `any` | Livox SDK2；RoboParty fork |
| `livox_ros_driver2` | `1.2.6-1` | `any` | 官方驱动；ROS 2 Jazzy，安装到 /opt/roboparty |
| `roboparty_onnxruntime` | `1.21.0-1` | `any` | amd64 + arm64 |
| `robopi_addon` | `1.8.8-1` | `arm64` | 用户态板级工具；最新 release `v1.8.8` |
| `robopi_tui` | `1.1.2` | `all` | 配置/诊断 TUI；`Architecture: all`，arm64 全量插件、x86 仅 `generic` 插件；板端工具 `adbd`/`robopi-addon`/`roboparty-analyze` 在 arm64 以 Recommends 安装 |

### 包名说明

- `robopi-config`：已废弃，APT 和 routing 中已移除。
- `robopi-tui`：当前配置工具；核心依赖 `python3`、`dialog`、`systemd`，板端工具 `adbd`、`robopi-addon`、`roboparty-analyze` 在 arm64 以 Recommends 安装。自动同步会保留手工注入版本。
- `roboparty-analyze`：正式包名；`robopi-analyze` 过渡包已移除，不再发布。
- `adbd`：源码已进入 manifest，APT 已有 `1.0.4-1`；arm64 上由 `robopi-tui` 的 Recommends 默认安装，不再单独加入安装列表。
- `roboparty-start`（旧名 `roboparty-go`）：当前不进入发布矩阵；自动同步的 stale cleanup 会将其从 APT 移除。
