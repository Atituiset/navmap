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
        # 逐层剥数组维度并计数：tbl[N](dims=1) / tbl[N][M](dims=2) / ...
        dims = 1
        elem = t.get_array_element_type().get_canonical()
        while elem.kind in (ci.TypeKind.CONSTANTARRAY, ci.TypeKind.INCOMPLETEARRAY):
            dims += 1
            elem = elem.get_array_element_type().get_canonical()
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
        for idx_path, entry_cur in self._iter_init_leaves(init):
            if bare_fnptr:
                e = self._extract_fnptr_entry(entry_cur, idx_path)
                if e is not None:
                    table.entries.append(e)
        if not bare_fnptr:
            # 表项兜底用：整个 VarDecl 的源码文本（宏展开塌缩 extent 时，
            # 从宏调用实参文本按表项序号配对——见 _extract_entry）
            var_text = self._decl_text(var)
            for entry_idx, entry_cur in enumerate(
                    self._iter_struct_entries(init, dims - 1)):
                e = self._extract_entry(entry_cur, var_text, entry_idx)
                if e is not None:
                    table.entries.append(e)
        return table  # 空表也保留（覆盖率统计用）

    # ---------------- 初始化列表遍历（支持多维数组） ----------------

    def _iter_init_leaves(self, init, prefix: tuple = ()):
        """嵌套 INIT_LIST_EXPR 递归展开（裸函数指针数组用）：
        一维 → ((i), handler)；多维 → ((i,j,...), handler)。"""
        ci = self._cindex
        for idx, child in enumerate(init.get_children()):
            if child.kind == ci.CursorKind.INIT_LIST_EXPR:
                yield from self._iter_init_leaves(child, prefix + (idx,))
            else:
                yield prefix + (idx,), child

    def _iter_struct_entries(self, init, depth: int):
        """结构体表的表项遍历：depth = 剩余「行」层数（数组维度 - 1）。

        depth == 0 时子节点即表项——即使表项首字段是聚合初始化
        （如 `{ {1,2}, handler }` 的 ids[2]），也整体产出不拆开。"""
        ci = self._cindex
        for child in init.get_children():
            if child.kind != ci.CursorKind.INIT_LIST_EXPR:
                continue
            if depth > 0:
                yield from self._iter_struct_entries(child, depth - 1)
            else:
                yield child

    # ---------------- 裸函数指针数组 ----------------

    def _is_function_pointer(self, t) -> bool:
        ci = self._cindex
        if t.kind != ci.TypeKind.POINTER:
            return False
        pt = t.get_pointee().get_canonical()
        return pt.kind in (ci.TypeKind.FUNCTIONPROTO, ci.TypeKind.FUNCTIONNOPROTO)

    def _extract_fnptr_entry(self, child, idx_path: tuple) -> Entry | None:
        """裸函数指针数组元素：即 handler 本身，msg_id 取数组下标（多维为复合下标）。"""
        ref = self._peel_to_ref(child)
        if ref is None or ref.kind != self._cindex.CursorKind.FUNCTION_DECL:
            return None
        handler_loc = None
        rloc = ref.location
        if rloc.file:
            handler_loc = f"{self._relpath(rloc.file.name)}:{rloc.line}"
        msg_id = ",".join(str(i) for i in idx_path)
        return Entry(
            msg_id=msg_id,
            msg_id_value=msg_id,
            handler=ref.spelling,
            handler_loc=handler_loc,
            handler_usr=ref.get_usr(),
            cond=self._cond_at(child),
        )

    def _extract_entry(self, entry_cur, var_text: str = "", entry_idx: int = -1) -> Entry | None:
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
                # 跳过隐式零初始化（无源码拼写且非字面量）；宏展开塌缩
                # extent 的字段也保留（eval_str 仍可求字符串值）
                if self._extent_text(child) or self._is_literal_like(child):
                    scalars.append(child)

        if handler is None:
            return None  # 无 handler 的元素不是分发项（AST 过滤误报）

        msg_id, msg_id_value = "", None
        if scalars:
            msg_id = self._norm_msg_id(self._extent_text(scalars[0]))
            if not msg_id:
                # 宏展开塌缩 extent 的字符串字段（u-boot #name 字符串化）：
                # 求值兜底
                msg_id = self._eval_str(scalars[0]) or ""
            msg_id_value = self._eval(scalars[0])
        if not msg_id:
            # X-Macro 场景：子节点 extent 塌缩（实参 token 无独立 range），
            # 且 libclang 无法对非主文件范围分词。按优先级兜底：
            #  1. 表项 extent 文本：宏调用形态 U_BOOT_SUBCMD_MKENT(info, 2, 1,
            #     do_blkmap_common) → 首实参（剥引号）为 msg_id；
            #  2. 展开形态 {.name = "info", .cmd = fn} / { "info", ... }
            #     → 第一个字符串字面量或非 handler 标识符；
            #  3. 全塌缩（u-boot blkmap 实测：9 表项 extent 全指宏调用首行且
            #     读取为空串）→ 用整表 VarDecl 源码文本，按表项序号配对第 N 个
            #     实参组的首实参。
            text = self._extent_text(entry_cur)
            if text:
                msg_id = self._norm_msg_id(text)
                if msg_id == text:  # 不是宏调用形态
                    m = re.search(r'"([^"]+)"', text)
                    if m:
                        msg_id = m.group(1)
                    else:
                        for m in re.finditer(r"([A-Za-z_]\w*)\s*(\()?", text):
                            ident, paren = m.group(1), m.group(2)
                            if not paren and ident != handler:
                                msg_id = ident
                                break
        if not msg_id and var_text and entry_idx >= 0:
            msg_id = self._msg_id_from_var_text(var_text, entry_idx, handler)

        cond = self._cond_at(entry_cur)
        return Entry(
            msg_id=msg_id,
            msg_id_value=msg_id_value,
            handler=handler,
            handler_loc=handler_loc,
            handler_usr=handler_usr,
            cond=cond,
        )

    def _is_literal_like(self, cursor) -> bool:
        """无源码拼写但可能有常量值的字段（宏塌缩 extent 的字面量）。"""
        ci = self._cindex
        return cursor.kind in (
            ci.CursorKind.INTEGER_LITERAL,
            ci.CursorKind.STRING_LITERAL,
            ci.CursorKind.UNEXPOSED_EXPR,
            ci.CursorKind.UNARY_OPERATOR,
            ci.CursorKind.BINARY_OPERATOR,
        )

    def _eval_str(self, cursor) -> str | None:
        """字符串字段求值兜底（clangeval.eval_str）。"""
        try:
            from .. import clangeval
        except ImportError:
            return None
        return clangeval.eval_str(cursor)

    # ---------------- 求值 ----------------

    _CALL_ARG_RE = re.compile(r"([A-Za-z_]\w*)\s*\(")

    @classmethod
    def _msg_id_from_var_text(cls, var_text: str, entry_idx: int,
                              handler: str | None) -> str:
        """extent 全塌缩时的最终兜底：在整表声明源码文本里按深度优先收集
        叶子层宏调用（无嵌套调用的 IDENT(args)），取第 entry_idx 个的
        首实参为 msg_id。

        u-boot 实测形态（U_BOOT_CMD_WITH_SUBCMDS 生成的 blkmap_subcmds）：
        表项元素 extent 全部塌缩到宏调用首行且读为空；声明文本里的叶子
        调用 = [START(x), MKENT(name, ...)×N, END]，滤掉 0/1 实参的
        结构宏后恰与表项一一对应。"""
        if not var_text:
            return ""
        leaves: list[str] = []

        def _scan(seg: str, is_top: bool) -> None:
            pos = 0
            while True:
                m = cls._CALL_ARG_RE.search(seg, pos)
                if not m:
                    return
                start = m.end()
                depth = 1
                j = start
                n = len(seg)
                while j < n and depth > 0:
                    c = seg[j]
                    if c == "(":
                        depth += 1
                    elif c == ")":
                        depth -= 1
                    j += 1
                if depth != 0:  # 未闭合（截断文本），放弃剩余
                    return
                args_text = seg[start:j - 1]
                inner_m = cls._CALL_ARG_RE.search(args_text)
                if inner_m:
                    _scan(args_text, False)  # 有嵌套 → 下钻
                elif not is_top:
                    parts = cls._split_args(args_text)
                    if parts is not None:
                        leaves.append(parts)
                pos = j

        # 顶层调用本身（= 整个声明宏）不下钻收集，只递归它的实参区
        m = cls._CALL_ARG_RE.match(var_text.strip())
        if m and var_text.strip().endswith(")"):
            depth = 1
            j = m.end()
            while j < len(var_text) and depth > 0:
                c = var_text[j]
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                j += 1
            if depth == 0:
                _scan(var_text[m.end():j - 1], False)
        else:
            _scan(var_text, True)

        # 结构性宏（0/1 实参）滤除后须与表项数对齐才能按序号配对
        if entry_idx < len(leaves):
            arg = leaves[entry_idx]
            if handler is None or arg != handler:
                return arg.strip().strip('"')
        return ""

    @staticmethod
    def _split_args(args_text: str) -> str | None:
        """顶层逗号切分；要求 ≥2 个实参（表项宏至少带 name + handler，
        滤掉 U_BOOT_SUBCMD_START(x) 这类 1 实参结构宏）、非语句体。"""
        if "{" in args_text or ";" in args_text:
            return None
        depth = 0
        first: str | None = None
        nargs = 0
        seg_start = 0
        for k, c in enumerate(args_text):
            if c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
            elif c == "," and depth == 0:
                if nargs == 0:
                    first = args_text[seg_start:k].strip()
                nargs += 1
                seg_start = k + 1
        tail = args_text[seg_start:].strip()
        if tail:
            if nargs == 0:
                first = tail
            nargs += 1
        if nargs < 2 or not first:
            return None
        return first

    @staticmethod
    def _norm_msg_id(text: str) -> str:
        """宏调用形态的标量拼写归一：CMD_MKENT(a, b) → a（剥引号）。

        u-boot U_BOOT_SUBCMD_MKENT(info, 2, 1, fn) 展开后的首字段
        extent 覆盖整个宏调用，取首实参才是真实 msg_id。
        """
        m = re.match(r"([A-Za-z_]\w*)\s*\((.*)\)$", text.strip(), re.DOTALL)
        if m and m.group(2):
            first_arg = m.group(2).split(",")[0].strip().strip('"')
            if first_arg:
                return first_arg
        return text

    def _eval(self, cursor) -> str | None:
        """展开值（可选，供调试）：ctypes 直调 clang_Cursor_Evaluate
        （官方 cindex.py 未打包该绑定）。非常量表达式返回 None。"""
        try:
            from .. import clangeval
        except ImportError:
            return None
        return clangeval.eval_int(cursor)
