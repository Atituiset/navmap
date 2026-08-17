"""状态机表提取（设计文档 §5.4）。

与分发表复用同一套 InitListExpr 遍历（base.py），差异在字段映射：
`{state, event, handler, next_state}` 四元组。字段名配置化
（config/navmap.toml [statemachine]，各团队命名不同）。

识别规则：数组元素结构体同时含 state_fields 与 event_fields 命中的字段名，
且含函数指针成员或 next_state_fields 命中字段 → 状态机表。
switch 式手写状态机不做（按设计）。
"""

from __future__ import annotations

from ..model import StateEntry, Table, file_hash
from .base import TUExtractor


class StatemachineExtractor(TUExtractor):
    def __init__(
        self,
        compdb,
        src_root,
        *,
        state_fields: list[str],
        event_fields: list[str],
        next_state_fields: list[str],
        extra_args: list[str] | None = None,
    ):
        super().__init__(compdb, src_root, extra_args=extra_args)
        self._state_fields = {s.lower() for s in state_fields}
        self._event_fields = {s.lower() for s in event_fields}
        self._next_fields = {s.lower() for s in next_state_fields}

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

    def _extract_one(self, path: str) -> tuple[list[Table], bool]:
        tu, fatal = self._parse(path)
        tables: list[Table] = []
        for cur in self._walk_globals(tu.cursor):
            tbl = self._try_table(cur)
            if tbl is not None:
                tables.append(tbl)
        return tables, fatal

    # ---------------- 表识别 ----------------

    def _field_names(self, record_decl) -> list[str]:
        return [f.spelling for f in record_decl.type.get_fields()]

    def _is_sm_record(self, record_decl) -> bool:
        names = {n.lower() for n in self._field_names(record_decl)}
        if not (names & self._state_fields and names & self._event_fields):
            return False
        return bool(names & self._next_fields) or self._has_funcptr_member(record_decl)

    def _try_table(self, var) -> Table | None:
        ci = self._cindex
        t = var.type
        if t.kind not in (ci.TypeKind.CONSTANTARRAY, ci.TypeKind.INCOMPLETEARRAY):
            return None
        elem = t.get_array_element_type().get_canonical()
        if elem.kind != ci.TypeKind.RECORD:
            return None
        decl = elem.get_declaration()
        if not self._is_sm_record(decl):
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
        fields = self._field_names(decl)
        for entry_cur in init.get_children():
            if entry_cur.kind != ci.CursorKind.INIT_LIST_EXPR:
                continue
            e = self._extract_row(entry_cur, fields)
            if e is not None:
                table.entries.append(e)
        return table

    # ---------------- 表项 ----------------

    def _row_cells(self, entry_cur, fields: list[str]):
        """逐字段拆表项：指定初始化器取设计器字段名，否则按声明顺序对齐。
        产出 (field_name, value_cursor) 序列。"""
        ci = self._cindex
        cells: list[tuple[str, object]] = []
        pos = 0
        for raw_child in entry_cur.get_children():
            designator = None
            if raw_child.kind == ci.CursorKind.UNEXPOSED_EXPR:
                children = list(raw_child.get_children())
                d = [c for c in children
                     if c.kind in (ci.CursorKind.MEMBER_REF, ci.CursorKind.MEMBER_REF_EXPR)]
                if d:
                    designator = d[0].spelling
            value = self._designated_value(raw_child)
            if designator:
                name = designator
                if name in fields:
                    pos = fields.index(name) + 1
            else:
                name = fields[pos] if pos < len(fields) else ""
                pos += 1
            cells.append((name, value))
        return cells

    def _extract_row(self, entry_cur, fields: list[str]) -> StateEntry | None:
        state = event = next_state = ""
        handler = handler_loc = handler_usr = None

        for name, value in self._row_cells(entry_cur, fields):
            ref = self._peel_to_ref(value)
            if ref is not None and ref.kind == self._cindex.CursorKind.FUNCTION_DECL:
                handler = ref.spelling
                rloc = ref.location
                if rloc.file:
                    handler_loc = f"{self._relpath(rloc.file.name)}:{rloc.line}"
                handler_usr = ref.get_usr()
                continue
            text = self._extent_text(value)
            if not text:
                continue
            lname = name.lower()
            if lname in self._state_fields:
                state = text
            elif lname in self._event_fields:
                event = text
            elif lname in self._next_fields:
                next_state = text

        # 有效行：至少有 state/event 之一 + handler/next_state 之一
        if not (state or event) or not (handler or next_state):
            return None
        return StateEntry(
            state=state,
            event=event,
            handler=handler,
            next_state=next_state or None,
            handler_loc=handler_loc,
            handler_usr=handler_usr,
            cond=self._cond_at(entry_cur),
        )
