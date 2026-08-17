"""JSON → markdown 渲染（设计文档 §3[5]）。

与 JSON 同源生成：只消费 model 对象，保证双格式不漂移。
markdown 产物用于 review plan 阶段按 diff 落点子系统注入 prompt。
"""

from __future__ import annotations

from .model import DispatchArtifact


def render_dispatch_md(art: DispatchArtifact) -> str:
    lines: list[str] = []
    lines.append(f"# 消息分发表导航图 — {art.subsystem}")
    lines.append("")
    lines.append(f"> 基线 commit: `{art.baseline_commit}`  ")
    lines.append(f"> 生成时间: {art.generated_at}  ")
    lines.append(f"> 表数量: {len(art.tables)}，表项总数: {sum(len(t.entries) for t in art.tables)}")
    lines.append("")
    for t in art.tables:
        lines.append(f"## `{t.name}`")
        lines.append("")
        lines.append(f"位置: `{t.file}:{t.line}`  ")
        lines.append(f"source_hash: `{t.source_hash[:23]}…`")
        lines.append("")
        if not t.entries:
            lines.append("（空表）")
            lines.append("")
            continue
        lines.append("| msg_id | handler | handler 位置 | 编译条件 |")
        lines.append("|---|---|---|---|")
        for e in t.entries:
            cond = f"`{e.cond}`" if e.cond else "—"
            loc = f"`{e.handler_loc}`" if e.handler_loc else "?"
            lines.append(f"| `{e.msg_id}` | `{e.handler}` | {loc} | {cond} |")
        lines.append("")
    if art.parse_failures:
        lines.append("## 解析失败文件")
        lines.append("")
        for f in art.parse_failures:
            lines.append(f"- `{f}`")
        lines.append("")
    return "\n".join(lines)
