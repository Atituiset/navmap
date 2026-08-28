"""消息分发表提取（设计文档 §5.2）。

AST 遍历逻辑：
1. 找 VarDecl：数组且元素类型（typedef 解引用后）是含函数指针成员的结构体；
2. 取其 InitListExpr 逐元素初始化：
   - handler 字段：剥掉 CStyleCastExpr / UnaryOperator(&) / UnexposedExpr 后
     取 referenced（函数声明）→ 记 USR 与 file:line；
   - msg_id 字段：取初始化表达式在源码中的原始拼写（extent 截取，保留宏名），
     展开值经 clang_Cursor_Evaluate 尽力取（ctypes 直调，cindex.py 未打包）；
3. #ifdef 条件：源文本按行回溯条件编译栈；
4. X-Macro：展开后元素 location 指向 .def 文件，spelling location 天然正确。

每个候选文件用 compile_commands.json 对应 TU 的原始参数解析（§0.2）；
头文件借包含它的 .c TU 的参数（§5.2 末尾）。公共解析逻辑在 base.py。
"""

from __future__ import annotations

import re

from ..model import Entry, Table, file_hash
from .base import TUExtractor


class DispatchExtractor(TUExtractor):
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
        tu, fatal = self._parse(path)
        tables: list[Table] = []
        for cur in self._walk_globals(tu.cursor):
            tbl = self._try_table(cur)
            if tbl is not None:
                tables.append(tbl)
        return tables, fatal

    # ---------------- 表识别 ----------------

    def _try_table(self, var) -> Table | None:
        ci = self._cindex
        t = var.type
        if t.kind not in (ci.TypeKind.CONSTANTARRAY, ci.TypeKind.INCOMPLETEARRAY):
            return None
        elem = t.get_array_element_type().get_canonical()
        bare_fnptr = False
        if elem.kind == ci.TypeKind.RECORD:
            decl = elem.get_declaration()
            if not self._has_funcptr_member(decl):
                return None  # 元素结构体不含函数指针成员 → 不是分发表
        elif self._is_function_pointer(elem):
            bare_fnptr = True  # 裸函数指针数组：{handler, ...}（typedef/using 别名均可）
        else:
            return None

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
        for idx, entry_cur in enumerate(init.get_children()):
            if bare_fnptr:
                e = self._extract_fnptr_entry(entry_cur, idx)
            else:
                if entry_cur.kind != ci.CursorKind.INIT_LIST_EXPR:
                    continue
                e = self._extract_entry(entry_cur)
            if e is not None:
                table.entries.append(e)
        return table  # 空表也保留（覆盖率统计用）

    # ---------------- 裸函数指针数组 ----------------

    def _is_function_pointer(self, t) -> bool:
        ci = self._cindex
        if t.kind != ci.TypeKind.POINTER:
            return False
        pt = t.get_pointee().get_canonical()
        return pt.kind in (ci.TypeKind.FUNCTIONPROTO, ci.TypeKind.FUNCTIONNOPROTO)

    def _extract_fnptr_entry(self, child, idx: int) -> Entry | None:
        """裸函数指针数组元素：即 handler 本身，msg_id 取数组下标。"""
        ref = self._peel_to_ref(child)
        if ref is None or ref.kind != self._cindex.CursorKind.FUNCTION_DECL:
            return None
        handler_loc = None
        rloc = ref.location
        if rloc.file:
            handler_loc = f"{self._relpath(rloc.file.name)}:{rloc.line}"
        return Entry(
            msg_id=str(idx),
            msg_id_value=str(idx),
            handler=ref.spelling,
            handler_loc=handler_loc,
            handler_usr=ref.get_usr(),
            cond=self._cond_at(child),
        )

    # ---------------- 表项 ----------------

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

    # ---------------- 求值 ----------------

    def _eval(self, cursor) -> str | None:
        """展开值（可选，供调试）：ctypes 直调 clang_Cursor_Evaluate
        （官方 cindex.py 未打包该绑定）。非常量表达式返回 None。"""
        try:
            from .. import clangeval
        except ImportError:
            return None
        return clangeval.eval_int(cursor)
