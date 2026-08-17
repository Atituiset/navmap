"""navmap CLI：extract / report 子命令（设计文档 §3、§6）。

M1 范围：scan 粗筛 → compdb 查参 → dispatch 提取 → JSON + markdown。
report：读产物出覆盖率/失败摘要（quality.py 三指标属 M2）。
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path

from . import clangenv
from .compdb import CompilationDB
from .model import DispatchArtifact
from .render import render_dispatch_md
from .scan import scan


def _load_config(path: str | None) -> dict:
    default = Path(__file__).resolve().parent.parent / "config" / "navmap.toml"
    p = Path(path) if path else default
    if p.is_file():
        with open(p, "rb") as f:
            return tomllib.load(f)
    return {}


def cmd_extract(args: argparse.Namespace) -> int:
    cfg = _load_config(args.config)
    scan_cfg = cfg.get("scan", {})

    libclang_path = (cfg.get("libclang") or {}).get("path") or None
    lib = clangenv.setup(libclang_path)
    print(f"[navmap] libclang: {lib} ({clangenv.libclang_version(lib)})", file=sys.stderr)

    # [1] 文本粗筛（分钟级）→ 候选文件清单
    candidates = scan(
        args.src,
        name_roots=scan_cfg.get("name_roots", ["table", "disp", "map", "hdlr", "state", "trans"]),
        register_apis=scan_cfg.get("register_apis", []),
        extensions=scan_cfg.get("extensions"),
    )
    print(f"[navmap] 粗筛候选文件: {len(candidates)}", file=sys.stderr)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    cand_json = out_dir / "navmap-candidates.json"
    cand_json.write_text(
        json.dumps([{"file": c.file, "reasons": c.reasons} for c in candidates],
                   ensure_ascii=False, indent=2)
    )

    # [2] compdb 查参 → [3] 只解析候选文件
    compdb = CompilationDB(args.compdb)
    from .extract.dispatch import DispatchExtractor

    extractor = DispatchExtractor(compdb, src_root=args.src,
                                  extra_args=cfg.get("extract", {}).get("extra_args"))
    tables, failures = extractor.extract_files([c.file for c in candidates])

    art = DispatchArtifact(
        baseline_commit=args.baseline,
        subsystem=args.subsystem,
        tables=tables,
        parse_failures=failures,
    )

    # [5] JSON + markdown 同源产出
    json_path = out_dir / f"navmap-dispatch-{args.subsystem}.json"
    md_path = out_dir / f"navmap-dispatch-{args.subsystem}.md"
    json_path.write_text(art.to_json() + "\n")
    md_path.write_text(render_dispatch_md(art))

    print(f"[navmap] 表: {len(tables)}，表项: {sum(len(t.entries) for t in tables)}，"
          f"解析失败: {len(failures)}", file=sys.stderr)
    for hdr, tu in extractor.args_source.items():
        print(f"[navmap] 头文件借参: {hdr} ← TU {tu}", file=sys.stderr)
    print(f"[navmap] 产物: {json_path} / {md_path}", file=sys.stderr)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """读取产物出摘要（M1 简化版；ERROR/WARN/INFO 分级属 M2 quality.py）。"""
    out_dir = Path(args.out)
    arts = []
    for p in sorted(out_dir.glob("navmap-dispatch-*.json")):
        arts.append(DispatchArtifact.from_dict(json.loads(p.read_text())))

    total_tables = sum(len(a.tables) for a in arts)
    total_entries = sum(len(t.entries) for a in arts for t in a.tables)
    total_failures = sum(len(a.parse_failures) for a in arts)
    no_usr = sum(
        1 for a in arts for t in a.tables for e in t.entries if not e.handler_usr
    )
    no_loc = sum(
        1 for a in arts for t in a.tables for e in t.entries if not e.handler_loc
    )

    print("# navmap 质量摘要（M1）")
    print()
    print(f"- 子系统数: {len(arts)}")
    print(f"- 分发表: {total_tables}，表项: {total_entries}")
    print(f"- ERROR 级: 解析失败文件 {total_failures} 个；handler USR 缺失 {no_usr} 条")
    print(f"- WARN 级: handler 位置缺失 {no_loc} 条")
    for a in arts:
        print(f"  - [{a.subsystem}] 表 {len(a.tables)}，表项 "
              f"{sum(len(t.entries) for t in a.tables)}，失败 {len(a.parse_failures)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="navmap", description="C/C++ 导航图提取器（libclang 20.1.0）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pe = sub.add_parser("extract", help="粗筛 + 提取分发表，产出 JSON + markdown")
    pe.add_argument("--src", required=True, help="源码根目录")
    pe.add_argument("--compdb", required=True, help="compile_commands.json 路径")
    pe.add_argument("--baseline", required=True, help="基线 commit")
    pe.add_argument("--out", required=True, help="产物输出目录")
    pe.add_argument("--subsystem", default="default", help="子系统名（产物文件名后缀）")
    pe.add_argument("--config", default=None, help="navmap.toml 路径（默认取项目 config/）")
    pe.set_defaults(fn=cmd_extract)

    pr = sub.add_parser("report", help="产物质量摘要")
    pr.add_argument("--out", required=True, help="产物目录")
    pr.set_defaults(fn=cmd_report)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
