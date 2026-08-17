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
            hit = self._try_entry(call)
            if hit is None:
                continue
            api, entry = hit
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
            tbl.entries.append(entry)
        return list(tables.values()), fatal

    # ---------------- 调用点识别 ----------------

    def _walk_calls(self, cursor):
        """全 TU 递归（含函数体），产出所有 CALL_EXPR。"""
        ci = self._cindex
        if cursor.kind == ci.CursorKind.CALL_EXPR:
            yield cursor
        for child in cursor.get_children():
            yield from self._walk_calls(child)

    def _try_entry(self, call) -> tuple[str, Entry] | None:
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
        if handler is None:
            return None  # handler 解析不到的调用点不是注册项

        msg_id, msg_id_value = "", None
        if args:
            msg_id = self._extent_text(args[0])
            msg_id_value = self._eval(args[0])

        cond = self._cond_at(call)
        return ref.spelling, Entry(
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
