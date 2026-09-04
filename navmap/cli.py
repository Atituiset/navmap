"""navmap CLI：extract / refresh / report 子命令（设计文档 §3、§6、§7）。

extract：scan 粗筛 → compdb 查参 → dispatch/registry/statemachine(/globalvar)
提取 → 各产物 JSON + markdown。refresh：git diff 增量刷新（incr.py）。
report：产物质检（quality.py，ERROR/WARN/INFO 分级）。
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
from .render import render_dispatch_md, render_globalvar_md, render_statemachine_md
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
        name_roots=scan_cfg.get("name_roots", ["table", "tbl", "disp", "map", "hdlr", "state", "trans"]),
        register_apis=scan_cfg.get("register_apis", []),
        extensions=scan_cfg.get("extensions"),
        exclude_dirs=tuple(scan_cfg.get("exclude_dirs") or ()) or None,
        workers=args.workers or 0,
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
    extra_args = cfg.get("extract", {}).get("extra_args")
    from .extract.dispatch import DispatchExtractor

    extractor = DispatchExtractor(compdb, src_root=args.src, extra_args=extra_args)
    tables, failures = extractor.extract_files([c.file for c in candidates])

    # [3b] 注册式分发（配置 register_apis 后启用；虚拟表合入 dispatch 产物）
    register_apis = scan_cfg.get("register_apis", [])
    if register_apis:
        from .extract.registry import RegistryExtractor

        reg_ex = RegistryExtractor(compdb, src_root=args.src,
                                   register_apis=register_apis, extra_args=extra_args)
        rt, rf = reg_ex.extract_files([c.file for c in candidates])
        tables.extend(rt)
        failures.extend(rf)

    # [3b'] 单 ops-struct 分发（curl Curl_protocol / Linux file_operations 形态；
    # 默认关闭，配置 [extract] ops_structs = true 启用，虚拟表合入 dispatch 产物）
    if cfg.get("extract", {}).get("ops_structs"):
        from .extract.opsstruct import OpsStructExtractor

        ops_ex = OpsStructExtractor(compdb, src_root=args.src,
                                     extra_args=extra_args)
        ot, of = ops_ex.extract_files([c.file for c in candidates])
        tables.extend(ot)
        failures.extend(of)

    # [3c] 状态机表（字段映射命中的表从 dispatch 产物剔除，归状态机产物）
    sm_cfg = cfg.get("statemachine", {})
    from .extract.statemachine import StatemachineExtractor

    sm_ex = StatemachineExtractor(
        compdb, src_root=args.src,
        state_fields=sm_cfg.get("state_fields", ["state", "cur_state", "from"]),
        event_fields=sm_cfg.get("event_fields", ["event", "evt", "msg_id", "msg"]),
        next_state_fields=sm_cfg.get("next_state_fields", ["next_state", "next", "to"]),
        extra_args=extra_args,
    )
    sm_tables, sm_failures = sm_ex.extract_files([c.file for c in candidates])
    failures.extend(sm_failures)
    if sm_tables:
        sm_keys = {(t.file, t.line) for t in sm_tables}
        tables = [t for t in tables if (t.file, t.line) not in sm_keys]

    failures = sorted(set(failures))
    art = DispatchArtifact(
        baseline_commit=args.baseline,
        subsystem=args.subsystem,
        tables=tables,
        parse_failures=failures,
    )
    sm_art = DispatchArtifact(
        baseline_commit=args.baseline,
        subsystem=args.subsystem,
        tables=sm_tables,
        kind="statemachine",
        parse_failures=[],
    )

    # [5] JSON + markdown 同源产出
    json_path = out_dir / f"navmap-dispatch-{args.subsystem}.json"
    md_path = out_dir / f"navmap-dispatch-{args.subsystem}.md"
    json_path.write_text(art.to_json() + "\n")
    md_path.write_text(render_dispatch_md(art))
    sm_json = out_dir / f"navmap-statemachine-{args.subsystem}.json"
    sm_md = out_dir / f"navmap-statemachine-{args.subsystem}.md"
    sm_json.write_text(sm_art.to_json() + "\n")
    sm_md.write_text(render_statemachine_md(sm_art))

    # [3d] 全局变量读写清单（配置 [globalvar] variables 后启用）
    gv_paths = ""
    gv_vars = cfg.get("globalvar", {}).get("variables", [])
    if gv_vars:
        from .extract.globalvar import GlobalvarExtractor
        from .model import GlobalvarArtifact

        modules = {k: v for k, v in cfg.get("subsystems", {}).items()
                   if k != "default"}
        gv_ex = GlobalvarExtractor(
            compdb, src_root=args.src, variables=gv_vars,
            modules=modules or None,
            extensions=scan_cfg.get("extensions"),
            extra_args=extra_args,
        )
        vars_, gv_failures = gv_ex.extract()
        gv_art = GlobalvarArtifact(
            baseline_commit=args.baseline,
            subsystem=args.subsystem,
            vars=vars_,
            parse_failures=gv_failures,
        )
        gv_json = out_dir / f"navmap-globalvar-{args.subsystem}.json"
        gv_md = out_dir / f"navmap-globalvar-{args.subsystem}.md"
        gv_json.write_text(gv_art.to_json() + "\n")
        gv_md.write_text(render_globalvar_md(gv_art))
        gv_paths = f" / {gv_json}"
        print(f"[navmap] 全局变量: {len(vars_)}，解析失败: {len(gv_failures)}",
              file=sys.stderr)

    print(f"[navmap] 表: {len(tables)}，表项: {sum(len(t.entries) for t in tables)}，"
          f"状态机表: {len(sm_tables)}，解析失败: {len(failures)}", file=sys.stderr)
    for hdr, tu in extractor.args_source.items():
        print(f"[navmap] 头文件借参: {hdr} ← TU {tu}", file=sys.stderr)
    print(f"[navmap] 产物: {json_path} / {md_path} / {sm_json}{gv_paths}",
          file=sys.stderr)
    return 0


def cmd_refresh(args: argparse.Namespace) -> int:
    cfg = _load_config(args.config)

    from .incr import refresh

    rep = refresh(args.src, args.compdb, args.out, args.subsystem, cfg=cfg)
    if rep.up_to_date:
        print(f"[navmap] 已是最新（baseline {rep.new_baseline[:12]}），无需刷新",
              file=sys.stderr)
        return 0
    print(f"[navmap] baseline: {rep.old_baseline[:12]} → {rep.new_baseline[:12]}，"
          f"变更文件 {rep.changed_count} 个", file=sys.stderr)
    print(f"[navmap] 受影响候选 {len(rep.affected)}，新候选 {len(rep.new_candidates)}，"
          f"删除候选 {len(rep.dropped)}", file=sys.stderr)
    for f in rep.affected:
        print(f"[navmap] 重提取: {f}", file=sys.stderr)
    gv = "，globalvar 已重算" if rep.globalvar_refreshed else ""
    print(f"[navmap] 表: {rep.tables_before} → {rep.tables_after}{gv}，"
          f"本次解析失败 {len(rep.reparse_failures)}", file=sys.stderr)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """产物质检报告（设计文档 §7，ERROR/WARN/INFO 分级）。"""
    from .quality import build_report

    cfg = _load_config(args.config)
    msgid_headers = cfg.get("quality", {}).get("msgid_headers")
    print(build_report(args.out, src=args.src, msgid_headers=msgid_headers))
    return 0


def cmd_suggest_apis(args: argparse.Namespace) -> int:
    """注册 API 候选发现（设计文档 §5.3-1 半自动部分）。"""
    cfg = _load_config(args.config)
    from .suggest import suggest_register_apis

    clangenv.setup((cfg.get("libclang") or {}).get("path") or None)
    known = cfg.get("scan", {}).get("register_apis", [])
    sug, failures = suggest_register_apis(
        args.src, args.compdb,
        threshold=args.threshold,
        extensions=cfg.get("scan", {}).get("extensions"),
        extra_args=cfg.get("extract", {}).get("extra_args"),
        known_apis=known,
    )
    lines = ["# 注册 API 候选（人审后拷入 [scan] register_apis）", ""]
    lines.append("| API | 不同 handler 数 | 调用点数 | 状态 |")
    lines.append("|---|---|---|---|")
    for s in sug:
        mark = "已入名单" if s.api in known else "候选"
        lines.append(f"| `{s.api}` | {len(s.distinct_handlers)} | {s.call_sites} | {mark} |")
    report = "\n".join(lines)
    print(report)
    if args.out:
        Path(args.out).write_text(report + "\n")
        print(f"[navmap] 已写出: {args.out}", file=sys.stderr)
    if failures:
        print(f"解析失败 {len(failures)} 个文件（详见 stderr 不列）",
              file=sys.stderr)
    return 0


def cmd_suggest_vars(args: argparse.Namespace) -> int:
    """全局变量 Top-N 候选（设计文档 §5.5-1 自动候选部分）。"""
    cfg = _load_config(args.config)
    from .suggest import suggest_global_vars

    sug = suggest_global_vars(
        args.src, top=args.top,
        extensions=cfg.get("scan", {}).get("extensions"))
    known = set(cfg.get("globalvar", {}).get("variables", []))
    lines = ["# 全局变量候选 Top-%d（人审后拷入 [globalvar] variables）" % args.top, ""]
    lines.append("| 变量 | 全仓引用次数 | 声明位置 | 状态 |")
    lines.append("|---|---|---|---|")
    for s in sug:
        mark = "已入名单" if s.name in known else "候选"
        lines.append(f"| `{s.name}` | {s.refs} | `{s.decl_file}` | {mark} |")
    report = "\n".join(lines)
    print(report)
    if args.out:
        Path(args.out).write_text(report + "\n")
        print(f"[navmap] 已写出: {args.out}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="navmap", description="C/C++ 导航图提取器（libclang 20.1.0）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pe = sub.add_parser("extract", help="粗筛 + 提取分发表/状态机表(/注册式/全局变量)，产出 JSON + markdown")
    pe.add_argument("--src", required=True, help="源码根目录")
    pe.add_argument("--compdb", required=True, help="compile_commands.json 路径")
    pe.add_argument("--baseline", required=True, help="基线 commit")
    pe.add_argument("--out", required=True, help="产物输出目录")
    pe.add_argument("--subsystem", default="default", help="子系统名（产物文件名后缀）")
    pe.add_argument("--config", default=None, help="navmap.toml 路径（默认取项目 config/）")
    pe.add_argument("--workers", type=int, default=0,
                    help="粗筛并行进程数（0 = 串行；默认 0）")
    pe.set_defaults(fn=cmd_extract)

    pr = sub.add_parser("report", help="产物质检报告（ERROR/WARN/INFO 分级）")
    pr.add_argument("--out", required=True, help="产物目录")
    pr.add_argument("--src", default=None, help="源码根目录（孤儿 handler/覆盖率检查需要）")
    pr.add_argument("--config", default=None, help="navmap.toml 路径（默认取项目 config/）")
    pr.set_defaults(fn=cmd_report)

    pf = sub.add_parser("refresh", help="增量刷新：git diff ∩ 候选 → 只重提取受影响文件并 merge")
    pf.add_argument("--src", required=True, help="源码根目录（git 仓库）")
    pf.add_argument("--compdb", required=True, help="compile_commands.json 路径")
    pf.add_argument("--out", required=True, help="产物输出目录（须有先前 extract 产物）")
    pf.add_argument("--subsystem", default="default", help="子系统名（产物文件名后缀）")
    pf.add_argument("--config", default=None, help="navmap.toml 路径（默认取项目 config/）")
    pf.set_defaults(fn=cmd_refresh)

    pa = sub.add_parser("suggest-apis", help="注册 API 候选发现（人审后入 [scan] register_apis）")
    pa.add_argument("--src", required=True, help="源码根目录")
    pa.add_argument("--compdb", required=True, help="compile_commands.json 路径")
    pa.add_argument("--threshold", type=int, default=5, help="不同 handler 数阈值（默认 5）")
    pa.add_argument("--out", default=None, help="候选清单输出文件（markdown；缺省只打印）")
    pa.add_argument("--config", default=None, help="navmap.toml 路径（默认取项目 config/）")
    pa.set_defaults(fn=cmd_suggest_apis)

    pv = sub.add_parser("suggest-vars", help="全局变量 Top-N 候选（人审后入 [globalvar] variables）")
    pv.add_argument("--src", required=True, help="源码根目录")
    pv.add_argument("--top", type=int, default=20, help="取引用次数前 N（默认 20）")
    pv.add_argument("--out", default=None, help="候选清单输出文件（markdown；缺省只打印）")
    pv.add_argument("--config", default=None, help="navmap.toml 路径（默认取项目 config/）")
    pv.set_defaults(fn=cmd_suggest_vars)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
