"""scan.py 粗筛测试：误报允许，漏报不允许。"""

import os

from navmap.scan import scan


def test_scan_finds_all_table_files(fixture_dir):
    cands = scan(
        fixture_dir,
        name_roots=["table", "disp", "map", "hdlr", "state", "trans"],
        extensions=[".c", ".h", ".def"],
    )
    files = {c.file.rsplit("/", 1)[-1] for c in cands}
    # 含分发表的文件必须全部命中（漏报不允许）
    assert "disp.c" in files
    assert "disp2.c" in files
    assert "xmacro.c" in files
    assert "oam_table.h" in files


def test_scan_reasons(fixture_dir):
    cands = scan(
        fixture_dir,
        name_roots=["table", "disp"],
        register_apis=["RegisterHandler"],
        extensions=[".c", ".h"],
    )
    by_name = {c.file.rsplit("/", 1)[-1]: c for c in cands}
    # handlers.h 含函数指针 typedef → func-ptr-member
    assert "func-ptr-member" in by_name["handlers.h"].reasons
    # disp.c 含 g_msgTable[] = { → array-init
    assert "array-init" in by_name["disp.c"].reasons


def test_scan_register_api(fixture_dir):
    cands = scan(
        fixture_dir,
        name_roots=["__nomatch__"],
        register_apis=["sess_handle_invite"],  # 借 handler 名验证字面搜索
        extensions=[".c", ".h"],
    )
    files = {c.file.rsplit("/", 1)[-1] for c in cands}
    assert "handlers.h" in files  # 声明处命中 register-api 字面搜索


def test_scan_parallel_matches_serial(fixture_dir):
    """并行粗筛与串行结果完全一致。"""
    serial = scan(fixture_dir,
                  name_roots=["table", "disp", "map", "hdlr", "state", "trans"],
                  extensions=[".c", ".h", ".def"])
    parallel = scan(fixture_dir,
                    name_roots=["table", "disp", "map", "hdlr", "state", "trans"],
                    extensions=[".c", ".h", ".def"], workers=4)
    assert serial == parallel


def test_scan_excludes_build_variants(fixture_dir, tmp_path):
    """fnmatch 通配排除目录：build-* / build-*-* / _deps 下的候选不进清单。"""
    for d in ("build-asan", "build", "_deps/googletest-src"):
        os.makedirs(tmp_path / d, exist_ok=True)
    # 每个目录放一个必命中的候选文件
    for d in ("build-asan", "build", "_deps/googletest-src"):
        (tmp_path / d / "g_msgTable.c").write_text(
            "void f(void);\n"
            "void (*g_msgTable[])(void) = { f };\n")
    cands = scan(tmp_path, name_roots=["table"], extensions=[".c"])
    files = {c.file for c in cands}
    assert files == set()  # 全部被排除
