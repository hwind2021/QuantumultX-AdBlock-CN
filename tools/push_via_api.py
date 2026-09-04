#!/usr/bin/env python3
"""
push_via_api.py — 通过 GitHub Git Data API 推送提交，绕过 git/SSH 协议限制。

使用场景
--------
在部分受限网络（公司代理、校园网、某些地区网络）中，`github.com` 与
`ssh.github.com:22` 会被拦截或拒绝 CONNECT，但 `api.github.com` 通常仍可达。
此时 `git push` 会失败（`CONNECT tunnel failed, response 502`），
本脚本改用纯 HTTPS + REST API 完成等价的提交动作。

原理（对应 git 的底层对象模型）
-------------------------------
    git hash-object  ->  POST   /repos/{o}/{r}/git/blobs    (创建 blob)
    git mktree       ->  POST   /repos/{o}/{r}/git/trees    (创建 tree)
    git commit-tree  ->  POST   /repos/{o}/{r}/git/commits  (创建 commit)
    git update-ref   ->  PATCH  /repos/{o}/{r}/git/refs/heads/{branch}

用法
----
    export GITHUB_TOKEN=ghp_xxx
    python tools/push_via_api.py                      # 提交当前工作区所有受版本控制的文件
    python tools/push_via_api.py --branch main        # 指定分支
    python tools/push_via_api.py --dry-run            # 只统计不提交

注意事项
--------
* 单次 blob 上限 100 MB；超过需改用 Git LFS。
* 采用「全量快照」语义：以工作区 `git ls-files` 的结果为准，
  远端已存在但本地已删除的文件会被移除，等价于一次 `git add -A && git commit`。
* 不依赖本地 git 二进制，仅需 `git ls-files` 获取文件清单（可退化到全目录扫描）。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable

API = "https://api.github.com"
CHUNK = 4096


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class GitHub:
    def __init__(self, token: str, owner: str, repo: str):
        self.token = token
        self.owner = owner
        self.repo = repo
        self.timeout = 120

    def _req(self, method: str, path: str, payload: dict | None = None, retry: int = 3):
        url = f"{API}/repos/{self.owner}/{self.repo}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        for attempt in range(1, retry + 1):
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Authorization", f"Bearer {self.token}")
            req.add_header("Accept", "application/vnd.github+json")
            req.add_header("User-Agent", "push-via-api/1.0")
            if data:
                req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "ignore")
                # 5xx 与 429 可重试
                if e.code in (429, 500, 502, 503, 504) and attempt < retry:
                    wait = 2 ** attempt
                    print(f"      ! HTTP {e.code}，{wait}s 后重试 ({attempt}/{retry})")
                    time.sleep(wait)
                    continue
                raise RuntimeError(f"{method} {path} 失败 HTTP {e.code}: {body[:400]}")
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < retry:
                    wait = 2 ** attempt
                    print(f"      ! 网络错误 {e}，{wait}s 后重试 ({attempt}/{retry})")
                    time.sleep(wait)
                    continue
                raise RuntimeError(f"{method} {path} 网络失败: {e}")
        raise RuntimeError("unreachable")

    def create_blob(self, content: bytes) -> str:
        # 大文件用 base64 编码传输，保证二进制安全
        r = self._req("POST", "/git/blobs", {
            "encoding": "base64",
            "content": base64.b64encode(content).decode("ascii"),
        })
        return r["sha"]

    def create_tree(self, entries: list[dict]) -> str:
        r = self._req("POST", "/git/trees", {"tree": entries})
        return r["sha"]

    def create_commit(self, message: str, tree_sha: str, parents: list[str]) -> str:
        r = self._req("POST", "/git/commits", {
            "message": message, "tree": tree_sha, "parents": parents,
        })
        return r["sha"]

    def get_ref(self, branch: str) -> str | None:
        try:
            r = self._req("GET", f"/git/ref/heads/{branch}")
            return r["object"]["sha"]
        except RuntimeError as e:
            if "404" in str(e):
                return None
            raise

    def create_ref(self, branch: str, sha: str) -> None:
        self._req("POST", "/git/refs", {"ref": f"refs/heads/{branch}", "sha": sha})

    def update_ref(self, branch: str, sha: str, force: bool = True) -> None:
        self._req("PATCH", f"/git/refs/heads/{branch}", {"sha": sha, "force": force})


# --------------------------------------------------------------------------- #
# 文件收集
# --------------------------------------------------------------------------- #
SKIP_DIRS = {".git", ".cache", "__pycache__", "node_modules", ".venv", "venv"}


def collect_files(root: Path) -> list[Path]:
    """优先用 git ls-files（尊重 .gitignore），失败则退回目录扫描。"""
    try:
        # core.quotepath=false 必须显式指定：否则中文路径会被转义成
        # "\345\277\253..." 这类八进制序列，导致后续 stat() 失败
        out = subprocess.run(
            ["git", "-c", "core.quotepath=false", "ls-files"],
            cwd=root, capture_output=True, text=True, check=True,
        ).stdout
        files = [root / line for line in out.splitlines() if line.strip()]
        files = [f for f in files if f.exists()]
        if files:
            return sorted(files)
    except Exception:
        pass

    files = []
    for p in root.rglob("*"):
        if p.is_file() and not any(s in p.parts for s in SKIP_DIRS):
            files.append(p)
    return sorted(files)


def guess_mode(p: Path) -> str:
    """判定 git 文件模式；仅可执行文件记为 100755。"""
    return "100755" if os.access(p, os.X_OK) and p.suffix in {".sh", ".py", ".js"} else "100644"


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="通过 GitHub API 推送提交")
    ap.add_argument("--owner", default=os.environ.get("GH_OWNER", ""))
    ap.add_argument("--repo", default=os.environ.get("GH_REPO", ""))
    ap.add_argument("--branch", default="main")
    ap.add_argument("--message", default="")
    ap.add_argument("--root", default=".")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print("✗ 缺少 GITHUB_TOKEN 环境变量", file=sys.stderr)
        return 2
    if not args.owner or not args.repo:
        print("✗ 缺少 --owner/--repo", file=sys.stderr)
        return 2

    root = Path(args.root).resolve()
    files = collect_files(root)
    if not files:
        print("✗ 没有待提交的文件", file=sys.stderr)
        return 1

    total = sum(f.stat().st_size for f in files)
    print(f"仓库: {args.owner}/{args.repo}  分支: {args.branch}")
    print(f"文件: {len(files)} 个，共 {total/1024/1024:.2f} MB")

    if args.dry_run:
        for f in files[:20]:
            print(f"  {f.relative_to(root).as_posix()}")
        if len(files) > 20:
            print(f"  ...（另有 {len(files)-20} 个）")
        return 0

    gh = GitHub(token, args.owner, args.repo)

    # 1. 创建 blob
    print("\n[1/4] 创建 blob ...")
    entries: list[dict] = []
    done_bytes = 0
    for i, f in enumerate(files, 1):
        rel = f.relative_to(root).as_posix()
        data = f.read_bytes()
        sha = gh.create_blob(data)
        entries.append({"path": rel, "mode": guess_mode(f), "type": "blob", "sha": sha})
        done_bytes += len(data)
        pct = done_bytes / total * 100
        size = len(data) / 1024
        unit = "KB" if size < 1024 else "MB"
        val = size if size < 1024 else size / 1024
        print(f"  [{i:>3}/{len(files)}] {pct:5.1f}%  {val:7.2f}{unit:2s}  {rel}")

    # 2. 创建 tree
    print("\n[2/4] 创建 tree ...")
    tree_sha = gh.create_tree(entries)
    print(f"  tree {tree_sha}")

    # 3. 创建 commit（首次提交无 parent）
    parent = gh.get_ref(args.branch)
    parents = [parent] if parent else []
    message = args.message or ("feat: 初始化规则集" if not parent else "chore: 更新规则集")
    print(f"\n[3/4] 创建 commit ...（parent: {parent or '无，首次提交'}）")
    commit_sha = gh.create_commit(message, tree_sha, parents)
    print(f"  commit {commit_sha}")

    # 4. 更新 ref
    print("\n[4/4] 更新 ref ...")
    if parent:
        gh.update_ref(args.branch, commit_sha)
    else:
        gh.create_ref(args.branch, commit_sha)
    print(f"  refs/heads/{args.branch} -> {commit_sha}")

    print(f"\n✓ 完成：https://github.com/{args.owner}/{args.repo}/commit/{commit_sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
