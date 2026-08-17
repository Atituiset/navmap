"""scan.py 粗筛测试：误报允许，漏报不允许。"""

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
