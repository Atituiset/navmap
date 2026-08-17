"""JSON → markdown 渲染（设计文档 §3[5]）。

与 JSON 同源生成：只消费 model 对象，保证双格式不漂移。
markdown 产物用于 review plan 阶段按 diff 落点子系统注入 prompt。
"""

from __future__ import annotations

from .model import DispatchArtifact, GlobalvarArtifact


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


def render_statemachine_md(art: DispatchArtifact) -> str:
    """状态机表产物 → markdown（设计文档 §5.4，四元组）。"""
    lines: list[str] = []
    lines.append(f"# 状态机表导航图 — {art.subsystem}")
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
        lines.append("| state | event | handler | next_state | 编译条件 |")
        lines.append("|---|---|---|---|---|")
        for e in t.entries:
            handler = f"`{e.handler}`" if e.handler else "—"
            nxt = f"`{e.next_state}`" if e.next_state else "—"
            cond = f"`{e.cond}`" if e.cond else "—"
            lines.append(f"| `{e.state}` | `{e.event}` | {handler} | {nxt} | {cond} |")
        lines.append("")
    if art.parse_failures:
        lines.append("## 解析失败文件")
        lines.append("")
        for f in art.parse_failures:
            lines.append(f"- `{f}`")
        lines.append("")
    return "\n".join(lines)


def render_globalvar_md(art: GlobalvarArtifact) -> str:
    """全局变量读写清单 + 布局（设计文档 §5.5/§5.6）。"""
    lines: list[str] = []
    lines.append(f"# 全局变量读写清单 — {art.subsystem}")
    lines.append("")
    lines.append(f"> 基线 commit: `{art.baseline_commit}`  ")
    lines.append(f"> 生成时间: {art.generated_at}  ")
    lines.append(f"> 变量数: {len(art.vars)}")
    lines.append("")
    for v in art.vars:
        lines.append(f"## `{v.variable}`")
        lines.append("")
        loc = f"`{v.def_loc}`" if v.def_loc else "?"
        lines.append(f"定义: {loc}  |  总引用: {v.total_refs}")
        lines.append("")
        lines.append("**写者**:")
        lines.append("")
        if v.writers:
            for w in v.writers:
                lines.append(f"- `{w.func}` — `{w.loc}`（{w.kind}）")
        else:
            lines.append("- （无）")
        lines.append("")
        if v.readers_by_module:
            readers = "，".join(f"{m} {n}" for m, n in
                               sorted(v.readers_by_module.items(),
                                      key=lambda kv: -kv[1]))
            lines.append(f"**读者（按模块）**: {readers}")
            lines.append("")
    # 布局（§5.6）：按定义位置分组的变量一览
    by_module: dict[str, list[str]] = {}
    for v in art.vars:
        mod = v.def_loc.split(":")[0] if v.def_loc else "?"
        by_module.setdefault(mod, []).append(v.variable)
    if by_module:
        lines.append("## 全局状态布局（按定义位置）")
        lines.append("")
        for loc, names in sorted(by_module.items()):
            lines.append(f"- `{loc}`: {', '.join(f'`{n}`' for n in names)}")
        lines.append("")
    if art.parse_failures:
        lines.append("## 解析失败文件")
        lines.append("")
        for f in art.parse_failures:
            lines.append(f"- `{f}`")
        lines.append("")
    return "\n".join(lines)
