#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Quantumult X 去广告规则构建器
================================================================================

从多个上游开源规则源抓取数据，经过「归一化 → 白名单过滤 → 去重压缩 → 分级」
四道工序，产出适合国内网络环境的 Quantumult X 分流规则、重写规则与 MITM 清单。

设计要点
------------------------------------------------------------------------------
1. 归一化：不同上游格式（QX / anti-AD / 纯域名）统一成内部 Rule 结构
2. 白名单：剔除命中关键业务域名的规则，避免误杀支付/登录/推送
3. 去重压缩：HOST-SUFFIX 可覆盖其所有子域，据此剪除冗余的 HOST / 更长 SUFFIX
4. 分级输出：Splash（开屏专项）/ Lite（日常推荐）/ Full（完整）三档

用法
------------------------------------------------------------------------------
    python tools/build.py                 # 构建全部产物
    python tools/build.py --no-fetch      # 仅用本地缓存重新构建
    python tools/build.py --tier lite     # 只构建指定档位
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import hashlib
import os
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterable

# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".cache" / "upstream"
DATA = ROOT / "tools" / "data"
OUT_FILTER = ROOT / "quantumultx" / "filter"
OUT_REWRITE = ROOT / "quantumultx" / "rewrite"
OUT_MITM = ROOT / "quantumultx" / "mitm"
OUT_SCRIPT = ROOT / "quantumultx" / "script"

CACHE.mkdir(parents=True, exist_ok=True)

CST = timezone(timedelta(hours=8))
BUILD_TIME = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
BUILD_DATE = datetime.now(CST).strftime("%Y-%m-%d")

# 上游源定义：(本地缓存名, URL)
SOURCES: dict[str, str] = {
    # blackmatrix7 / ios_rule_script —— 主力源，Quantumult X 原生格式
    "bm7_ad_lite": "https://raw.githubusercontent.com/blackmatrix7/ios_rule_script/master/rule/QuantumultX/AdvertisingLite/AdvertisingLite.list",
    "bm7_ad_full": "https://raw.githubusercontent.com/blackmatrix7/ios_rule_script/master/rule/QuantumultX/Advertising/Advertising.list",
    "bm7_httpdns": "https://raw.githubusercontent.com/blackmatrix7/ios_rule_script/master/rule/QuantumultX/BlockHttpDNS/BlockHttpDNS.list",
    "bm7_privacy": "https://raw.githubusercontent.com/blackmatrix7/ios_rule_script/master/rule/QuantumultX/Privacy/Privacy.list",
    "bm7_hijack": "https://raw.githubusercontent.com/blackmatrix7/ios_rule_script/master/rule/QuantumultX/Hijacking/Hijacking.list",
    "bm7_rewrite_ad": "https://raw.githubusercontent.com/blackmatrix7/ios_rule_script/master/rewrite/QuantumultX/Advertising/Advertising.conf",
    "bm7_rewrite_script": "https://raw.githubusercontent.com/blackmatrix7/ios_rule_script/master/rewrite/QuantumultX/AdvertisingScript/AdvertisingScript.conf",
    # anti-AD —— 中文区命中率最高的广告域名库，已提供 QX 格式
    "antiad_quanx": "https://raw.githubusercontent.com/privacy-protection-tools/anti-AD/master/anti-ad-quanx.txt",
}

# 下载失败的兜底：允许使用过期缓存，保证每日构建不会因为上游抖动而产出空文件
MAX_CACHE_AGE_HOURS = 72

VALID_RULE_TYPES = {"HOST", "HOST-SUFFIX", "HOST-KEYWORD", "IP-CIDR", "IP6-CIDR"}
DOMAIN_TYPES = {"HOST", "HOST-SUFFIX", "HOST-KEYWORD"}

# 域名与 CIDR 合法性：上游数据偶尔会混入带路径或含非法字符的条目，
# 在解析阶段就丢弃，避免污染产物（QX 遇到非法规则会整段加载失败）。
DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]*[a-z0-9])?)*$")
KEYWORD_RE = re.compile(r"^[a-z0-9.\-_]+$")
CIDR_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}/\d{1,2}$")
CIDR6_RE = re.compile(r"^[0-9a-f:./]+$")


def valid_value(rtype: str, value: str) -> bool:
    """校验规则值是否合法。"""
    if not value or len(value) > 253:
        return False
    if rtype in {"HOST", "HOST-SUFFIX"}:
        return bool(DOMAIN_RE.match(value))
    if rtype == "HOST-KEYWORD":
        return bool(KEYWORD_RE.match(value))
    if rtype == "IP-CIDR":
        return bool(CIDR_RE.match(value))
    if rtype == "IP6-CIDR":
        return bool(CIDR6_RE.match(value))
    return False

# 判定为「开屏 / 启动广告」的高置信度特征
SPLASH_PATTERNS = re.compile(
    r"splash|launch|startup|start_ad|startad|boot_ad|bootad"
    r"|开屏|启动广告|splash_screen|start_advert|getStartAd|startAdvert",
    re.IGNORECASE,
)

# 信息流 / 弹窗 / banner 特征
FEED_PATTERNS = re.compile(
    r"/ad/|/ads/|/advert|/adinfo|/ad_list|/adlist|/adpos|/ad_pos"
    r"|/banner|/popup|/pop_?up|/dialog_?ad|/feed_?ad|/recommend_?ad"
    r"|adRealTime|advertisement|advertising|getAdList|ad_list",
    re.IGNORECASE,
)


def log(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


# ---------------------------------------------------------------------------
# 数据抓取
# ---------------------------------------------------------------------------

def _http_get(url: str, timeout: int = 120) -> str | None:
    """抓取文本，优先 urllib，失败时回落到 curl（部分环境代理只对 curl 生效）。"""
    import urllib.request

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    except Exception:
        pass

    import subprocess

    try:
        out = subprocess.run(
            ["curl", "-sfL", "--max-time", str(timeout), url],
            capture_output=True,
            timeout=timeout + 30,
        )
        if out.returncode == 0 and out.stdout:
            return out.stdout.decode("utf-8", errors="ignore")
    except Exception:
        pass
    return None


def fetch_all(force: bool = True) -> dict[str, Path]:
    """并发抓取所有上游源，返回 名称 -> 本地缓存路径。"""
    paths: dict[str, Path] = {}

    def work(item: tuple[str, str]) -> tuple[str, bool, int]:
        name, url = item
        dest = CACHE / f"{name}.txt"
        if not force and dest.exists():
            return name, True, dest.stat().st_size
        text = _http_get(url)
        if text is None:
            return name, False, 0
        if len(text) < 200:  # 明显异常的空响应
            return name, False, len(text)
        dest.write_text(text, encoding="utf-8")
        return name, True, len(text)

    with futures.ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(work, SOURCES.items()))

    for name, ok, size in results:
        dest = CACHE / f"{name}.txt"
        if ok:
            log(f"  ✓ {name:20s} {size:>10,d} bytes")
            paths[name] = dest
        elif dest.exists():
            age_h = (time.time() - dest.stat().st_mtime) / 3600
            if age_h <= MAX_CACHE_AGE_HOURS:
                log(f"  ! {name:20s} 抓取失败，回退缓存（{age_h:.1f}h 前）")
                paths[name] = dest
            else:
                log(f"  ✗ {name:20s} 抓取失败且缓存过期")
        else:
            log(f"  ✗ {name:20s} 抓取失败，无缓存")
    return paths


# ---------------------------------------------------------------------------
# 规则解析
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Rule:
    """一条归一化后的分流规则。"""

    rtype: str          # HOST / HOST-SUFFIX / HOST-KEYWORD / IP-CIDR / IP6-CIDR
    value: str
    weight: int = 0     # 数值越大越优先保留（用于跨源冲突仲裁）

    @property
    def key(self) -> tuple[str, str]:
        return (self.rtype, self.value)


def parse_qx_filter(text: str, weight: int = 0) -> list[Rule]:
    """
    解析 Quantumult X 分流规则文本。
    支持形如 `HOST-SUFFIX,example.com,REJECT` 的三段式，策略位会被忽略（由构建器统一注入）。
    """
    rules: list[Rule] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        rtype = parts[0].upper()
        value = parts[1].lower()
        if rtype not in VALID_RULE_TYPES or not valid_value(rtype, value):
            continue
        rules.append(Rule(rtype, value, weight))
    return rules


def parse_antiad(text: str, weight: int = 0) -> list[Rule]:
    """
    解析 anti-AD 的 QX 格式（host-suffix,domain,reject）。
    与标准 QX 格式一致，单独实现是为了兼容其未来可能的格式变化。
    """
    return parse_qx_filter(text, weight)


def parse_curated(text: str, weight: int = 0) -> list[Rule]:
    """
    解析本地精选清单。格式与 QX 一致但**不带策略位**（两段式），
    便于人工维护时少写一段。
    """
    rules: list[Rule] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            parts.append("")
        rtype, value = parts[0].upper(), parts[1].lower()
        if rtype not in VALID_RULE_TYPES or not valid_value(rtype, value):
            continue
        rules.append(Rule(rtype, value, weight))
    return rules


# ---------------------------------------------------------------------------
# 白名单
# ---------------------------------------------------------------------------

def load_whitelist() -> list[str]:
    """
    载入白名单。仅保留"纯域名"条目参与匹配；
    带路径的条目（如 taobao.com/h5）只作文档说明用，不参与拦截判定，
    在载入阶段就剔除，避免运行时反复判断。
    """
    path = DATA / "whitelist.txt"
    if not path.exists():
        return []
    items = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip().lower()
        if not line or line.startswith("#") or "/" in line:
            continue
        items.append(line)
    return items


def is_whitelisted(rule: Rule, whitelist: list[str]) -> bool:
    """后缀匹配白名单：命中即丢弃该规则。"""
    if rule.rtype not in DOMAIN_TYPES:
        return False
    value = rule.value
    for w in whitelist:
        if value == w or value.endswith("." + w):
            return True
    return False


# ---------------------------------------------------------------------------
# 去重与压缩
# ---------------------------------------------------------------------------

def _covered_by_suffix(domain: str, suffix_set: set[str]) -> bool:
    """
    判断 domain 是否已被后缀集合中的某条规则覆盖。

    朴素做法是拿 domain 去和每个后缀做 endswith 比较，代价 O(n)；
    规则量到 40 万级时会退化成几十亿次比较。

    改为反向思路：把 domain 按标签逐级剥离（a.b.c.com → b.c.com → c.com → com），
    只要任意一级已存在于后缀集合中，就说明它被覆盖了。域名标签数通常不超过 5，
    于是复杂度降为 O(标签数)，与规则总量无关。
    """
    labels = domain.split(".")
    for i in range(1, len(labels)):
        if ".".join(labels[i:]) in suffix_set:
            return True
    return False


def compress(rules: Iterable[Rule]) -> list[Rule]:
    """
    规则压缩，分三步：
      1. 精确去重（同 type 同 value 只留一条，保留 weight 最大的）
      2. 后缀覆盖：若存在 HOST-SUFFIX,example.com，则删除其所有子域的 HOST/SUFFIX 规则
      3. 关键词覆盖：HOST-KEYWORD 能匹配的 HOST/HOST-SUFFIX 规则予以删除
    """
    best: dict[tuple[str, str], Rule] = {}
    for r in rules:
        cur = best.get(r.key)
        if cur is None or r.weight > cur.weight:
            best[r.key] = r
    unique = list(best.values())

    suffixes = {r.value for r in unique if r.rtype == "HOST-SUFFIX"}
    keywords = {k for k in (r.value for r in unique if r.rtype == "HOST-KEYWORD") if len(k) >= 4}

    # 2. 剔除被更短后缀覆盖的后缀规则（保留最短、覆盖面最广的那条）
    kept_suffixes = {s for s in suffixes if not _covered_by_suffix(s, suffixes)}

    result: list[Rule] = []
    for r in unique:
        if r.rtype in {"IP-CIDR", "IP6-CIDR"}:
            result.append(r)
        elif r.rtype == "HOST-SUFFIX":
            if r.value in kept_suffixes:
                result.append(r)
        elif r.rtype == "HOST":
            if _covered_by_suffix(r.value, kept_suffixes):
                continue
            if any(k in r.value for k in keywords):
                continue
            result.append(r)
        elif r.rtype == "HOST-KEYWORD":
            result.append(r)
    return result


def sort_rules(rules: list[Rule]) -> list[Rule]:
    """按类型分组排序，让 QX 加载时命中路径更清晰，也便于人工审阅。"""
    order = {"HOST-SUFFIX": 0, "HOST": 1, "HOST-KEYWORD": 2, "IP-CIDR": 3, "IP6-CIDR": 4}
    return sorted(rules, key=lambda r: (order.get(r.rtype, 9), r.value))


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

def write_filter(path: Path, name: str, rules: list[Rule], policy: str,
                 extra_desc: str | None = None) -> int:
    """写出 QX 分流规则文件，带完整元信息头。"""
    counts = Counter(r.rtype for r in rules)
    total = len(rules)
    lines = [
        f"# NAME: {name}",
        f"# AUTHOR: QuantumultX-CN",
        f"# REPO: https://github.com/hwind2021/QuantumultX-AdBlock-CN",
        f"# UPDATED: {BUILD_TIME} (UTC+8)",
        f"# POLICY: {policy}",
    ]
    for t in ("HOST", "HOST-SUFFIX", "HOST-KEYWORD", "IP-CIDR", "IP6-CIDR"):
        if counts.get(t):
            lines.append(f"# {t}: {counts[t]}")
    lines.append(f"# TOTAL: {total}")
    if extra_desc:
        lines.append(f"# DESC: {extra_desc}")
    lines.append("")
    for r in rules:
        lines.append(f"{r.rtype},{r.value},{policy}")
    lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return total


def write_rewrite(path: Path, name: str, blocks: list[tuple[str, list[str]]],
                  extra_desc: str | None = None) -> int:
    """写出 QX 重写规则文件。blocks 为 [(分节标题, 规则行列表)]。"""
    total = sum(len(b[1]) for b in blocks)
    lines = [
        f"# NAME: {name}",
        f"# AUTHOR: QuantumultX-CN",
        f"# REPO: https://github.com/hwind2021/QuantumultX-AdBlock-CN",
        f"# UPDATED: {BUILD_TIME} (UTC+8)",
        f"# TOTAL: {total}",
    ]
    if extra_desc:
        lines.append(f"# DESC: {extra_desc}")
    lines.append("")
    for title, body in blocks:
        lines.append(f"# {'=' * 66}")
        lines.append(f"# {title}")
        lines.append(f"# {'=' * 66}")
        lines.extend(body)
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return total


# ---------------------------------------------------------------------------
# 重写规则处理
# ---------------------------------------------------------------------------

URL_RE = re.compile(r"^(?:\(\?i\))?\^?https?:[\\/]{2}")
HOST_IN_REGEX = re.compile(r"https?:[\\/]{2}([^\\/\s]+)")


def extract_hosts_from_rewrite(lines: list[str]) -> set[str]:
    """从重写规则的正则中提取主机名，用于生成 MITM 清单。"""
    hosts: set[str] = set()
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = HOST_IN_REGEX.search(s)
        if not m:
            continue
        host = m.group(1)
        # 去掉正则语法残留：转义符、分组括号、 alternation 竖线
        host = host.replace("\\", "").replace("(", "").replace(")", "").replace("|", " ")
        for candidate in host.split():
            candidate = candidate.strip().lower()
            candidate = re.sub(r"^[{\[]|\$", "", candidate)
            if candidate.count(".") >= 1 and re.fullmatch(r"[a-z0-9.*\-_]+", candidate):
                if candidate.startswith("*."):
                    candidate = candidate[2:]
                if candidate and not candidate.startswith("."):
                    hosts.add(candidate)
    return hosts


def load_rewrite_conf(text: str) -> list[str]:
    """提取重写规则行（丢弃注释与空行）。"""
    out = []
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def build(tiers: set[str], do_fetch: bool) -> None:
    log("==> 抓取上游规则源")
    if do_fetch:
        paths = fetch_all(force=True)
    else:
        paths = {n: CACHE / f"{n}.txt" for n in SOURCES
                 if (CACHE / f"{n}.txt").exists()}
        log(f"  使用本地缓存，共 {len(paths)} 个源")

    def read(name: str) -> str:
        p = paths.get(name)
        if not p or not p.exists():
            return ""
        return p.read_text(encoding="utf-8", errors="ignore")

    log("==> 解析与归一化")
    whitelist = load_whitelist()
    log(f"  白名单条目：{len(whitelist)}")

    # --- 权重设定：人工精选 > 双源共识 > 单源 ---
    W_CURATED = 100     # 本地精选（开屏 SDK）
    W_ANTIAD = 50       # anti-AD
    W_BM7 = 40          # blackmatrix7

    curated_splash = parse_curated(
        (DATA / "splash_sdk.conf").read_text(encoding="utf-8"), W_CURATED
    )
    bm7_lite = parse_qx_filter(read("bm7_ad_lite"), W_BM7)
    bm7_full = parse_qx_filter(read("bm7_ad_full"), W_BM7)
    antiad = parse_antiad(read("antiad_quanx"), W_ANTIAD)
    httpdns = parse_qx_filter(read("bm7_httpdns"), W_BM7)
    privacy = parse_qx_filter(read("bm7_privacy"), W_BM7)
    hijack = parse_qx_filter(read("bm7_hijack"), W_BM7)

    log(f"  精选开屏 SDK : {len(curated_splash):>8,d}")
    log(f"  BM7 精简广告 : {len(bm7_lite):>8,d}")
    log(f"  BM7 完整广告 : {len(bm7_full):>8,d}")
    log(f"  anti-AD      : {len(antiad):>8,d}")
    log(f"  HTTPDNS 拦截 : {len(httpdns):>8,d}")

    # --- 白名单过滤 ---
    def clean(rules: list[Rule]) -> list[Rule]:
        return [r for r in rules if not is_whitelisted(r, whitelist)]

    curated_splash = clean(curated_splash)
    bm7_lite, bm7_full, antiad = clean(bm7_lite), clean(bm7_full), clean(antiad)
    httpdns, privacy, hijack = clean(httpdns), clean(privacy), clean(hijack)
    log("  白名单过滤完成")

    # --- 索引集合，便于做交集/差集 ---
    def dset(rules):
        return {r.value for r in rules if r.rtype in DOMAIN_TYPES}

    antiad_set = dset(antiad)
    bm7_set = dset(bm7_lite)
    consensus = antiad_set & bm7_set   # 双源共识，置信度最高
    log(f"  双源共识域名 : {len(consensus):>8,d}")

    # ======================================================================
    # 产物 1：开屏广告专项
    # ======================================================================
    if "splash" in tiers:
        log("==> 构建 [开屏广告专项]")
        splash_rules: list[Rule] = list(curated_splash)

        # 从上游完整广告库中，补充与开屏 SDK 相关的域名
        sdk_markers = (
            "pangolin", "pangle", "snssdk", "csjplatform", "toutiao",
            "gdt", "ugdtimg", "e.qq.com", "adsdk",
            "mobads", "cpro.baidu", "union.baidu", "mssp.baidu",
            "gifshow", "kuaishou",
            "tanx", "alimama", "adash",
            "googleadservices", "doubleclick", "googlesyndication", "admob",
            "applovin", "unityads", "ironsrc", "impressiondesk",
            "vungle", "mintegral", "mobvista", "inmobi",
            "tapjoy", "chartboost", "adcolony", "smaato",
            "pubmatic", "openx", "mopub", "amazon-adsystem",
            "adservice", "adsdk", "adapi",
        )
        for src in (bm7_full, antiad):
            for r in src:
                if r.rtype not in DOMAIN_TYPES:
                    continue
                if any(m in r.value for m in sdk_markers):
                    splash_rules.append(Rule(r.rtype, r.value, W_BM7))

        splash_rules = sort_rules(compress(splash_rules))
        n = write_filter(
            OUT_FILTER / "Splash-Killer.list", "Splash-Killer",
            splash_rules, "REJECT",
            "开屏广告专项：覆盖穿山甲/广点通/百青藤/快手/阿里妈妈/AdMob/AppLovin 等主流 SDK",
        )
        log(f"  → Splash-Killer.list ({n:,d} 条)")

    # ======================================================================
    # 产物 2：精简版（日常推荐）
    # ======================================================================
    if "lite" in tiers:
        log("==> 构建 [精简版]")
        lite_rules: list[Rule] = list(curated_splash)
        for src in (bm7_lite, antiad):
            for r in src:
                if r.rtype not in DOMAIN_TYPES:
                    continue
                # 保留双源共识 + 开屏 SDK 相关，其余按置信度筛
                if r.value in consensus:
                    lite_rules.append(r)
        # 补充 IP-CIDR（广告服务器 IP 段，量小价值高）
        for src in (bm7_lite,):
            for r in src:
                if r.rtype in {"IP-CIDR", "IP6-CIDR"}:
                    lite_rules.append(r)
        # 合并 HTTPDNS
        lite_rules.extend(httpdns)

        lite_rules = sort_rules(compress(lite_rules))
        n = write_filter(
            OUT_FILTER / "AdBlock-Lite.list", "AdBlock-Lite",
            lite_rules, "REJECT",
            "日常推荐：开屏专项 + 双源共识广告域名 + HTTPDNS 拦截，体量与效果平衡",
        )
        log(f"  → AdBlock-Lite.list ({n:,d} 条)")

    # ======================================================================
    # 产物 3：完整版
    # ======================================================================
    if "full" in tiers:
        log("==> 构建 [完整版]")
        full_rules: list[Rule] = list(curated_splash)
        full_rules.extend(bm7_full)
        full_rules.extend(antiad)
        full_rules.extend(httpdns)

        full_rules = sort_rules(compress(full_rules))
        n = write_filter(
            OUT_FILTER / "AdBlock-Full.list", "AdBlock-Full",
            full_rules, "REJECT",
            "完整版：全量合并去重，拦截率最高但占用内存较多，建议高性能设备使用",
        )
        log(f"  → AdBlock-Full.list ({n:,d} 条)")

    # ======================================================================
    # 产物 4：HTTPDNS 拦截（独立，必装）
    # ======================================================================
    if "splash" in tiers:
        n = write_filter(
            OUT_FILTER / "BlockHttpDNS.list", "BlockHttpDNS",
            sort_rules(compress(httpdns)), "REJECT",
            "阻断 HTTPDNS：防止 App 绕过本地 DNS 解析广告域名，是开屏广告拦截生效的前提",
        )
        log(f"  → BlockHttpDNS.list ({n:,d} 条)")

    # ======================================================================
    # 产物 5/6：可选模块
    # ======================================================================
    if "optional" in tiers:
        n = write_filter(
            OUT_FILTER / "AdBlock-Privacy.list", "AdBlock-Privacy",
            sort_rules(compress(privacy)), "REJECT",
            "可选：隐私追踪与数据统计上报拦截（友盟/TalkingData/神策等），可能影响崩溃日志上报",
        )
        log(f"  → AdBlock-Privacy.list ({n:,d} 条)")

        n = write_filter(
            OUT_FILTER / "AdBlock-AntiHijack.list", "AdBlock-AntiHijack",
            sort_rules(compress(hijack)), "REJECT",
            "可选：HTTP 劫持与运营商插播广告防护",
        )
        log(f"  → AdBlock-AntiHijack.list ({n:,d} 条)")

    # ======================================================================
    # 重写规则
    # ======================================================================
    if "rewrite" in tiers:
        log("==> 构建 [重写规则]")
        up_rewrite = load_rewrite_conf(read("bm7_rewrite_ad"))
        up_script = load_rewrite_conf(read("bm7_rewrite_script"))

        # 拆分：开屏 / 信息流
        splash_rw = [l for l in up_rewrite if SPLASH_PATTERNS.search(l)]
        feed_rw = [l for l in up_rewrite
                   if l not in splash_rw and FEED_PATTERNS.search(l)]

        local_splash = load_rewrite_conf(LOCAL_SPLASH_REWRITE)
        local_feed = load_rewrite_conf(LOCAL_FEED_REWRITE)

        # 本地规则置顶（人工维护，优先级最高），上游按去重追加
        def merge(local: list[str], upstream: list[str]) -> list[str]:
            seen = set(local)
            out = list(local)
            for l in upstream:
                if l not in seen:
                    seen.add(l)
                    out.append(l)
            return out

        splash_all = merge(local_splash, splash_rw)
        feed_all = merge(local_feed, feed_rw)

        n1 = write_rewrite(
            OUT_REWRITE / "AdBlock-Splash.conf", "AdBlock-Splash",
            [("开屏广告拦截（MITM · 精准匹配）", splash_all)],
            "需开启 MITM；返回空响应让 App 立即跳过开屏，而非等待超时",
        )
        log(f"  → AdBlock-Splash.conf ({n1:,d} 条)")

        n2 = write_rewrite(
            OUT_REWRITE / "AdBlock-Feed.conf", "AdBlock-Feed",
            [("信息流 / Banner / 弹窗广告拦截（MITM）", feed_all)],
            "需开启 MITM；清理首页 banner、信息流插入广告与弹窗",
        )
        log(f"  → AdBlock-Feed.conf ({n2:,d} 条)")

        n3 = write_rewrite(
            OUT_REWRITE / "AdBlock-Script.conf", "AdBlock-Script",
            [("脚本型去广告（MITM · 需先安装 JS 脚本）", up_script)],
            "依赖 JS 脚本处理复杂响应结构，使用前请先安装 script/ 目录下的脚本",
        )
        log(f"  → AdBlock-Script.conf ({n3:,d} 条)")

        n4 = write_rewrite(
            OUT_REWRITE / "AdBlock-All.conf", "AdBlock-All",
            [
                ("开屏广告拦截", splash_all),
                ("信息流 / Banner / 弹窗广告拦截", feed_all),
                ("脚本型去广告", up_script),
            ],
            "全量重写规则合集",
        )
        log(f"  → AdBlock-All.conf ({n4:,d} 条)")

        # MITM 清单
        all_rw_lines = splash_all + feed_all + up_script
        hosts = extract_hosts_from_rewrite(all_rw_lines)
        # MITM_EXTRA 是多行文本，必须按行切分（按字符迭代会混入单个符号）
        hosts |= {h.strip().lower() for h in MITM_EXTRA.split() if h.strip()}
        # 合并开屏专项分流规则里的域名：MITM 未覆盖时，rewrite 对这些域名无效
        hosts |= {r.value for r in curated_splash
                  if r.rtype in {"HOST", "HOST-SUFFIX"}
                  and not r.value.startswith("*")
                  and re.fullmatch(r"[a-z0-9.\-]+", r.value)}
        mitm_lines = [
            "# NAME: MITM",
            "# AUTHOR: QuantumultX-CN",
            f"# UPDATED: {BUILD_TIME} (UTC+8)",
            f"# TOTAL: {len(hosts)}",
            "# DESC: 重写规则依赖的主机名清单，填入 Quantumult X 的 [mitm] 段 hostname 后",
            "",
        ]
        mitm_lines.extend(sorted(hosts))
        mitm_lines.append("")
        OUT_MITM.mkdir(parents=True, exist_ok=True)
        (OUT_MITM / "MITM.list").write_text("\n".join(mitm_lines), encoding="utf-8")
        log(f"  → MITM.list ({len(hosts):,d} 个主机名)")

    log("==> 构建完成")


# ---------------------------------------------------------------------------
# 本地补充的重写规则（人工维护，优先级高于上游）
# ---------------------------------------------------------------------------

LOCAL_SPLASH_REWRITE = r"""
# ---- 穿山甲 / Pangle ----
^https?:\/\/(api-access|api5-access|tobid|isub|pangolin)\.pangolin-sdk-toutiao\.com\/ url reject-dict
^https?:\/\/(isub|pangolin|ad)\.snssdk\.com\/ url reject-dict
^https?:\/\/[\w-]+\.pangolin-sdk-toutiao\.com\/api\/ad\/ url reject-dict

# ---- 腾讯广点通 / 优量汇 ----
^https?:\/\/sdk\.e\.qq\.com\/ url reject-dict
^https?:\/\/(mi|wb|iad)\.gdt\.qq\.com\/ url reject-dict
^https?:\/\/[\w-]+\.ugdtimg\.com\/ url reject-img
^https?:\/\/[\w-]+\.gdtimg\.com\/ url reject-img

# ---- 百度百青藤 ----
^https?:\/\/(mobads|mssp)\.baidu\.com\/ url reject-dict
^https?:\/\/(cpro|cpro2|pos|cbjs)\.baidu\.com\/ url reject-dict
^https?:\/\/union\.baidu\.com\/ url reject-dict

# ---- 快手磁力引擎 ----
^https?:\/\/(ad|cm\.ad)\.partner?\.?gifshow\.com\/ url reject-dict
^https?:\/\/ad\.partner\.gifshow\.com\/ url reject-dict
^https?:\/\/(adse|api-ad)\.kuaishou\.com\/ url reject-dict

# ---- 阿里妈妈 / Tanx ----
^https?:\/\/p\.tanx\.com\/ url reject-dict
^https?:\/\/[\w-]*adashx?[\w-]*\.[\w.]*taobao\.com\/ url reject-dict

# ---- Google AdMob ----
^https?:\/\/[\w-]+\.googleadservices\.com\/ url reject-dict
^https?:\/\/(pagead2|googleads)\.googlesyndication\.com\/ url reject-dict
^https?:\/\/[\w-]+\.doubleclick\.net\/ url reject-dict

# ---- 海外聚合平台 ----
^https?:\/\/[\w-]+\.applovin\.com\/ url reject-dict
^https?:\/\/[\w-]+\.unityads\.unity3d\.com\/ url reject-dict
^https?:\/\/[\w-]+\.ironsrc\.com\/ url reject-dict
^https?:\/\/[\w-]+\.vungle\.com\/ url reject-dict
^https?:\/\/[\w-]+\.mintegral\.com\/ url reject-dict
^https?:\/\/[\w-]+\.inmobi\.com\/ url reject-dict
^https?:\/\/[\w-]+\.tapjoy\.com\/ url reject-dict
^https?:\/\/[\w-]+\.chartboost\.com\/ url reject-dict
^https?:\/\/[\w-]+\.adcolony\.com\/ url reject-dict
^https?:\/\/an\.facebook\.com\/ url reject-dict

# ---- 通用开屏广告路径特征 ----
(?i)\/splash\/(get|list|listAll|config|ad) url reject-dict
(?i)\/(splash|launch|startup)[_-]?(ad|ads|advert|advertise|screen|page|list) url reject-dict
(?i)\/api\/[\w\/]*start(up)?[_-]?ad url reject-dict
(?i)\/api\/[\w\/]*boot[_-]?ad url reject-dict
(?i)\/[\w\/]*open[_-]?screen[_-]?ad url reject-dict
(?i)advert[\w\/]*\/(splash|launch|startup) url reject-dict
(?i)\/ad\/[\w\/]*(splash|launch|startup|open) url reject-dict
"""

LOCAL_FEED_REWRITE = r"""
# ---- 通用信息流 / Banner / 弹窗 ----
(?i)\/api\/[\w\/]*feed[\w\/]*ad url reject-array
(?i)\/api\/[\w\/]*ad[\w\/]*feed url reject-array
(?i)\/api\/[\w\/]*banner[\w\/]*(list|info) url reject-array
(?i)\/api\/[\w\/]*popup url reject-dict
(?i)\/api\/[\w\/]*pop[\w-]*up[\w\/]*ad url reject-dict
(?i)\/api\/[\w\/]*advert(ise|ising)?[\w\/]*position url reject-array
(?i)\/api\/[\w\/]*ad[\w\/]*position url reject-array
(?i)\/api\/[\w\/]*getAd(vert)?List url reject-array
(?i)\/api\/[\w\/]*ad[\w\/]*config url reject-dict
(?i)\/api\/[\w\/]*operation[\w\/]*ad url reject-dict
(?i)\/api\/[\w\/]*(home|index)[\w\/]*ad[\w\/]* url reject-dict
"""

MITM_EXTRA = """
pangolin-sdk-toutiao.com
pangolin.pangle.cn
pangle.cn
pangle.io
isub.snssdk.com
pangolin.snssdk.com
ad.snssdk.com
sdk.e.qq.com
mi.gdt.qq.com
wb.gdt.qq.com
iad.gdt.qq.com
gdt.qq.com
ugdtimg.com
gdtimg.com
mobads.baidu.com
mssp.baidu.com
cpro.baidu.com
cpro2.baidu.com
pos.baidu.com
cbjs.baidu.com
union.baidu.com
ad.partner.gifshow.com
cm.ad.gifshow.com
adse.kuaishou.com
api-ad.kuaishou.com
p.tanx.com
tanx.com
alimama.com
googleadservices.com
googlesyndication.com
doubleclick.net
admob.com
applovin.com
unityads.unity3d.com
ironsrc.com
impressiondesk.com
vungle.com
mintegral.com
mobvista.com
inmobi.com
tapjoy.com
chartboost.com
adcolony.com
an.facebook.com
smaato.net
pubmatic.com
openx.net
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Quantumult X 去广告规则构建器")
    parser.add_argument("--no-fetch", action="store_true",
                        help="跳过抓取，仅用本地缓存重新构建")
    parser.add_argument("--tier", default="all",
                        help="逗号分隔：splash,lite,full,rewrite,optional 或 all")
    args = parser.parse_args()

    if args.tier == "all":
        tiers = {"splash", "lite", "full", "rewrite", "optional"}
    else:
        tiers = {t.strip() for t in args.tier.split(",") if t.strip()}

    build(tiers, do_fetch=not args.no_fetch)
    return 0


if __name__ == "__main__":
    sys.exit(main())
