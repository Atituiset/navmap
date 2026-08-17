"""incr.py 增量刷新测试：git 仓内变更 → 只重提取受影响文件并 merge。"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "mini_ims"


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args],
                       check=True, capture_output=True, text=True)
    return r.stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """mini_ims 拷入临时 git 仓并提交，compdb 按临时路径重生成。"""
    dst = tmp_path / "mini_ims"
    shutil.copytree(FIXTURE_DIR, dst, ignore=shutil.ignore_patterns(
        "compile_commands.json", "__pycache__"))
    subprocess.run([sys.executable, str(dst / "gen_compdb.py")],
                   check=True, capture_output=True)
    _git(dst, "init", "-q")
    _git(dst, "add", "-A")
    _git(dst, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "init")
    return dst


def _cli(argv: list[str]) -> int:
    from navmap.cli import main

    return main(argv)


def _extract(repo: Path, out: Path) -> None:
    rc = _cli([
        "extract", "--src", str(repo),
        "--compdb", str(repo / "compile_commands.json"),
        "--baseline", _git(repo, "rev-parse", "HEAD"),
        "--subsystem", "ims", "--out", str(out),
    ])
    assert rc == 0


def _refresh(repo: Path, out: Path) -> int:
    return _cli([
        "refresh", "--src", str(repo),
        "--compdb", str(repo / "compile_commands.json"),
        "--subsystem", "ims", "--out", str(out),
    ])


def _load(out: Path) -> dict:
    return json.loads((out / "navmap-dispatch-ims.json").read_text())


def test_refresh_up_to_date(repo: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    _extract(repo, out)
    assert _refresh(repo, out) == 0  # baseline 未变，直接短路
    art = _load(out)
    assert len(art["tables"]) >= 4


def test_refresh_picks_up_table_change(repo: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    _extract(repo, out)
    old_head = _git(repo, "rev-parse", "HEAD")

    # 改 g_msgTable：加一条表项（新宏同步在 msg_ids.h 定义）
    ids = repo / "include" / "msg_ids.h"
    ids.write_text(ids.read_text() + "#define MSG_1005 0x1005\n")
    disp = repo / "disp.c"
    text = disp.read_text()
    assert "MSG_1003" in text
    disp.write_text(text.replace(
        "{ MSG_1003,", "{ MSG_1005, sess_handle_invite },\n    { MSG_1003,", 1))
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "add entry")
    new_head = _git(repo, "rev-parse", "HEAD")

    assert _refresh(repo, out) == 0
    art = _load(out)
    assert art["baseline_commit"] == new_head != old_head

    tables = {t["name"]: t for t in art["tables"]}
    msg_ids = {e["msg_id"] for e in tables["g_msgTable"]["entries"]}
    assert "MSG_1005" in msg_ids          # 新表项进来了
    assert "MSG_1001" in msg_ids          # 旧表项还在
    # 未触碰的表原样保留（merge 不是全量重算）
    assert "g_xmsgTable" in tables
    # 候选清单仍含 disp.c
    cands = {c["file"] for c in json.loads((out / "navmap-candidates.json").read_text())}
    assert str(repo / "disp.c") in cands


def test_refresh_drops_deleted_candidate(repo: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    _extract(repo, out)

    _git(repo, "rm", "-q", "xmacro.c")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "rm xmacro")

    assert _refresh(repo, out) == 0
    art = _load(out)
    assert all(t["file"] != "xmacro.c" for t in art["tables"])
    cands = {c["file"] for c in json.loads((out / "navmap-candidates.json").read_text())}
    assert str(repo / "xmacro.c") not in cands


def test_refresh_header_touch_reextracts(repo: Path, tmp_path: Path) -> None:
    """变更被候选 #include 的头文件 → 包含它的候选重提取（handler_loc 行号刷新）。"""
    out = tmp_path / "out"
    _extract(repo, out)

    hdr = repo / "include" / "handlers.h"
    hdr.write_text("\n" + hdr.read_text())  # 行号整体下移一行
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "touch hdr")

    assert _refresh(repo, out) == 0
    art = _load(out)
    tables = {t["name"]: t for t in art["tables"]}
    e = next(e for e in tables["g_msgTable"]["entries"] if e["msg_id"] == "MSG_1001")
    line = int(e["handler_loc"].rsplit(":", 1)[1])
    # handlers.h 中 sess_handle_invite 的实际行号
    real = next(i for i, ln in enumerate(hdr.read_text().splitlines(), 1)
                if "sess_handle_invite" in ln)
    assert line == real
