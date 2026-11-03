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
SUITES=("common" "robopi0" "robopi1" "robopi2" "robopi3" "x86")

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
ROUTING="$REPO_DIR/routing.yaml"
ROUTE_DEBS="$SCRIPT_DIR/route_debs.py"

export PATH=$PATH:/usr/bin:/bin

# 检查必备工具
command -v reprepro &>/dev/null || { echo "❌ [Bot] 缺少必备工具: reprepro"; exit 1; }

if [ ! -d "$INCOMING_DIR" ]; then
    echo "⚠️ [Bot] 找不到 incoming 目录: $INCOMING_DIR，跳过注入。"
    exit 0
fi

echo "========================================"
echo " 🤖 RoboParty APT 自动注入机器人启动"
echo " 时间: $(date)"
echo "========================================"

for suite in "${SUITES[@]}"; do
    expected=$(python3 "$ROUTE_DEBS" --print-expected "$ROUTING" "$suite" 2>/dev/null)
    if [ -n "$expected" ]; then
        current=$(reprepro -b "$APT_DIR" list "$suite" 2>/dev/null | sed 's/^[^:]*: //' | awk '{print $1}' | sort -u)
        if [ -n "$current" ]; then
            stale=$(comm -23 <(echo "$current") <(echo "$expected"))
            if [ -n "$stale" ]; then
                while IFS= read -r pkg; do
                    [ -z "$pkg" ] && continue
                    echo "🧹 清理残留包: $pkg ($suite)"
                    reprepro -b "$APT_DIR" remove "$suite" "$pkg" >/dev/null 2>&1
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
            if [ -z "$pkg_name" ] || [ -z "$pkg_arch" ] || [ -z "$pkg_ver" ]; then
                echo "   ❌ 无法解析 deb 元数据，跳过: $(basename "$deb")"
                continue
            fi
            echo "👉 [注入] $pkg_name ($pkg_arch) $pkg_ver : $(basename "$deb")"

            # 先入库新包；旧包在新包成功入库前保留，避免入库失败后包消失。
            injected=0
            if reprepro -b "$APT_DIR" includedeb "$suite" "$deb"; then
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
                if reprepro -b "$APT_DIR" includedeb "$suite" "$deb"; then
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
            fi
        done
    fi
done

# 清理无引用包
echo "🧹 正在执行 deleteunreferenced 回收无效空间..."
reprepro -b "$APT_DIR" deleteunreferenced >/dev/null 2>&1

echo "🎉 自动入库流程执行完毕！"
