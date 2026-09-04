"""注册式分发提取（设计文档 §5.3）。

无静态表、运行期注册的场景（`MsgReg(MSG_1001, sess_handle_invite);`）：

1. 注册 API 名单由配置注入（register_apis；自动扩展名单属 §5.3.1 后续工作）；
2. 提取调用点：递归遍历 TU 全部 cursor（注册调用在函数体内，
   NEEDS_FUNCTION_BODIES=True），找 CALL_EXPR，其 referenced 为
   FUNCTION_DECL 且 spelling 命中名单 → 第 1 实参取源码原始拼写（保留宏名）
   为 msg_id，函数指针实参剥 CStyleCastExpr / UnaryOperator(&) 后取
   referenced 为 handler（记 USR 与 file:line）；
3. 产出与分发表同构的 Entry，按 (api, 调用点文件) 聚合挂到虚拟表
   `registry:<ApiName>` 下。

每个候选文件用 compile_commands.json 对应 TU 的原始参数解析（§0.2）；
公共解析逻辑在 base.py。
"""

from __future__ import annotations

from ..model import Entry, Table, file_hash
from .base import TUExtractor


class RegistryExtractor(TUExtractor):
    # 注册调用在函数体内
    NEEDS_FUNCTION_BODIES = True

    def __init__(
        self,
        compdb,
        src_root,
        *,
        register_apis: list[str],
        extra_args: list[str] | None = None,
    ):
        super().__init__(compdb, src_root, extra_args=extra_args)
        self.register_apis = list(register_apis)

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
        # (api, 调用点文件) → 虚拟表；按首次出现顺序产出
        tables: dict[tuple[str, str], Table] = {}
        for call in self._walk_calls(tu.cursor):
            hit = self._try_entries(call)
            if hit is None:
                continue
            api, new_entries = hit
            loc = call.location
            fname = loc.file.name if loc.file else path
            key = (api, fname)
            tbl = tables.get(key)
            if tbl is None:
                tbl = Table(
                    name=f"registry:{api}",
                    file=self._relpath(fname),
                    line=loc.line,
                    source_hash=file_hash(fname),
                )
                tables[key] = tbl
            tbl.entries.extend(new_entries)
        return list(tables.values()), fatal

    # ---------------- 调用点识别 ----------------

    def _walk_calls(self, cursor):
        """全 TU 递归（含函数体），产出所有 CALL_EXPR。"""
        ci = self._cindex
        if cursor.kind == ci.CursorKind.CALL_EXPR:
            yield cursor
        for child in cursor.get_children():
            yield from self._walk_calls(child)

    def _try_entries(self, call) -> tuple[str, list[Entry]] | None:
        """注册调用点 → (api, 表项列表)。

        两类形态（pjproject/freeDiameter 实测）：
        1. 裸函数指针：MsgReg(MSG_1001, fn) → 单表项；
        2. 模块/回调结构体指针：pjsip_endpt_register_module(endpt, &mod) /
           fd_disp_register(&hdlers) → 把该结构体初始化器里的全部函数指针
           成员展开为表项（msg_id = 注册参数拼写 + "." + 成员名）。
        """
        ci = self._cindex
        ref = call.referenced
        if (
            ref is None
            or ref.kind != ci.CursorKind.FUNCTION_DECL
            or ref.spelling not in self.register_apis
        ):
            return None

        args = list(call.get_arguments())
        handler = None
        handler_loc = None
        handler_usr = None
        for a in args:
            fn = self._peel_to_ref(a)
            if fn is not None and fn.kind == ci.CursorKind.FUNCTION_DECL:
                handler = fn.spelling
                floc = fn.location
                if floc.file:
                    handler_loc = f"{self._relpath(floc.file.name)}:{floc.line}"
                handler_usr = fn.get_usr()
                break
        cond = self._cond_at(call)

        if handler is not None:
            msg_id, msg_id_value = "", None
            if args:
                msg_id = self._norm_arg(self._extent_text(args[0]))
                msg_id_value = self._eval(args[0])
            return ref.spelling, [Entry(
                msg_id=msg_id,
                msg_id_value=msg_id_value,
                handler=handler,
                handler_loc=handler_loc,
                handler_usr=handler_usr,
                cond=cond,
            )]

        # 形态 2：实参剥 cast/& 后指向含函数指针成员的结构体 VarDecl
        # （pjproject &mod_evsub.mod / freeDiameter &hdlers 实测形态）
        for a in args:
            entries = self._struct_handler_entries(a, cond)
            if entries is not None:
                return ref.spelling, entries
        return None  # handler 解析不到的调用点不是注册项

    def _struct_handler_entries(self, arg, cond) -> list[Entry] | None:
        """&mod / (cast*)&mod / &mod.member 形态 → 结构体注册项。

        把注册结构体初始化器里的全部函数指针成员展开为表项
        （msg_id = 成员名）；非此形态返回 None。"""
        ci = self._cindex
        var = self._struct_target_var(arg)
        if var is None:
            return None
        member_name = var[1]
        target = var[0]

        var_type = target.type.get_canonical()
        if var_type.kind != ci.TypeKind.RECORD:
            return None
        decl = var_type.get_declaration()
        if not self._funcptr_field_names(decl):
            # fnptr 不在顶层：&mod.member（member 是嵌套结构体）时下钻
            if member_name is None:
                return None
        init = None
        for child in target.get_children():
            if child.kind == ci.CursorKind.INIT_LIST_EXPR:
                init = child
                break
        if init is None:
            return None  # 结构体无初始化器（运行期填）→ 无法静态提取

        # &mod.member：先下钻到 member 的嵌套初始化器
        if member_name is not None:
            inner = self._member_init(decl, init, member_name)
            if inner is None:
                return None
            decl, init = inner
            member_name = None

        entries: list[Entry] = []
        for name, value in self._iter_fnptr_inits(decl, init):
            if member_name is not None and name != member_name:
                continue  # &mod.member：只取该成员（其类型为函数指针时）
            fn = self._peel_to_ref(value)
            if fn is None or fn.kind != ci.CursorKind.FUNCTION_DECL:
                continue
            floc = fn.location
            handler_loc = None
            if floc.file:
                handler_loc = f"{self._relpath(floc.file.name)}:{floc.line}"
            entries.append(Entry(
                msg_id=name,
                msg_id_value=None,
                handler=fn.spelling,
                handler_loc=handler_loc,
                handler_usr=fn.get_usr(),
                cond=cond,
            ))
        return entries or None

    def _member_init(self, record_decl, init, member: str):
        """在结构体初始化器里定位 member 的嵌套初始化器与内层 RECORD 声明。

        返回 (内层 record decl, member 的 INIT_LIST_EXPR) 或 None。"""
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
            if designator is not None:
                name = designator if designator in all_fields else None
                pos = (all_fields.index(designator) + 1
                       if designator in all_fields else pos)
            else:
                while pos < len(all_fields) and all_fields[pos] not in fnptr_fields:
                    pos += 1
                name = all_fields[pos] if pos < len(all_fields) else None
                pos += 1
            if name == member:
                if designator is not None:
                    raw = self._designated_value(raw) if raw.kind == \
                        ci.CursorKind.UNEXPOSED_EXPR else raw
                if raw.kind != ci.CursorKind.INIT_LIST_EXPR:
                    return None  # 成员不是聚合初始化（fnptr 或标量）
                t = raw.type.get_canonical()
                if t.kind == ci.TypeKind.RECORD:
                    return t.get_declaration(), raw
                return None
        return None

    def _struct_target_var(self, arg):
        """注册实参 → (包含注册回调的 VarDecl, member 名或 None)。

        剥 cast/unexposed/& 后：
        - DeclRefExpr → 全局结构体变量 → (var, None)
        - MemberRefExpr(&mod.member 里引用的外层 var) → (var, member 名)
        """
        ci = self._cindex
        cur = arg
        member = None
        for _ in range(6):
            if cur.kind in (
                ci.CursorKind.CSTYLE_CAST_EXPR,
                ci.CursorKind.UNARY_OPERATOR,
                ci.CursorKind.UNEXPOSED_EXPR,
            ):
                kids = [c for c in cur.get_children()
                        if c.kind not in (ci.CursorKind.TYPE_REF,)]
                if len(kids) == 1:
                    cur = kids[0]
                    continue
            break
        if cur.kind == ci.CursorKind.DECL_REF_EXPR:
            ref = cur.referenced
            if ref is not None and ref.kind == ci.CursorKind.VAR_DECL \
                    and ref.semantic_parent is not None \
                    and ref.semantic_parent.kind == ci.CursorKind.TRANSLATION_UNIT:
                return ref, None
            return None
        if cur.kind == ci.CursorKind.MEMBER_REF_EXPR:
            kids = list(cur.get_children())
            outer = next((c.referenced for c in kids
                          if c.kind == ci.CursorKind.DECL_REF_EXPR
                          and c.referenced is not None
                          and c.referenced.kind == ci.CursorKind.VAR_DECL), None)
            fld = cur.referenced
            if outer is not None and fld is not None \
                    and fld.kind == ci.CursorKind.FIELD_DECL:
                return outer, fld.spelling
        return None

    @staticmethod
    def _norm_arg(text: str) -> str:
        """实参拼写归一（与 dispatch._norm_msg_id 同义：宏调用取首实参）。"""
        import re

        m = re.match(r"([A-Za-z_]\w*)\s*\((.*)\)$", text.strip(), re.DOTALL)
        if m and m.group(2):
            first = m.group(2).split(",")[0].strip().strip('"')
            if first:
                return first
        return text

    # ---------------- 求值 ----------------

    def _eval(self, cursor) -> str | None:
        """展开值（可选，供调试）：ctypes 直调 clang_Cursor_Evaluate
        （官方 cindex.py 未打包该绑定）。非常量表达式返回 None。"""
        try:
            from .. import clangeval
        except ImportError:
            return None
        return clangeval.eval_int(cursor)
