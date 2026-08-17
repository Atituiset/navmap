"""产物质检（设计文档 §7）：覆盖率、一致性、孤儿 handler，ERROR/WARN/INFO 分级。

- ERROR：提取器 bug 信号——解析失败文件、handler USR 缺失；
- WARN：覆盖缺口——handler 位置缺失、空表、msg_id 缺失；
- INFO：统计与线索——孤儿 handler（定义了但不在任何表/注册点，命名启发式
  `handle_*` / `*_hdlr`）、消息枚举覆盖率（需配置 [quality] msgid_headers）。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .model import DispatchArtifact, GlobalvarArtifact

ERROR, WARN, INFO = "ERROR", "WARN", "INFO"

# 孤儿 handler 命名启发式（§7）
_ORPHAN_NAME_RE = re.compile(
    r"^\s*(?:static\s+)?[\w\*]+\s+(handle_\w+|\w+_hdlr)\s*\([^;]*$")
# 消息枚举来源：#define MSG_x 0x123 / enum 成员
_DEFINE_RE = re.compile(r"^\s*#\s*define\s+(\w+)\s+(?:0x[0-9a-fA-F]+|\d+)")
_ENUM_MEMBER_RE = re.compile(r"^\s*(\w+)\s*(?:=\s*(?:0x[0-9a-fA-F]+|\d+))?\s*,?\s*$")


@dataclass
class Issue:
    level: str
    msg: str


@dataclass
class QualityReport:
    issues: list[Issue] = field(default_factory=list)
    stats: dict[str, str] = field(default_factory=dict)

    def add(self, level: str, msg: str) -> None:
        self.issues.append(Issue(level, msg))

    def counts(self) -> dict[str, int]:
        out = {ERROR: 0, WARN: 0, INFO: 0}
        for i in self.issues:
            out[i.level] += 1
        return out


def _load_artifacts(out_dir: Path):
    """读产物目录全部 artifact：(kind, artifact) 列表。"""
    arts = []
    for p in sorted(out_dir.glob("navmap-*.json")):
        if p.name == "navmap-candidates.json":
            continue
        d = json.loads(p.read_text())
        kind = d.get("kind", "dispatch")
        if kind == "globalvar":
            arts.append((kind, GlobalvarArtifact.from_dict(d)))
        else:
            arts.append((kind, DispatchArtifact.from_dict(d)))
    return arts


def check_tables(rep: QualityReport, art: DispatchArtifact) -> None:
    """表类产物（dispatch/statemachine/registry）一致性检查。"""
    sub = f"[{art.kind}:{art.subsystem}]"
    for f in art.parse_failures:
        rep.add(ERROR, f"{sub} 解析失败: {f}")
    for t in art.tables:
        if not t.entries:
            rep.add(WARN, f"{sub} 空表: {t.name} ({t.file}:{t.line})")
        for e in t.entries:
            handler = getattr(e, "handler", None)
            if handler and not getattr(e, "handler_usr", None):
                rep.add(ERROR, f"{sub} handler USR 缺失: {handler}（表 {t.name}）")
            if handler and not getattr(e, "handler_loc", None):
                rep.add(WARN, f"{sub} handler 位置缺失: {handler}（表 {t.name}）")
            if art.kind == "dispatch" and not getattr(e, "msg_id", ""):
                rep.add(WARN, f"{sub} msg_id 缺失: {handler}（表 {t.name}）")


def check_globalvar(rep: QualityReport, art: GlobalvarArtifact) -> None:
    sub = f"[globalvar:{art.subsystem}]"
    for f in art.parse_failures:
        rep.add(ERROR, f"{sub} 解析失败: {f}")
    for v in art.vars:
        if v.def_loc is None:
            rep.add(WARN, f"{sub} 定义位置未找到: {v.variable}")
        if not v.writers:
            rep.add(INFO, f"{sub} 无写者（只读/外部写入？）: {v.variable}")


def orphan_handlers(src: Path, candidates: list[str], tables) -> list[str]:
    """候选文件里定义了、但不出现在任何表项的 handle_*/*_hdlr 函数。"""
    used = {getattr(e, "handler", None) for t in tables for e in t.entries}
    used.discard(None)
    orphans: list[str] = []
    for f in candidates:
        try:
            lines = Path(f).read_text(errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            m = _ORPHAN_NAME_RE.match(line)
            if m and m.group(1) not in used:
                rel = os.path.relpath(f, src)
                orphans.append(f"{m.group(1)}（{rel}:{i}）")
    return sorted(set(orphans))


def extract_msg_ids(header: Path) -> set[str]:
    """从协议头文件文本提取消息枚举：#define 群 + enum 成员（§7 覆盖率分子）。"""
    ids: set[str] = set()
    try:
        text = header.read_text(errors="replace")
    except OSError:
        return ids
    for m in _DEFINE_RE.finditer(text):
        ids.add(m.group(1))
    for em in re.finditer(r"enum\s+\w*\s*\{([^}]*)\}", text, re.DOTALL):
        for line in em.group(1).splitlines():
            line = line.split("//")[0].split("/*")[0].strip()
            m = _ENUM_MEMBER_RE.match(line)
            if m:
                ids.add(m.group(1))
    return ids


def build_report(
    out_dir: str | Path,
    src: str | Path | None = None,
    msgid_headers: list[str] | None = None,
) -> str:
    """汇总产物目录，输出分级质检报告（markdown）。"""
    out = Path(out_dir)
    rep = QualityReport()
    arts = _load_artifacts(out)

    all_tables = []
    for kind, art in arts:
        if kind == "globalvar":
            check_globalvar(rep, art)
        else:
            check_tables(rep, art)
            all_tables.extend(art.tables)

    # 统计（INFO）
    for kind, art in arts:
        if kind == "globalvar":
            rep.stats[f"globalvar:{art.subsystem}"] = (
                f"变量 {len(art.vars)}，写者 {sum(len(v.writers) for v in art.vars)}，"
                f"失败 {len(art.parse_failures)}")
        else:
            rep.stats[f"{art.kind}:{art.subsystem}"] = (
                f"表 {len(art.tables)}，表项 {sum(len(t.entries) for t in art.tables)}，"
                f"失败 {len(art.parse_failures)}")

    src_path = Path(src).resolve() if src else None
    # 孤儿 handler（需 --src + candidates 清单）
    cand_file = out / "navmap-candidates.json"
    if src_path and cand_file.is_file():
        candidates = [c["file"] for c in json.loads(cand_file.read_text())]
        for name in orphan_handlers(src_path, candidates, all_tables):
            rep.add(INFO, f"孤儿 handler（不在任何表/注册点）: {name}")

    # 消息枚举覆盖率（需配置 msgid_headers）
    if src_path and msgid_headers:
        covered_ids = {getattr(e, "msg_id", "") for t in all_tables for e in t.entries}
        covered_ids.discard("")
        for hdr in msgid_headers:
            ids = extract_msg_ids(src_path / hdr)
            if not ids:
                rep.add(WARN, f"消息枚举头文件无内容或未找到: {hdr}")
                continue
            hit = ids & covered_ids
            miss = sorted(ids - covered_ids)
            pct = len(hit) * 100 // len(ids)
            rep.add(INFO, f"覆盖率 {hdr}: {len(hit)}/{len(ids)}（{pct}%）有 handler")
            for m in miss[:20]:  # 截断防刷屏
                rep.add(WARN, f"  未覆盖: {m}（{hdr}）")

    # ---- 渲染 ----
    counts = rep.counts()
    lines = ["# navmap 质检报告", ""]
    lines.append(f"- ERROR {counts[ERROR]} / WARN {counts[WARN]} / INFO {counts[INFO]}")
    lines.append("")
    if rep.stats:
        lines.append("## 统计")
        lines.append("")
        for k, v in rep.stats.items():
            lines.append(f"- [{k}] {v}")
        lines.append("")
    for level in (ERROR, WARN, INFO):
        msgs = [i.msg for i in rep.issues if i.level == level]
        if msgs:
            lines.append(f"## {level}")
            lines.append("")
            for m in msgs:
                lines.append(f"- {m}")
            lines.append("")
    return "\n".join(lines)
