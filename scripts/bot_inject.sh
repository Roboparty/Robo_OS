#!/bin/bash

# SPDX-License-Identifier: GPL-3.0
# Copyright (C) 2026 wentywenty

#=================================================
# APT 源服务端 Github Action 机器人注入脚本 v1.0
# 用于静默处理 /srv/apt-incoming 目录中的所有 deb 包
# 无需任何交互界面，直接由 Github Action 的流水线B触发
#=================================================

APT_DIR="/srv/apt"
INCOMING_DIR="/srv/apt-incoming"
SUITES=("common" "robopi0" "robopi1" "robopi2" "robopi3" "robopi4" "x86")

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
ROUTING="$REPO_DIR/routing.yaml"
ROUTE_DEBS="$SCRIPT_DIR/route_debs.py"

export PATH=$PATH:/usr/bin:/bin

# 检查必备工具
command -v reprepro &>/dev/null || { echo "❌ [Bot] 缺少必备工具: reprepro"; exit 1; }

if [ ! -d "$INCOMING_DIR" ]; then
    echo "❌ [Bot] 找不到 incoming 目录: $INCOMING_DIR"
    exit 1
fi

echo "========================================"
echo " 🤖 RoboParty APT 自动注入机器人启动"
echo " 时间: $(date)"
echo "========================================"

failures=0

for suite in "${SUITES[@]}"; do
    expected=$(python3 "$ROUTE_DEBS" --print-expected "$ROUTING" "$suite")
    if [ $? -ne 0 ]; then
        echo "❌ [Bot] 读取 routing 失败: $suite"
        failures=$((failures + 1))
        expected=""
    fi
    if [ -n "$expected" ]; then
        current=$(reprepro -b "$APT_DIR" list "$suite" | sed 's/^[^:]*: //' | awk '{print $1}' | sort -u)
        if [ $? -ne 0 ]; then
            echo "❌ [Bot] reprepro list 失败: $suite"
            failures=$((failures + 1))
            current=""
        fi
        if [ -n "$current" ]; then
            stale=$(comm -23 <(echo "$current") <(echo "$expected"))
            if [ -n "$stale" ]; then
                while IFS= read -r pkg; do
                    [ -z "$pkg" ] && continue
                    echo "🧹 清理残留包: $pkg ($suite)"
                    if ! reprepro -b "$APT_DIR" remove "$suite" "$pkg"; then
                        echo "❌ [Bot] 清理失败: $pkg ($suite)"
                        failures=$((failures + 1))
                    fi
                done <<< "$stale"
            fi
        fi
    fi

    shopt -s nullglob
    debs=("$INCOMING_DIR/$suite"/*.deb)
    shopt -u nullglob
    
    if [ ${#debs[@]} -gt 0 ]; then
        echo "📦 发现 [$suite] 源有 ${#debs[@]} 个包裹，正在开始静默注入..."
        for deb in "${debs[@]}"; do
            pkg_name=$(dpkg-deb -f "$deb" Package 2>/dev/null)
            pkg_arch=$(dpkg-deb -f "$deb" Architecture 2>/dev/null)
            pkg_ver=$(dpkg-deb -f "$deb" Version 2>/dev/null)
            pkg_section=$(dpkg-deb -f "$deb" Section 2>/dev/null || true)
            if [ -z "$pkg_name" ] || [ -z "$pkg_arch" ] || [ -z "$pkg_ver" ]; then
                echo "   ❌ 无法解析 deb 元数据: $(basename "$deb")"
                failures=$((failures + 1))
                continue
            fi
            echo "👉 [注入] $pkg_name ($pkg_arch) $pkg_ver : $(basename "$deb")"

            # Some D-Robotics debs omit Section/Priority; reprepro rejects
            # them with "No section given". Supply Debian defaults only then.
            reprepro_extra=()
            if [ -z "$pkg_section" ]; then
                reprepro_extra=(-S misc -P optional)
            fi

            # 先入库新包；旧包在新包成功入库前保留，避免入库失败后包消失。
            injected=0
            if reprepro -b "$APT_DIR" "${reprepro_extra[@]}" includedeb "$suite" "$deb"; then
                injected=1
            else
                # 同包同架构同版本重建会因 Hash 冲突被拒：仅移除该版本后重试。
                echo "   ⚠️ 首次入库失败，尝试替换同版本旧包后重试..."
                if [ "$pkg_arch" = "all" ]; then
                    reprepro -b "$APT_DIR" removefilter "$suite" \
                        "Package (== $pkg_name), Version (== $pkg_ver)" >/dev/null 2>&1
                else
                    reprepro -b "$APT_DIR" removefilter "$suite" \
                        "Package (== $pkg_name), Architecture (== $pkg_arch), Version (== $pkg_ver)" >/dev/null 2>&1
                fi
                if reprepro -b "$APT_DIR" "${reprepro_extra[@]}" includedeb "$suite" "$deb"; then
                    injected=1
                fi
            fi

            if [ "$injected" -eq 1 ]; then
                # 成功入库后再清理同包的其它版本。
                if [ "$pkg_arch" = "all" ]; then
                    # 新包改为 Architecture: all 时，清掉旧的按架构构建版本。
                    reprepro -b "$APT_DIR" removefilter "$suite" \
                        "Package (== $pkg_name), Architecture (!= all)" >/dev/null 2>&1
                    reprepro -b "$APT_DIR" removefilter "$suite" \
                        "Package (== $pkg_name), Version (!= $pkg_ver)" >/dev/null 2>&1
                else
                    reprepro -b "$APT_DIR" removefilter "$suite" \
                        "Package (== $pkg_name), Architecture (== $pkg_arch), Version (!= $pkg_ver)" >/dev/null 2>&1
                fi
                rm -f "$deb"
                echo "   ✅ 入库成功并已清理缓存: $(basename "$deb")"
            else
                echo "   ❌ 入库失败，已保留文件: $(basename "$deb")"
                failures=$((failures + 1))
            fi
        done
    fi
done

# 清理无引用包
echo "🧹 正在执行 deleteunreferenced 回收无效空间..."
if ! reprepro -b "$APT_DIR" deleteunreferenced; then
    echo "❌ [Bot] deleteunreferenced 失败"
    failures=$((failures + 1))
fi

if [ "$failures" -gt 0 ]; then
    echo "❌ 自动入库流程结束，$failures 项失败"
    exit 1
fi
echo "🎉 自动入库流程执行完毕！"
