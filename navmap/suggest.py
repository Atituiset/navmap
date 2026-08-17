"""半自动名单扩充（设计文档 §5.3-1 / §5.5-1）：发现候选，人工确认后入配置。

- suggest_register_apis：找"某参数为函数指针类型、且调用点传入的不同函数数
  ≥ 阈值"的函数——注册式分发 API 的信号。文本粗筛注册味命名（Reg/Register/
  Subscribe/Attach/Bind）的调用 → 解析命中文件 → CallExpr 按 referenced
  函数声明的参数类型与实参函数指针归集。
- suggest_global_vars：从 `extern <type> <name>;` 声明收集全局变量宇宙，
  文本统计全仓引用次数，取 Top N。

产出都是建议清单（人审后拷入 config/navmap.toml），不自动改配置。
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .compdb import CompilationDB
from .extract.base import TUExtractor

_DEFAULT_EXTS = (".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".hh")
_EXCLUDE_DIRS = (".git", "node_modules", ".venv", "vendor", "build")

# 注册味命名的调用点粗筛（误报允许，AST 会过滤）
_REGISTERISH_RE = re.compile(
    r"\b\w*(?:[Rr]eg(?:ister)?|[Ss]ubscribe|[Aa]ttach|[Bb]ind)\w*\s*\(")
# extern 全局变量声明（排除函数声明）
_EXTERN_RE = re.compile(
    r"^\s*extern\s+(?!.*\()[\w\s\*]+?(\w+)\s*(?:\[[^\]]*\])?\s*;", re.MULTILINE)


def _walk_sources(src: Path, exts: tuple[str, ...]):
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_DIRS]
        for fn in filenames:
            if fn.endswith(exts):
                yield Path(dirpath) / fn


# ---------------------------------------------------------------- 注册 API 发现


@dataclass
class ApiSuggestion:
    api: str
    distinct_handlers: set[str] = field(default_factory=set)
    call_sites: int = 0


class _ApiDiscovery(TUExtractor):
    """单文件：收集"实参是函数指针"的调用点。"""

    NEEDS_FUNCTION_BODIES = True

    def collect(self, path: str) -> list[tuple[str, str]]:
        """返回 [(api_spelling, handler_spelling), ...]。"""
        ci = self._cindex
        tu, _fatal = self._parse(path)
        out: list[tuple[str, str]] = []
        for cur in self._walk_all(tu.cursor):
            if cur.kind != ci.CursorKind.CALL_EXPR:
                continue
            ref = cur.referenced
            if ref is None or ref.kind != ci.CursorKind.FUNCTION_DECL:
                continue
            if not self._has_funcptr_param(ref):
                continue
            for arg in cur.get_arguments():
                fn = self._peel_to_ref(arg)
                if fn is not None and fn.kind == ci.CursorKind.FUNCTION_DECL:
                    out.append((ref.spelling, fn.spelling))
                    break
        return out

    def _walk_all(self, cursor):
        yield cursor
        for child in cursor.get_children():
            yield from self._walk_all(child)

    def _has_funcptr_param(self, func_decl) -> bool:
        ci = self._cindex
        for p in func_decl.get_arguments():
            pt = p.type.get_canonical()
            if pt.kind == ci.TypeKind.POINTER and pt.get_pointee().kind in (
                ci.TypeKind.FUNCTIONPROTO,
                ci.TypeKind.FUNCTIONNOPROTO,
            ):
                return True
        return False


def suggest_register_apis(
    src_root: str | Path,
    compdb_path: str | Path,
    *,
    threshold: int = 5,
    extensions: list[str] | None = None,
    extra_args: list[str] | None = None,
    known_apis: list[str] | None = None,
) -> tuple[list[ApiSuggestion], list[str]]:
    """注册 API 候选：不同 handler 数 ≥ threshold 的函数指针参数函数。

    返回 (建议清单按 handler 数降序, 解析失败文件)。known_apis 中已有的
    API 也会列出（供复核），由调用方标注。
    """
    src = Path(src_root).resolve()
    exts = tuple(extensions or _DEFAULT_EXTS)
    files = [str(p) for p in _walk_sources(src, exts)
             if _REGISTERISH_RE.search(p.read_text(errors="replace"))]

    disc = _ApiDiscovery(CompilationDB(compdb_path), src_root=src,
                         extra_args=extra_args)
    apis: dict[str, ApiSuggestion] = {}
    failures: list[str] = []
    for f in files:
        try:
            pairs = disc.collect(f)
        except Exception as e:  # 单文件失败不整批崩
            failures.append(f"{f}: {e}")
            continue
        for api, handler in pairs:
            s = apis.setdefault(api, ApiSuggestion(api))
            s.distinct_handlers.add(handler)
            s.call_sites += 1

    ranked = sorted(apis.values(),
                    key=lambda s: (-len(s.distinct_handlers), s.api))
    return [s for s in ranked
            if len(s.distinct_handlers) >= threshold
            or s.api in (known_apis or [])], failures


# ---------------------------------------------------------------- 全局变量 Top-N


@dataclass
class VarSuggestion:
    name: str
    refs: int
    decl_file: str


def suggest_global_vars(
    src_root: str | Path,
    *,
    top: int = 20,
    extensions: list[str] | None = None,
) -> list[VarSuggestion]:
    """extern 声明的全局变量，按全仓文本引用次数取 Top N（§5.5-1 自动候选）。"""
    src = Path(src_root).resolve()
    exts = tuple(extensions or _DEFAULT_EXTS)
    texts: dict[str, str] = {}
    decls: dict[str, str] = {}  # name -> decl 文件（相对路径）
    for p in _walk_sources(src, exts):
        rel = os.path.relpath(p, src)
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        texts[rel] = text
        for m in _EXTERN_RE.finditer(text):
            decls.setdefault(m.group(1), rel)

    counts: dict[str, int] = defaultdict(int)
    if decls:
        # 合并大正则一次扫描（逐变量扫全仓是 O(文件×变量)，万级文件下不可行）
        big = re.compile(
            r"\b(" + "|".join(re.escape(n) for n in decls) + r")\b")
        for rel, text in texts.items():
            for name in big.findall(text):
                counts[name] += 1
        for name, rel in decls.items():
            counts[name] -= 1  # 排除 extern 声明行本身

    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top]
    return [VarSuggestion(name=n, refs=c, decl_file=decls[n]) for n, c in ranked]
