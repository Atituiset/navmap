"""数据模型 + JSON 序列化（设计文档 §4）。

JSON 与 markdown 同源生成：render.py 只消费这里的对象，保证双格式不漂移。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path


def file_hash(path: str | Path) -> str:
    """表所在文件 sha256，增量失效键。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


@dataclass
class Entry:
    msg_id: str                    # 源码宏拼写，不是展开值
    msg_id_value: str | None       # 展开值（可选，供调试）
    handler: str
    handler_loc: str | None        # file:line
    handler_usr: str | None
    cond: str | None = None        # #ifdef 条件，无则 None
    source: str = "ast"            # ast | llm-extracted


@dataclass
class StateEntry:
    """状态机表项（设计文档 §5.4）：{state, event, handler, next_state} 四元组。"""
    state: str
    event: str
    handler: str | None = None
    next_state: str | None = None
    handler_loc: str | None = None
    handler_usr: str | None = None
    cond: str | None = None


@dataclass
class VarRef:
    """全局变量一处引用（§5.5）：kind = assign|compound|incdec|addr|read。"""
    func: str
    loc: str                       # file:line
    kind: str


@dataclass
class GlobalVar:
    variable: str
    def_loc: str | None = None
    writers: list[VarRef] = field(default_factory=list)
    readers_by_module: dict[str, int] = field(default_factory=dict)
    total_refs: int = 0
    ref_files: list[str] = field(default_factory=list)  # 增量失效键（仓库相对路径）


@dataclass
class Table:
    name: str
    file: str
    line: int
    source_hash: str
    entries: list = field(default_factory=list)


@dataclass
class DispatchArtifact:
    """表类产物（dispatch / statemachine / registry 共用信封）。

    kind = "dispatch" 时表项为 Entry；kind = "statemachine" 时为 StateEntry；
    registry 结果以 Entry 挂到虚拟表 `registry:<ApiName>` 下，合入 dispatch 产物。
    """
    baseline_commit: str
    subsystem: str
    tables: list[Table] = field(default_factory=list)
    generated_at: str = ""
    kind: str = "dispatch"
    # 解析失败的候选文件（fatal diagnostics），供 report/quality 统计
    parse_failures: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.generated_at:
            self.generated_at = datetime.now(timezone.utc).astimezone().isoformat(
                timespec="seconds"
            )

    def to_dict(self) -> dict:
        return {
            "baseline_commit": self.baseline_commit,
            "generated_at": self.generated_at,
            "subsystem": self.subsystem,
            "kind": self.kind,
            "tables": [
                {
                    "name": t.name,
                    "file": t.file,
                    "line": t.line,
                    "source_hash": t.source_hash,
                    "entries": [asdict(e) for e in t.entries],
                }
                for t in self.tables
            ],
            "parse_failures": self.parse_failures,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, d: dict) -> "DispatchArtifact":
        art = cls(
            baseline_commit=d["baseline_commit"],
            subsystem=d["subsystem"],
            generated_at=d.get("generated_at", ""),
            kind=d.get("kind", "dispatch"),
            parse_failures=d.get("parse_failures", []),
        )
        entry_cls = StateEntry if art.kind == "statemachine" else Entry
        for t in d.get("tables", []):
            art.tables.append(
                Table(
                    name=t["name"],
                    file=t["file"],
                    line=t["line"],
                    source_hash=t["source_hash"],
                    entries=[entry_cls(**e) for e in t.get("entries", [])],
                )
            )
        return art


@dataclass
class GlobalvarArtifact:
    """全局变量读写清单产物（设计文档 §4/§5.5）。"""

    baseline_commit: str
    subsystem: str
    vars: list[GlobalVar] = field(default_factory=list)
    generated_at: str = ""
    kind: str = "globalvar"
    parse_failures: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.generated_at:
            self.generated_at = datetime.now(timezone.utc).astimezone().isoformat(
                timespec="seconds"
            )

    def to_dict(self) -> dict:
        return {
            "baseline_commit": self.baseline_commit,
            "generated_at": self.generated_at,
            "subsystem": self.subsystem,
            "kind": self.kind,
            "vars": [
                {
                    "variable": v.variable,
                    "def_loc": v.def_loc,
                    "writers": [asdict(w) for w in v.writers],
                    "readers_by_module": v.readers_by_module,
                    "total_refs": v.total_refs,
                    "ref_files": v.ref_files,
                }
                for v in self.vars
            ],
            "parse_failures": self.parse_failures,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, d: dict) -> "GlobalvarArtifact":
        art = cls(
            baseline_commit=d["baseline_commit"],
            subsystem=d["subsystem"],
            generated_at=d.get("generated_at", ""),
            parse_failures=d.get("parse_failures", []),
        )
        for v in d.get("vars", []):
            art.vars.append(
                GlobalVar(
                    variable=v["variable"],
                    def_loc=v.get("def_loc"),
                    writers=[VarRef(**w) for w in v.get("writers", [])],
                    readers_by_module=v.get("readers_by_module", {}),
                    total_refs=v.get("total_refs", 0),
                    ref_files=v.get("ref_files", []),
                )
            )
        return art
