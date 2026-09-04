"""全局变量读写清单提取（设计文档 §5.5）。

流程（MVP 期，无 clangd 索引）：
1. find_ref_files()：文本搜变量名（\\b 词边界）得候选文件——全局变量名在
   电信代码里几乎不撞名，文本粗筛即可（§5.5-2）；
2. extract()：只对候选文件解析 TU（NEEDS_FUNCTION_BODIES=True，引用在函数
   体内），递归遍历全部 cursor 找 DeclRefExpr，要求 referenced 是 TU 作用域
   的 VarDecl（排除局部变量遮蔽），沿父链分类读/写（§5.5-3）：
   - 写：BinaryOperator 赋值左值 / CompoundAssign / UnaryOperator ++、-- /
     UnaryOperator(&) 取地址（保守记写，可能经指针传出修改）；
   - 读：其余；
3. 聚合：写者函数全量列出，读者按模块（路径前缀映射，配置化）聚合计数。

单文件解析失败不整批崩（与 dispatch.py 同契约）。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..compdb import CompilationDB
from ..model import GlobalVar, VarRef
from ..scan import DEFAULT_EXCLUDE_DIRS, match_exclude as _match_exclude
from .base import TUExtractor

#: 文本粗筛默认扩展名
_DEFAULT_EXTS = [".c", ".cc", ".cpp", ".cxx", ".h", ".hpp"]

#: 读/写分类时沿父链向上穿过的透传表达式
_PASS_THROUGH = (
    "ARRAY_SUBSCRIPT_EXPR",
    "MEMBER_REF",
    "MEMBER_REF_EXPR",
    "PAREN_EXPR",
    "UNEXPOSED_EXPR",
)


class GlobalvarExtractor(TUExtractor):
    # 全局变量引用在函数体内
    NEEDS_FUNCTION_BODIES = True

    def __init__(
        self,
        compdb: CompilationDB,
        src_root: str | Path,
        *,
        variables: list[str],
        modules: dict[str, list[str]] | None = None,
        extensions: list[str] | None = None,
        extra_args: list[str] | None = None,
    ):
        super().__init__(compdb, src_root, extra_args=extra_args)
        self.variables = list(variables)
        self._var_set = set(self.variables)
        self.modules = modules
        self._extensions = extensions or _DEFAULT_EXTS
        self._patterns = {
            v: re.compile(r"\b" + re.escape(v) + r"\b") for v in self.variables
        }
        self._pass_through = {getattr(self._cindex.CursorKind, k)
                              for k in _PASS_THROUGH}

    # ---------------- 文本粗筛（§5.5-2） ----------------

    def find_ref_files(self) -> dict[str, list[str]]:
        """os.walk 源码树做文本粗筛 → {变量: [仓库相对路径...]}（各列表排序）。"""
        hits: dict[str, set[str]] = {v: set() for v in self.variables}
        exts = tuple(self._extensions)
        for dirpath, dirnames, filenames in os.walk(self.src_root):
            dirnames[:] = [d for d in dirnames
                           if not _match_exclude(d, DEFAULT_EXCLUDE_DIRS)]
            for fn in filenames:
                if not fn.endswith(exts):
                    continue
                fpath = os.path.join(dirpath, fn)
                try:
                    text = self._read(fpath).decode(errors="replace")
                except OSError:
                    continue
                for var, pat in self._patterns.items():
                    if pat.search(text):
                        hits[var].add(self._relpath(fpath))
        return {v: sorted(files) for v, files in hits.items()}

    # ---------------- AST 提取（§5.5-3/4） ----------------

    def extract(self) -> tuple[list[GlobalVar], list[str]]:
        ref_files = self.find_ref_files()
        # 全部变量命中文件的并集（绝对路径），每文件只解析一次
        all_files: set[str] = set()
        for files in ref_files.values():
            for rel in files:
                all_files.add(os.path.join(str(self.src_root), rel))

        refs: dict[str, list[VarRef]] = {v: [] for v in self.variables}
        def_locs: dict[str, str] = {}
        failures: list[str] = []
        for f in sorted(all_files):
            try:
                self._extract_file(f, refs, def_locs)
            except Exception as e:  # 单文件失败不能整批崩
                failures.append(f"{self._relpath(f)}: {e}")

        out: list[GlobalVar] = []
        for var in self.variables:
            # 同一头文件被多个 TU 解析会重复遍历，按 (func, loc, kind) 去重
            seen: set[tuple[str, str, str]] = set()
            uniq: list[VarRef] = []
            for r in refs[var]:
                key = (r.func, r.loc, r.kind)
                if key not in seen:
                    seen.add(key)
                    uniq.append(r)
            writers = [r for r in uniq if r.kind != "read"]
            readers: dict[str, int] = {}
            for r in uniq:
                if r.kind == "read":
                    mod = self._module_of(r.loc.rsplit(":", 1)[0])
                    readers[mod] = readers.get(mod, 0) + 1
            ref_rel = sorted({r.loc.rsplit(":", 1)[0] for r in uniq})
            out.append(GlobalVar(
                variable=var,
                def_loc=def_locs.get(var),
                writers=writers,
                readers_by_module=readers,
                total_refs=len(uniq),
                ref_files=ref_rel,
            ))
        return out, failures

    def _extract_file(
        self,
        path: str,
        refs: dict[str, list[VarRef]],
        def_locs: dict[str, str],
    ) -> None:
        tu, fatal = self._parse(path)
        if fatal:
            raise RuntimeError("fatal diagnostics")
        self._walk(tu.cursor, (), "<global>", refs, def_locs)

    def _walk(self, cursor, ancestors, func, refs, def_locs) -> None:
        """递归遍历整棵 AST（含函数体），自顶向下携带祖先链与所在函数名。

        libclang 20 中表达式 cursor 的 semantic_parent/lexical_parent 返回
        空（声明类正常），读/写分类所需的父链只能遍历时自己带下来。
        """
        ci = self._cindex
        if cursor.kind in (ci.CursorKind.FUNCTION_DECL, ci.CursorKind.CXX_METHOD) \
                and cursor.spelling:
            func = cursor.spelling
        if cursor.kind == ci.CursorKind.DECL_REF_EXPR:
            vr = self._try_ref(cursor, ancestors, func)
            if vr is not None:
                refs[cursor.referenced.spelling].append(vr)
        elif cursor.kind == ci.CursorKind.VAR_DECL:
            self._try_def(cursor, def_locs)
        for child in cursor.get_children():
            self._walk(child, ancestors + (cursor,), func, refs, def_locs)

    # ---------------- 引用识别与分类 ----------------

    def _try_ref(self, cur, ancestors, func) -> VarRef | None:
        """DeclRefExpr → VarRef；非目标全局变量返回 None。"""
        ci = self._cindex
        ref = cur.referenced
        if ref is None or ref.kind != ci.CursorKind.VAR_DECL:
            return None
        if ref.spelling not in self._var_set:
            return None
        parent = ref.semantic_parent
        if parent is None or parent.kind != ci.CursorKind.TRANSLATION_UNIT:
            return None  # 局部变量遮蔽

        kind = self._classify(cur, ancestors)
        loc = cur.location
        file = self._relpath(loc.file.name) if loc.file else ""
        return VarRef(func=func, loc=f"{file}:{loc.line}", kind=kind)

    def _classify(self, cur, ancestors) -> str:
        """沿祖先链穿过透传表达式后按最外层父节点分类读/写（§5.5-3）。"""
        ci = self._cindex
        inner = cur
        i = len(ancestors) - 1
        while i >= 0 and ancestors[i].kind in self._pass_through:
            inner = ancestors[i]
            i -= 1
        parent = ancestors[i] if i >= 0 else None
        if parent is None:
            return "read"
        if parent.kind == ci.CursorKind.BINARY_OPERATOR:
            children = list(parent.get_children())
            if children and children[0] == inner:
                op = self._operator_token(parent)
                if op == "=":
                    return "assign"
            return "read"
        if parent.kind == ci.CursorKind.COMPOUND_ASSIGNMENT_OPERATOR:
            return "compound"
        if parent.kind == ci.CursorKind.UNARY_OPERATOR:
            op = self._operator_token(parent)
            if op in ("++", "--"):
                return "incdec"
            if op == "&":
                return "addr"  # 保守记写：可能经指针传出修改
        return "read"

    def _operator_token(self, cursor) -> str:
        """运算符表达式的操作符拼写。"""
        tokens = [t.spelling for t in cursor.get_tokens()]
        if cursor.kind == self._cindex.CursorKind.UNARY_OPERATOR:
            # 前置算子（&x / ++x）在首 token，后置（x++）在末 token，
            # 取第一个命中一元算子的 token。
            for t in tokens:
                if t in ("++", "--", "&", "*", "+", "-", "!", "~"):
                    return t
            return ""
        # 二元算子：tokens = [左值..., op, 右值...]，左值 token 数不定，
        # 取第一个命中已知二元算子的 token（赋值 = 与比较 ==/<= 等区分开）。
        for t in tokens:
            if t in ("=", "==", "!=", "<=", ">=", "<", ">", "+", "-", "*", "/",
                     "%", "&&", "||", "&", "|", "^", "<<", ">>"):
                return t
        return ""

    def _try_def(self, cur, def_locs: dict[str, str]) -> None:
        """VAR_DECL 且 is_definition() → 记定义位置（先到先得）。"""
        if cur.spelling in self._var_set and cur.spelling not in def_locs \
                and cur.is_definition():
            loc = cur.location
            if loc.file:
                def_locs[cur.spelling] = f"{self._relpath(loc.file.name)}:{loc.line}"

    # ---------------- 模块判定 ----------------

    def _module_of(self, rel_file: str) -> str:
        """路径前缀映射（§5.5-3）；都不中归 other，未配置时归 default。"""
        if self.modules is None:
            return "default"
        for name, prefixes in self.modules.items():
            if any(rel_file.startswith(p) for p in prefixes):
                return name
        return "other"
