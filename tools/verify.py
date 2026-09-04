#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
规则安全校验器
================================================================================

每日自动更新会引入上游的新规则，存在误杀关键业务域名的风险。
本校验器在构建后运行，一旦发现问题即返回非零退出码，阻断提交。

检查项
------------------------------------------------------------------------------
1. 关键域覆盖检查：是否存在规则把支付 / 登录 / 系统服务等主域整体拦截
2. 语法检查：规则类型是否合法、IP-CIDR 是否带掩码、域名是否含非法字符
3. 规模检查：规则量是否异常（过大说明上游污染，过小说明抓取失败）
4. 重叠检查：分流规则内部是否存在可被更短后缀覆盖的冗余项

用法
------------------------------------------------------------------------------
    python tools/verify.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILTER_DIR = ROOT / "quantumultx" / "filter"

# 一旦被整体拦截即视为严重事故的主域
CRITICAL_DOMAINS = [
    # Apple 系统服务
    "apple.com", "icloud.com", "mzstatic.com", "apple-cloudkit.com",
    "apple-dns.net", "itunes.apple.com", "apps.apple.com",
    # 支付
    "alipay.com", "alipayobjects.com", "tenpay.com", "unionpay.com",
    "weixin.qq.com", "pay.weixin.qq.com",
    # 通讯
    "wx.qq.com", "qq.com",
    # 主流业务主域
    "taobao.com", "tmall.com", "jd.com", "baidu.com", "bilibili.com",
    "zhihu.com", "weibo.com", "meituan.com", "dianping.com", "ctrip.com",
    "12306.cn", "amap.com", "xiaomi.com", "huawei.com", "douyin.com",
    "toutiao.com", "kuaishou.com", "pinduoduo.com", "vip.com", "163.com",
    # 基础设施
    "github.com", "githubusercontent.com", "cloudflare.com", "jsdelivr.net",
]

VALID_TYPES = {"HOST", "HOST-SUFFIX", "HOST-KEYWORD", "IP-CIDR", "IP6-CIDR"}
DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]*[a-z0-9])?)*$")
CIDR_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}/\d{1,2}$")
CIDR6_RE = re.compile(r"^[0-9a-f:.]+/\d{1,3}$", re.IGNORECASE)

# 各产物期望的规模区间（条），超出即告警——通常是上游数据异常
EXPECTED_SIZE = {
    "Splash-Killer.list": (200, 5000),
    "AdBlock-Lite.list": (5000, 80000),
    "AdBlock-Full.list": (50000, 600000),
    "BlockHttpDNS.list": (20, 500),
}


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def load_rules(path: Path) -> list[tuple[str, str]]:
    rules = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split(",")
        if len(parts) < 3:
            continue
        rules.append((parts[0].upper(), parts[1].lower()))
    return rules


def check_critical(path: Path, rules: list[tuple[str, str]], rep: Report) -> None:
    """检查是否有规则整体覆盖了关键主域。"""
    for crit in CRITICAL_DOMAINS:
        for rtype, value in rules:
            if rtype == "HOST-KEYWORD":
                # 关键词规则命中关键域，风险极高
                if value and value in crit:
                    rep.error(
                        f"{path.name}: 关键词规则 `{value}` 命中关键域 {crit}"
                    )
            elif crit == value or crit.endswith("." + value):
                rep.error(
                    f"{path.name}: `{rtype},{value}` 会整体拦截关键域 {crit}"
                )


def check_syntax(path: Path, rules: list[tuple[str, str]], rep: Report) -> None:
    bad_type = bad_domain = bad_cidr = 0
    for rtype, value in rules:
        if rtype not in VALID_TYPES:
            bad_type += 1
            continue
        if rtype in {"HOST", "HOST-SUFFIX"}:
            if not DOMAIN_RE.match(value):
                bad_domain += 1
        elif rtype == "IP-CIDR" and not CIDR_RE.match(value):
            bad_cidr += 1
        elif rtype == "IP6-CIDR" and not CIDR6_RE.match(value):
            bad_cidr += 1
    if bad_type:
        rep.error(f"{path.name}: {bad_type} 条规则类型非法")
    if bad_domain:
        rep.error(f"{path.name}: {bad_domain} 条域名格式非法")
    if bad_cidr:
        rep.error(f"{path.name}: {bad_cidr} 条 CIDR 格式非法")


def check_size(path: Path, rules: list[tuple[str, str]], rep: Report) -> None:
    bounds = EXPECTED_SIZE.get(path.name)
    if not bounds:
        return
    lo, hi = bounds
    n = len(rules)
    if n < lo:
        rep.error(f"{path.name}: 规则数 {n:,} 低于期望下限 {lo:,}（疑似上游抓取失败）")
    elif n > hi:
        rep.warn(f"{path.name}: 规则数 {n:,} 超过期望上限 {hi:,}（疑似上游数据异常）")


def check_redundant(path: Path, rules: list[tuple[str, str]], rep: Report) -> None:
    """检查是否存在可被更短后缀覆盖的冗余规则。"""
    suffixes = {v for t, v in rules if t == "HOST-SUFFIX"}
    if not suffixes:
        return
    redundant = 0
    sample = []
    for rtype, value in rules:
        if rtype != "HOST-SUFFIX":
            continue
        labels = value.split(".")
        for i in range(1, len(labels)):
            ancestor = ".".join(labels[i:])
            if ancestor in suffixes:
                redundant += 1
                if len(sample) < 3:
                    sample.append(f"{value} 被 {ancestor} 覆盖")
                break
    if redundant:
        rep.warn(f"{path.name}: {redundant} 条后缀规则冗余（例：{'; '.join(sample)}）")


def main() -> int:
    if not FILTER_DIR.exists():
        print("[verify] ✗ 未找到产物目录，请先运行 build.py")
        return 1

    rep = Report()
    files = sorted(FILTER_DIR.glob("*.list"))
    if not files:
        print("[verify] ✗ 产物目录为空")
        return 1

    for path in files:
        rules = load_rules(path)
        check_critical(path, rules, rep)
        check_syntax(path, rules, rep)
        check_size(path, rules, rep)
        check_redundant(path, rules, rep)
        print(f"[verify] 已检查 {path.name:24s} {len(rules):>7,d} 条")

    print()
    for w in rep.warnings:
        print(f"[verify] ⚠ 警告  {w}")
    for e in rep.errors:
        print(f"[verify] ✗ 错误  {e}")

    if rep.errors:
        print(f"\n[verify] 校验未通过：{len(rep.errors)} 项错误，已阻断提交")
        return 1

    print(f"[verify] ✓ 校验通过（{len(rep.warnings)} 项警告，不影响发布）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
