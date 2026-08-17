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
class Table:
    name: str
    file: str
    line: int
    source_hash: str
    entries: list[Entry] = field(default_factory=list)


@dataclass
class DispatchArtifact:
    baseline_commit: str
    subsystem: str
    tables: list[Table] = field(default_factory=list)
    generated_at: str = ""
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
            parse_failures=d.get("parse_failures", []),
        )
        for t in d.get("tables", []):
            art.tables.append(
                Table(
                    name=t["name"],
                    file=t["file"],
                    line=t["line"],
                    source_hash=t["source_hash"],
                    entries=[Entry(**e) for e in t.get("entries", [])],
                )
            )
        return art
