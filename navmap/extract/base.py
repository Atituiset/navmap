"""提取器公共层：TU 解析、源码文本、#ifdef 条件栈、表达式剥离。

DispatchExtractor / StatemachineExtractor / RegistryExtractor / GlobalvarExtractor
共用。行为与 §5.2 一致；各提取器差异只在 cursor 消费逻辑与解析选项
（是否需要函数体由 NEEDS_FUNCTION_BODIES 控制）。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..compdb import CompilationDB


# ---------------------------------------------------------------- ifdef 回溯

_IF_RE = re.compile(r"^\s*#\s*if\s+(.+)$")
_IFDEF_RE = re.compile(r"^\s*#\s*ifdef\s+(\w+)")
_IFNDEF_RE = re.compile(r"^\s*#\s*ifndef\s+(\w+)")
_ELIF_RE = re.compile(r"^\s*#\s*elif\s+(.+)$")
_ELSE_RE = re.compile(r"^\s*#\s*else\b")
_ENDIF_RE = re.compile(r"^\s*#\s*endif\b")


def cond_map(lines: list[str]) -> dict[int, str]:
    """行号 → 该行生效的条件编译表达式（源码拼写）。简单文本回溯，不展开宏。

    #elif/#else 分支按排斥语义记录：后续分支条件为
    `!(前面分支条件) && (本分支条件)`（保守近似，嵌套 #else 只取上一层）。
    """
    # 栈元素：(当前分支条件, 已出现过的兄弟分支条件列表)
    stack: list[tuple[str, list[str]]] = []
    out: dict[int, str] = {}

    def _active(stack_snapshot) -> str | None:
        parts = [cond for cond, _ in stack_snapshot if cond]
        return " && ".join(parts) if parts else None

    for i, line in enumerate(lines, 1):
        m = _IFDEF_RE.match(line)
        if m:
            stack.append((m.group(1), [m.group(1)]))
            continue
        m = _IFNDEF_RE.match(line)
        if m:
            cond = "!" + m.group(1)
            stack.append((cond, [cond]))
            continue
        m = _IF_RE.match(line)
        if m:
            cond = m.group(1).strip()
            stack.append((cond, [cond]))
            continue
        m = _ELIF_RE.match(line)
        if m and stack:
            cond, siblings = stack[-1]
            # 本分支生效 = 前面分支都不成立 && 本条件成立
            not_prev = " && ".join(f"!({s})" for s in siblings)
            cur = m.group(1).strip()
            stack[-1] = (f"{not_prev} && {cur}", siblings + [cur])
            continue
        if _ELSE_RE.match(line) and stack:
            cond, siblings = stack[-1]
            not_prev = " && ".join(f"!({s})" for s in siblings)
            stack[-1] = (not_prev, siblings + [None])
            continue
        if _ENDIF_RE.match(line):
            if stack:
                stack.pop()
            continue
        cond = _active(stack)
        if cond:
            out[i] = cond
    return out


# ---------------------------------------------------------------- 基类


class TUExtractor:
    """按 compdb 参数逐候选文件解析 TU 的公共逻辑。"""

    #: 注册点/全局变量引用在函数体内，子类置 True
    NEEDS_FUNCTION_BODIES = False

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

    # ---------------- TU 解析 ----------------

    def _parse(self, path: str):
        """按 compdb 参数解析单文件，返回 (tu, fatal)。头文件自动借参。"""
        ci = self._cindex
        args = self.compdb.lookup(path)
        if args is None:
            borrowed = self.compdb.borrow_args_for_header(path)
            if borrowed is None:
                raise RuntimeError("compile_commands.json 中无此文件，也未找到包含它的 TU")
            args, tu_src = borrowed
            self.args_source[path] = tu_src

        args = args + self._extra_args
        options = ci.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD
        if not self.NEEDS_FUNCTION_BODIES:
            options |= ci.TranslationUnit.PARSE_SKIP_FUNCTION_BODIES
        tu = self._index.parse(path, args=args, options=options)
        fatal = any(d.severity >= ci.Diagnostic.Fatal for d in tu.diagnostics)
        return tu, fatal

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

    # ---------------- 表达式剥离 ----------------

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

    # ---------------- 结构体初始化器 fnptr 成员展开（registry/opsstruct 共用） ----------------

    def _funcptr_field_names(self, record_decl) -> list[str]:
        """结构体里函数指针成员的名字（按声明顺序）。"""
        ci = self._cindex
        names: list[str] = []
        for f in record_decl.type.get_fields():
            ft = f.type.get_canonical()
            if ft.kind == ci.TypeKind.POINTER and ft.get_pointee().kind in (
                ci.TypeKind.FUNCTIONPROTO,
                ci.TypeKind.FUNCTIONNOPROTO,
            ):
                names.append(f.spelling)
        return names

    def _iter_fnptr_inits(self, record_decl, init):
        """结构体初始化器 → 逐函数指针成员产出 (成员名, 值 cursor)。

        指定初始化器按设计器成员名配对；按位初始化按声明顺序对齐。
        与 opsstruct/registry 的消费逻辑共用（M6 结构体注册形态）。"""
        ci = self._cindex
        all_fields = [f.spelling for f in record_decl.type.get_fields()]
        fnptr_fields = set(self._funcptr_field_names(record_decl))
        pos = 0
        for raw in init.get_children():
            designator = None
            if raw.kind == ci.CursorKind.UNEXPOSED_EXPR:
                d = [c for c in raw.get_children()
                     if c.kind in (ci.CursorKind.MEMBER_REF,
                                   ci.CursorKind.MEMBER_REF_EXPR)]
                if d:
                    designator = d[0].spelling
            value = self._designated_value(raw)
            if designator is not None:
                if designator not in all_fields:
                    continue  # 未知设计器（嵌套聚合）跳过
                name, pos = designator, all_fields.index(designator) + 1
            else:
                while pos < len(all_fields) and all_fields[pos] not in fnptr_fields:
                    pos += 1
                if pos >= len(all_fields):
                    break
                name = all_fields[pos]
                pos += 1
            if name in fnptr_fields:
                yield name, value

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

    # ---------------- 源码文本 / 条件 ----------------

    def _read(self, path: str) -> bytes:
        if path not in self._text_cache:
            self._text_cache[path] = Path(path).read_bytes()
        return self._text_cache[path]

    def _extent_text(self, cursor) -> str:
        """表达式在源码中的原始拼写（保留宏名）。X-Macro 展开的元素
        spelling location 指向 .def 文件；实参 token extent 塌缩为零宽时
        返回空串。"""
        ext = cursor.extent
        if ext.start.file is None:
            return ""
        fname = ext.start.file.name
        try:
            data = self._read(fname)
        except OSError:
            return ""
        return data[ext.start.offset : ext.end.offset].decode(errors="replace").strip()

    def _decl_text(self, cursor) -> str:
        """声明语句源码文本（从声明起始行读到 `};` 收尾行）。

        宏展开使 extent 塌缩为零宽时（u-boot U_BOOT_CMD_WITH_SUBCMDS
        实测），libclang 对该声明的 extent 读取为空——从 location 起按
        行读取到最后一个 `};` 行是最后兜底。用法字符串内可能含括号，
        不做括号配对。"""
        ext = cursor.extent
        if ext.start.file is not None:
            text = self._extent_text(cursor)
            if text:
                return text
        loc = cursor.location
        if loc.file is None:
            return ""
        try:
            lines = self._read(loc.file.name).decode(errors="replace").splitlines()
        except OSError:
            return ""
        out: list[str] = []
        for i in range(loc.line - 1, min(len(lines), loc.line + 400)):
            stripped = lines[i].rstrip()
            out.append(lines[i])
            if stripped.endswith("};") or stripped.endswith(");"):
                break
        return "\n".join(out)

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
            self._cond_cache[fname] = cond_map(lines)
        return self._cond_cache[fname].get(ext.start.line)

    def _relpath(self, path: str) -> str:
        try:
            return os.path.relpath(path, self.src_root)
        except ValueError:
            return path
