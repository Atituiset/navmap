"""单 ops-struct 分发提取（形态缺口 ②）。

形态：非数组的全局/静态结构体变量，成员含 ≥1 个函数指针，以指定初始化器
或按位初始化器填充 —— curl 的 `struct Curl_protocol Curl_protocol_file =
{ file_setup_connection, file_do, ... }`、pjsip 的 `mod_tsx_layer =
{ ..., mod_tsx_layer_load /* load() */, ... }`、Linux 驱动的
`file_operations` 惯用法。

产出：与分发表同构的 Entry（msg_id = 结构体成员名，handler = 函数）。
每个 ops-struct 一张"虚拟表"，表名 = 变量名。

与 dispatch.py 的差异：dispatch 只认数组（CONSTANT/INCOMPLETEARRAY）；
本提取器只认单结构体（RECORD 且非数组）。registry/statemachine 的识别
互斥（有 state+event 字段的表归状态机提取器，注册调用点归 registry）。
"""

from __future__ import annotations

from ..model import Entry, Table, file_hash
from .base import TUExtractor


class OpsStructExtractor(TUExtractor):
    """单 ops-struct 提取：结构体含 ≥2 个函数指针成员才算分发 ops
    （≥2 是启发式阈值：单回调句柄（如错误钩子）不是分发面）。"""

    #: 单 ops-struct 非数组 → 不需函数体
    NEEDS_FUNCTION_BODIES = False

    MIN_FUNCPTRS = 2

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
            tbl = self._try_ops_struct(cur)
            if tbl is not None:
                tables.append(tbl)
        return tables, fatal

    # ---------------- 识别 ----------------

    def _try_ops_struct(self, var) -> Table | None:
        ci = self._cindex
        t = var.type.get_canonical()
        if t.kind != ci.TypeKind.RECORD:
            return None  # 数组形态归 dispatch，这里只做单结构体
        decl = t.get_declaration()
        fnptr_fields = self._funcptr_field_names(decl)
        if len(fnptr_fields) < self.MIN_FUNCPTRS:
            return None

        init = self._var_init_list(var)
        if init is None:
            return None  # 声明无初始化（extern/前置）→ 运行期注册形态，归 registry

        loc = var.location
        file = self._relpath(loc.file.name) if loc.file else ""
        table = Table(
            name=var.spelling,
            file=file,
            line=loc.line,
            source_hash=file_hash(loc.file.name) if loc.file else "",
        )
        for name, value in self._iter_fnptr_inits(decl, init):
            ref = self._peel_to_ref(value)
            if ref is None or ref.kind != ci.CursorKind.FUNCTION_DECL:
                continue  # ZERO_NULL / NULL / 函数指针变量，非直接函数引用
            rloc = ref.location
            handler_loc = None
            if rloc.file:
                handler_loc = f"{self._relpath(rloc.file.name)}:{rloc.line}"
            table.entries.append(Entry(
                msg_id=name,
                msg_id_value=None,
                handler=ref.spelling,
                handler_loc=handler_loc,
                handler_usr=ref.get_usr(),
                cond=self._cond_at(value),
            ))
        return table  # 空 entries 也保留（类型是 ops-struct 但全 NULL 填充）
