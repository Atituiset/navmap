"""消息分发表提取（设计文档 §5.2）。

AST 遍历逻辑：
1. 找 VarDecl：数组且元素类型（typedef 解引用后）是含函数指针成员的结构体；
2. 取其 InitListExpr 逐元素初始化：
   - handler 字段：剥掉 CStyleCastExpr / UnaryOperator(&) / UnexposedExpr 后
     取 referenced（函数声明）→ 记 USR 与 file:line；
   - msg_id 字段：取初始化表达式在源码中的原始拼写（extent 截取，保留宏名），
     展开值经 cursor.evaluate() 尽力取；
3. #ifdef 条件：源文本按行回溯条件编译栈；
4. X-Macro：展开后元素 location 指向 .def 文件，spelling location 天然正确。

每个候选文件用 compile_commands.json 对应 TU 的原始参数解析（§0.2）；
头文件借包含它的 .c TU 的参数（§5.2 末尾）。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..compdb import CompilationDB
from ..model import Entry, Table, file_hash


# ---------------------------------------------------------------- ifdef 回溯

_IF_RE = re.compile(r"^\s*#\s*if\s+(.+)$")
_IFDEF_RE = re.compile(r"^\s*#\s*ifdef\s+(\w+)")
_IFNDEF_RE = re.compile(r"^\s*#\s*ifndef\s+(\w+)")
_ELIF_RE = re.compile(r"^\s*#\s*elif\s+(.+)$")
_ELSE_RE = re.compile(r"^\s*#\s*else\b")
_ENDIF_RE = re.compile(r"^\s*#\s*endif\b")


def _cond_map(lines: list[str]) -> dict[int, str]:
    """行号 → 该行生效的条件编译表达式（源码拼写）。简单文本回溯，不展开宏。"""
    stack: list[str] = []
    out: dict[int, str] = {}
    for i, line in enumerate(lines, 1):
        m = _IFDEF_RE.match(line)
        if m:
            stack.append(m.group(1))
            continue
        m = _IFNDEF_RE.match(line)
        if m:
            stack.append("!" + m.group(1))
            continue
        m = _IF_RE.match(line)
        if m:
            stack.append(m.group(1).strip())
            continue
        m = _ELIF_RE.match(line)
        if m and stack:
            stack[-1] = m.group(1).strip()
            continue
        if _ELSE_RE.match(line) and stack:
            stack[-1] = f"!({stack[-1]})"
            continue
        if _ENDIF_RE.match(line):
            if stack:
                stack.pop()
            continue
        if stack:
            out[i] = " && ".join(stack)
    return out


# ---------------------------------------------------------------- 提取器


class DispatchExtractor:
    def __init__(
        self,
        compdb: CompilationDB,
        src_root: str | Path,
        extra_args: list[str] | None = None,
    ):
        import clang.cindex as cindex  # 需 clangenv.setup() 先行

        self._cindex = cindex
        self.compdb = compdb
        self.src_root = Path(src_root).resolve()
        self._extra_args = list(extra_args or [])
        self._index = cindex.Index.create()
        self._text_cache: dict[str, bytes] = {}
        self._cond_cache: dict[str, dict[int, str]] = {}
        # 头文件 → 借用的 TU 来源标注（产物可追溯）
        self.args_source: dict[str, str] = {}

    # ---------------- 公共入口 ----------------

    def extract_files(self, files: list[str]) -> tuple[list[Table], list[str]]:
        tables: list[Table] = []
        failures: list[str] = []
        for f in files:
            try:
                t, fatal = self._extract_one(f)
                tables.extend(t)
                if fatal:
                    failures.append(f)
            except Exception as e:  # 单文件失败不能整批崩
                failures.append(f"{f}: {e}")
        return tables, failures

    # ---------------- 单文件 ----------------

    def _extract_one(self, path: str) -> tuple[list[Table], bool]:
        ci = self._cindex
        args = self.compdb.lookup(path)
        if args is None:
            borrowed = self.compdb.borrow_args_for_header(path)
            if borrowed is None:
                raise RuntimeError("compile_commands.json 中无此文件，也未找到包含它的 TU")
            args, tu_src = borrowed
            self.args_source[path] = tu_src

        args = args + self._extra_args
        tu = self._index.parse(
            path,
            args=args,
            options=(
                ci.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD
                | ci.TranslationUnit.PARSE_SKIP_FUNCTION_BODIES
            ),
        )
        fatal = any(d.severity >= ci.Diagnostic.Fatal for d in tu.diagnostics)

        tables: list[Table] = []
        for cur in self._walk_globals(tu.cursor):
            tbl = self._try_table(cur)
            if tbl is not None:
                tables.append(tbl)
        return tables, fatal

    def _walk_globals(self, cursor):
        """只走全局作用域：不进函数体（表都在文件/命名空间作用域）。"""
        ci = self._cindex
        if cursor.kind == ci.CursorKind.VAR_DECL:
            yield cursor
            return
        if cursor.kind in (
            ci.CursorKind.FUNCTION_DECL,
            ci.CursorKind.CXX_METHOD,
            ci.CursorKind.FUNCTION_TEMPLATE,
        ):
            return
        for child in cursor.get_children():
            yield from self._walk_globals(child)

    # ---------------- 表识别 ----------------

    def _try_table(self, var) -> Table | None:
        ci = self._cindex
        t = var.type
        if t.kind not in (ci.TypeKind.CONSTANTARRAY, ci.TypeKind.INCOMPLETEARRAY):
            return None
        elem = t.get_array_element_type().get_canonical()
        if elem.kind != ci.TypeKind.RECORD:
            return None
        decl = elem.get_declaration()
        if not self._has_funcptr_member(decl):
            return None  # 元素结构体不含函数指针成员 → 不是分发表

        init = None
        for child in var.get_children():
            if child.kind == ci.CursorKind.INIT_LIST_EXPR:
                init = child
                break
        if init is None:
            return None

        loc = var.location
        file = self._relpath(loc.file.name) if loc.file else ""
        table = Table(
            name=var.spelling,
            file=file,
            line=loc.line,
            source_hash=file_hash(loc.file.name) if loc.file else "",
        )
        for entry_cur in init.get_children():
            if entry_cur.kind != ci.CursorKind.INIT_LIST_EXPR:
                continue
            e = self._extract_entry(entry_cur)
            if e is not None:
                table.entries.append(e)
        return table  # 空表也保留（覆盖率统计用）

    def _has_funcptr_member(self, record_decl) -> bool:
        """结构体是否含函数指针成员（按类型识别，不靠命名）。"""
        ci = self._cindex
        for f in record_decl.type.get_fields():
            ft = f.type.get_canonical()
            if ft.kind == ci.TypeKind.POINTER and ft.get_pointee().kind in (
                ci.TypeKind.FUNCTIONPROTO,
                ci.TypeKind.FUNCTIONNOPROTO,
            ):
                return True
        return False

    # ---------------- 表项 ----------------

    def _designated_value(self, child):
        """DesignatedInitExpr 归一：libclang 表现为 UNEXPOSED_EXPR，
        子节点 = [MEMBER_REF 设计器, 值表达式]。取值表达式本身。"""
        ci = self._cindex
        if child.kind != ci.CursorKind.UNEXPOSED_EXPR:
            return child
        children = list(child.get_children())
        if not any(c.kind in (ci.CursorKind.MEMBER_REF, ci.CursorKind.MEMBER_REF_EXPR)
                   for c in children):
            return child
        vals = [c for c in children
                if c.kind not in (ci.CursorKind.MEMBER_REF, ci.CursorKind.MEMBER_REF_EXPR)]
        return vals[0] if len(vals) == 1 else child

    def _extract_entry(self, entry_cur) -> Entry | None:
        handler = None
        handler_loc = None
        handler_usr = None
        scalars: list = []

        for raw_child in entry_cur.get_children():
            child = self._designated_value(raw_child)
            ref = self._peel_to_ref(child)
            if ref is not None and ref.kind == self._cindex.CursorKind.FUNCTION_DECL:
                handler = ref.spelling
                rloc = ref.location
                if rloc.file:
                    handler_loc = f"{self._relpath(rloc.file.name)}:{rloc.line}"
                handler_usr = ref.get_usr()
            else:
                text = self._extent_text(child)
                if text:  # 跳过隐式零初始化（无源码拼写）
                    scalars.append(child)

        if handler is None:
            return None  # 无 handler 的元素不是分发项（AST 过滤误报）

        msg_id, msg_id_value = "", None
        if scalars:
            msg_id = self._extent_text(scalars[0])
            msg_id_value = self._eval(scalars[0])
        if not msg_id:
            # X-Macro 场景：子节点 extent 塌缩（实参 token 无独立 range），
            # 且 libclang 无法对非主文件范围分词——直接对表项 extent 的源码
            # 文本（msg.def 原始拼写）做正则分词，取第一个非宏名、非 handler
            # 的标识符。
            text = self._extent_text(entry_cur)
            for m in re.finditer(r"([A-Za-z_]\w*)\s*(\()?", text):
                ident, paren = m.group(1), m.group(2)
                if not paren and ident != handler:
                    msg_id = ident
                    break

        cond = self._cond_at(entry_cur)
        return Entry(
            msg_id=msg_id,
            msg_id_value=msg_id_value,
            handler=handler,
            handler_loc=handler_loc,
            handler_usr=handler_usr,
            cond=cond,
        )

    def _peel_to_ref(self, cursor):
        """剥 CStyleCastExpr / UnaryOperator(&) / UnexposedExpr，到 DeclRefExpr 为止。

        CStyleCastExpr 的子节点可能带 TypeRef（typedef 拼写的目标类型），
        需跳过，只沿值表达式下钻。
        """
        ci = self._cindex
        skip = {ci.CursorKind.TYPE_REF, ci.CursorKind.TEMPLATE_REF, ci.CursorKind.NAMESPACE_REF}
        cur = cursor
        for _ in range(8):  # 防御性上限
            if cur.kind == ci.CursorKind.DECL_REF_EXPR:
                return cur.referenced
            if cur.kind in (
                ci.CursorKind.CSTYLE_CAST_EXPR,
                ci.CursorKind.UNARY_OPERATOR,
                ci.CursorKind.UNEXPOSED_EXPR,
            ):
                children = [c for c in cur.get_children() if c.kind not in skip]
                if len(children) == 1:
                    cur = children[0]
                    continue
            return None
        return None

    # ---------------- 源码文本 / 求值 / 条件 ----------------

    def _read(self, path: str) -> bytes:
        if path not in self._text_cache:
            self._text_cache[path] = Path(path).read_bytes()
        return self._text_cache[path]

    def _extent_text(self, cursor) -> str:
        """初始化表达式在源码中的原始拼写（保留宏名）。X-Macro 展开的元素
        spelling location 指向 .def 文件；实参 token extent 塌缩为零宽时
        返回空串，由表项级分词兜底（见 _extract_entry）。"""
        ext = cursor.extent
        if ext.start.file is None:
            return ""
        fname = ext.start.file.name
        try:
            data = self._read(fname)
        except OSError:
            return ""
        return data[ext.start.offset : ext.end.offset].decode(errors="replace").strip()

    def _eval(self, cursor) -> str | None:
        """展开值（可选，供调试）。

        已知限制：官方 cindex.py 未打包 clang_Cursor_Evaluate 绑定
        （pip clang 20.1.0 亦无），M2 用 ctypes 直调 clang_Cursor_Evaluate 补。
        """
        return None

    def _cond_at(self, cursor) -> str | None:
        ext = cursor.extent
        if ext.start.file is None:
            return None
        fname = ext.start.file.name
        if fname not in self._cond_cache:
            try:
                lines = self._read(fname).decode(errors="replace").splitlines()
            except OSError:
                lines = []
            self._cond_cache[fname] = _cond_map(lines)
        return self._cond_cache[fname].get(ext.start.line)

    def _relpath(self, path: str) -> str:
        try:
            return os.path.relpath(path, self.src_root)
        except ValueError:
            return path
