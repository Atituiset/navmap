"""增量刷新（设计文档 §6）：git diff ∩ 候选 → 只重提取受影响文件并 merge。

受影响集合（§3 增量模式的 v1 实现）：
1. 变更文件本身是候选 → 重提取；
2. 变更/删除的头文件被某候选直接 #include → 该候选重提取；
3. 变更文件是某表项 handler_loc 所在文件（行号可能偏移）→ 该表所在文件重提取；
4. 变更文件原非候选、现命中粗筛 → 新候选，重提取；
5. 被删除的候选 → 直接摘表，零解析。

未命中任何一条：只 bump baseline_commit，产物零重解析。

多产物：dispatch（含 registry 虚拟表）与 statemachine 按表 merge；
globalvar 任一 ref_file 变更则整体重算（变量名单小，成本可接受）。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import clangenv
from .compdb import CompilationDB
from .model import DispatchArtifact, GlobalvarArtifact
from .render import render_dispatch_md, render_globalvar_md, render_statemachine_md
from .scan import build_matchers, match_file

_HEADER_EXTS = (".h", ".hpp", ".hh", ".inc", ".def")


@dataclass
class RefreshReport:
    up_to_date: bool
    old_baseline: str
    new_baseline: str
    changed_count: int = 0
    affected: list[str] = field(default_factory=list)
    new_candidates: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    tables_before: int = 0
    tables_after: int = 0
    globalvar_refreshed: bool = False
    reparse_failures: list[str] = field(default_factory=list)


def _git(src: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(src), *args],
                       check=True, capture_output=True, text=True)
    return r.stdout.strip()


def git_head(src: Path) -> str:
    return _git(src, "rev-parse", "HEAD")


def git_changed(src: Path, old: str, new: str) -> tuple[list[str], list[str]]:
    """old..new 变更文件（仓库相对路径）：(修改/新增, 删除)。"""
    try:
        out = _git(src, "diff", "--name-status", old, new)
    except subprocess.CalledProcessError:
        raise SystemExit(
            f"git diff {old[:12]}..{new[:12]} 失败（baseline 不可达？），请重跑 navmap extract"
        )
    changed, deleted = [], []
    for line in out.splitlines():
        parts = line.split("\t")
        status = parts[0]
        if status.startswith(("R", "C")):  # rename/copy = 删旧加新
            deleted.append(parts[1])
            changed.append(parts[2])
        elif status == "D":
            deleted.append(parts[1])
        else:
            changed.append(parts[1])
    return changed, deleted


def _include_pat(basename: str) -> re.Pattern:
    return re.compile(
        r'^\s*#\s*include\s*[<"][^">]*' + re.escape(basename) + r'[">]',
        re.MULTILINE,
    )


def _load_table_artifact(path: Path) -> DispatchArtifact | None:
    if path.is_file():
        return DispatchArtifact.from_dict(json.loads(path.read_text()))
    return None


def refresh(
    src_root: str | Path,
    compdb_path: str | Path,
    out_dir: str | Path,
    subsystem: str,
    *,
    cfg: dict,
) -> RefreshReport:
    src = Path(src_root).resolve()
    out = Path(out_dir)
    scan_cfg = cfg.get("scan", {})
    sm_cfg = cfg.get("statemachine", {})
    name_roots = scan_cfg.get("name_roots",
                              ["table", "tbl", "disp", "map", "hdlr", "state", "trans"])
    register_apis = scan_cfg.get("register_apis", [])
    extensions = scan_cfg.get("extensions")
    extra_args = cfg.get("extract", {}).get("extra_args")
    libclang_path = (cfg.get("libclang") or {}).get("path") or None
    gv_vars = cfg.get("globalvar", {}).get("variables", [])

    disp_path = out / f"navmap-dispatch-{subsystem}.json"
    sm_path = out / f"navmap-statemachine-{subsystem}.json"
    gv_path = out / f"navmap-globalvar-{subsystem}.json"
    cand_path = out / "navmap-candidates.json"
    if not disp_path.is_file():
        raise SystemExit(f"无先前产物 {disp_path}，请先运行 navmap extract")

    disp_art = DispatchArtifact.from_dict(json.loads(disp_path.read_text()))
    sm_art = _load_table_artifact(sm_path)
    gv_art = (GlobalvarArtifact.from_dict(json.loads(gv_path.read_text()))
              if gv_path.is_file() else None)

    old, new = disp_art.baseline_commit, git_head(src)
    n_tables = len(disp_art.tables) + (len(sm_art.tables) if sm_art else 0)
    rep = RefreshReport(up_to_date=(old == new), old_baseline=old, new_baseline=new,
                        tables_before=n_tables)
    if old == new:
        rep.tables_after = n_tables
        return rep

    changed, deleted = git_changed(src, old, new)
    rep.changed_count = len(changed) + len(deleted)

    prev_cands = json.loads(cand_path.read_text()) if cand_path.is_file() else []
    cand_map: dict[str, dict] = {
        os.path.relpath(c["file"], src): c for c in prev_cands
    }
    cand_rels = set(cand_map)
    exts = tuple(extensions or [".c", ".h"])
    array_re, api_res = build_matchers(name_roots, register_apis)

    affected: set[str] = set()
    # 1) 变更 ∩ 候选
    affected |= {r for r in changed if r in cand_rels}
    # 5) 删除的候选
    dropped = {r for r in deleted if r in cand_rels}
    # 2) 变更/删除的头文件被候选直接 #include
    hdrs = [r for r in changed + deleted if r.endswith(_HEADER_EXTS)]
    if hdrs:
        pats = [_include_pat(os.path.basename(r)) for r in hdrs]
        for crel in cand_rels - affected - dropped:
            try:
                text = (src / crel).read_text(errors="replace")
            except OSError:
                continue
            if any(p.search(text) for p in pats):
                affected.add(crel)
    # 3) handler_loc 所在文件变更（行号偏移）→ 表所在文件重提取
    loc_to_tables: dict[str, set[str]] = {}
    for art in (disp_art, sm_art):
        if art is None:
            continue
        for t in art.tables:
            for e in t.entries:
                if getattr(e, "handler_loc", None):
                    loc_to_tables.setdefault(
                        e.handler_loc.rsplit(":", 1)[0], set()).add(t.file)
    for rel in changed:
        affected |= {tf for tf in loc_to_tables.get(rel, ()) if tf in cand_rels}
    # 4) 原非候选的变更文件现命中粗筛 → 新候选
    new_cands: list[str] = []
    for rel in changed:
        if rel in cand_rels or not rel.endswith(exts):
            continue
        if match_file(src / rel, array_re, api_res):
            new_cands.append(rel)
            affected.add(rel)

    rep.affected = sorted(affected)
    rep.new_candidates = sorted(new_cands)
    rep.dropped = sorted(dropped)

    gone = affected | dropped
    gone_abs = {os.path.realpath(str(src / r)) for r in gone}

    def keep_failure(f: str) -> bool:
        p = f.split(": ")[0]
        rp = os.path.realpath(p) if os.path.isabs(p) \
            else os.path.realpath(str(src / p))
        return rp not in gone_abs

    # ---- 重提取 + merge（表类产物） ----
    if affected:
        clangenv.setup(libclang_path)
        from .extract.dispatch import DispatchExtractor
        from .extract.statemachine import StatemachineExtractor

        compdb = CompilationDB(compdb_path)
        abs_files = [str(src / r) for r in sorted(affected)]

        disp_ex = DispatchExtractor(compdb, src_root=src, extra_args=extra_args)
        new_tables, failures = disp_ex.extract_files(abs_files)
        if register_apis:
            from .extract.registry import RegistryExtractor

            reg_ex = RegistryExtractor(compdb, src_root=src,
                                       register_apis=register_apis,
                                       extra_args=extra_args)
            rt, rf = reg_ex.extract_files(abs_files)
            new_tables.extend(rt)
            failures.extend(rf)
        if cfg.get("extract", {}).get("ops_structs"):
            from .extract.opsstruct import OpsStructExtractor

            ops_ex = OpsStructExtractor(compdb, src_root=src,
                                         extra_args=extra_args)
            ot, of = ops_ex.extract_files(abs_files)
            new_tables.extend(ot)
            failures.extend(of)
        sm_ex = StatemachineExtractor(
            compdb, src_root=src,
            state_fields=sm_cfg.get("state_fields", ["state", "cur_state", "from"]),
            event_fields=sm_cfg.get("event_fields", ["event", "evt", "msg_id", "msg"]),
            next_state_fields=sm_cfg.get("next_state_fields",
                                         ["next_state", "next", "to"]),
            extra_args=extra_args,
        )
        new_sm, sf = sm_ex.extract_files(abs_files)
        failures.extend(sf)
        if new_sm:
            sm_keys = {(t.file, t.line) for t in new_sm}
            new_tables = [t for t in new_tables if (t.file, t.line) not in sm_keys]

        disp_art.tables = [t for t in disp_art.tables if t.file not in gone]
        disp_art.tables.extend(new_tables)
        disp_art.tables.sort(key=lambda t: (t.file, t.line))
        disp_art.parse_failures = [f for f in disp_art.parse_failures
                                   if keep_failure(f)]
        disp_art.parse_failures.extend(failures)
        rep.reparse_failures = failures

        if sm_art is not None:
            sm_art.tables = [t for t in sm_art.tables if t.file not in gone]
            sm_art.tables.extend(new_sm)
            sm_art.tables.sort(key=lambda t: (t.file, t.line))
    elif gone:
        disp_art.tables = [t for t in disp_art.tables if t.file not in gone]
        disp_art.parse_failures = [f for f in disp_art.parse_failures
                                   if keep_failure(f)]
        if sm_art is not None:
            sm_art.tables = [t for t in sm_art.tables if t.file not in gone]

    # ---- globalvar：任一 ref_file 变更 → 整体重算 ----
    if gv_art is not None and gv_vars:
        ref_files = {rf for v in gv_art.vars for rf in v.ref_files}
        if {r for r in changed + deleted if r in ref_files}:
            clangenv.setup(libclang_path)
            from .extract.globalvar import GlobalvarExtractor

            modules = {k: v for k, v in cfg.get("subsystems", {}).items()
                       if k != "default"}
            gv_ex = GlobalvarExtractor(
                CompilationDB(compdb_path), src_root=src, variables=gv_vars,
                modules=modules or None, extensions=extensions,
                extra_args=extra_args,
            )
            gv_art.vars, gv_art.parse_failures = gv_ex.extract()
            rep.globalvar_refreshed = True

    # ---- 候选清单维护 ----
    for r in dropped:
        cand_map.pop(r, None)
    for r in affected:
        reasons = match_file(src / r, array_re, api_res)
        if reasons:
            cand_map[r] = {"file": str(src / r), "reasons": reasons}
        else:  # 表被删干净，不再是候选
            cand_map.pop(r, None)
    for r in new_cands:
        cand_map[r] = {"file": str(src / r),
                       "reasons": match_file(src / r, array_re, api_res)}
    cand_path.write_text(
        json.dumps(sorted(cand_map.values(), key=lambda c: c["file"]),
                   ensure_ascii=False, indent=2)
    )

    # ---- 落盘 ----
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    disp_art.baseline_commit = new
    disp_art.generated_at = now
    disp_path.write_text(disp_art.to_json() + "\n")
    (out / f"navmap-dispatch-{subsystem}.md").write_text(
        render_dispatch_md(disp_art))
    if sm_art is not None:
        sm_art.baseline_commit = new
        sm_art.generated_at = now
        sm_path.write_text(sm_art.to_json() + "\n")
        (out / f"navmap-statemachine-{subsystem}.md").write_text(
            render_statemachine_md(sm_art))
    if gv_art is not None:
        gv_art.baseline_commit = new
        gv_art.generated_at = now
        gv_path.write_text(gv_art.to_json() + "\n")
        (out / f"navmap-globalvar-{subsystem}.md").write_text(
            render_globalvar_md(gv_art))

    rep.tables_after = len(disp_art.tables) + (len(sm_art.tables) if sm_art else 0)
    return rep
