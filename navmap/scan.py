"""第 1 步：候选粗筛（设计文档 §5.1，正则 MVP 版）。

只负责找候选文件，允许误报（第 2 步 AST 会过滤），漏报才要紧——
词根名单先宽后收，在 config/navmap.toml 配置。
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# 函数指针成员/typedef：`void (*handler)(...)`、`int (*cb)()` 等
_FUNC_PTR_RE = re.compile(r"\(\s*\*\s*\w+\s*\)\s*\(")

# 默认排除目录（支持 fnmatch 通配）：版本控制、依赖、构建产物、工具自身、第三方
DEFAULT_EXCLUDE_DIRS: tuple[str, ...] = (
    ".git", "node_modules", ".venv", "vendor", "_deps", "build", "build-*",
    ".navmap-tool", ".navmap", ".navmap-out", ".codegraph",
    "third_party", "third-party", "external", "googletest", "gtest", "gmock",
)


def _match_exclude(dirname: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(dirname, pat) for pat in patterns)


@dataclass
class Candidate:
    file: str
    reasons: list[str] = field(default_factory=list)


def _build_array_re(name_roots: list[str]) -> re.Pattern:
    roots = "|".join(re.escape(r) for r in name_roots)
    # `<type> <name>[] = {` 或 `[N] = {`，name 命中词根（大小写不敏感）
    return re.compile(
        r"\b\w*(?:" + roots + r")\w*\s*(?:\[[^\]]*\])+\s*=\s*\{",
        re.IGNORECASE,
    )


def build_matchers(
    name_roots: list[str],
    register_apis: list[str] | None = None,
) -> tuple[re.Pattern, list[tuple[str, re.Pattern]]]:
    """构造粗筛匹配器，供 scan() 与增量刷新单文件复判共用。"""
    array_re = _build_array_re(name_roots)
    api_res = [
        (api, re.compile(r"\b" + re.escape(api) + r"\s*\("))
        for api in (register_apis or [])
    ]
    return array_re, api_res


def match_file(
    path: str | Path,
    array_re: re.Pattern,
    api_res: list[tuple[str, re.Pattern]],
) -> list[str]:
    """单文件粗筛，返回命中原因（空列表 = 非候选）。"""
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return []
    reasons: list[str] = []
    if _FUNC_PTR_RE.search(text):
        reasons.append("func-ptr-member")
    if array_re.search(text):
        reasons.append("array-init")
    for api, pat in api_res:
        if pat.search(text):
            reasons.append(f"register-api:{api}")
    return reasons


def scan(
    src_root: str | Path,
    name_roots: list[str],
    register_apis: list[str] | None = None,
    extensions: list[str] | None = None,
    exclude_dirs: tuple[str, ...] | None = None,
) -> list[Candidate]:
    """全仓文本粗筛，返回候选文件清单。"""
    src_root = Path(src_root)
    exts = tuple(extensions or [".c", ".h"])
    excludes = tuple(exclude_dirs) if exclude_dirs else DEFAULT_EXCLUDE_DIRS
    array_re, api_res = build_matchers(name_roots, register_apis)

    candidates: dict[str, Candidate] = {}
    for dirpath, dirnames, filenames in os.walk(src_root):
        dirnames[:] = [d for d in dirnames if not _match_exclude(d, excludes)]
        for fn in filenames:
            if not fn.endswith(exts):
                continue
            path = Path(dirpath) / fn
            reasons = match_file(path, array_re, api_res)
            if reasons:
                candidates[str(path)] = Candidate(file=str(path), reasons=reasons)
    return sorted(candidates.values(), key=lambda c: c.file)
