"""suggest.py 测试：注册 API 候选发现 + 全局变量 Top-N 候选。"""

import pytest


def test_suggest_register_apis_finds_msgreg(fixture_dir):
    """fixture 的 MsgReg（3 个不同 handler）应被发现并标注。"""
    from navmap import clangenv
    from navmap.suggest import suggest_register_apis

    clangenv.setup()
    sug, failures = suggest_register_apis(
        fixture_dir,
        fixture_dir / "compile_commands.json",
        threshold=2,
        extensions=[".c", ".h"],
        known_apis=["MsgReg"],
    )
    assert failures == []
    by_api = {s.api: s for s in sug}
    assert "MsgReg" in by_api
    s = by_api["MsgReg"]
    assert s.distinct_handlers == {
        "sess_handle_invite", "sess_handle_bye", "sess_handle_refer"}
    assert s.call_sites == 3


def test_suggest_register_apis_threshold_filters(fixture_dir):
    """阈值高于实际 handler 数时，已知 API 仍列出（复核用），未知候选被过滤。"""
    from navmap import clangenv
    from navmap.suggest import suggest_register_apis

    clangenv.setup()
    sug, _ = suggest_register_apis(
        fixture_dir,
        fixture_dir / "compile_commands.json",
        threshold=99,
        extensions=[".c", ".h"],
        known_apis=["MsgReg"],
    )
    assert [s.api for s in sug] == ["MsgReg"]


def test_suggest_global_vars(fixture_dir):
    """g_sysConfig 引用计数 Top 命中；handlers.h 的 extern 函数不应混入。"""
    from navmap.suggest import suggest_global_vars

    sug = suggest_global_vars(fixture_dir, top=5, extensions=[".c", ".h"])
    by_name = {s.name: s for s in sug}
    assert "g_sysConfig" in by_name
    v = by_name["g_sysConfig"]
    # gvars.c 定义+2 写、gvars_user.c extern+1 读 1 写
    assert v.refs >= 4
    assert v.decl_file.endswith("gvars_user.c")
    assert "MsgReg" not in by_name  # extern 函数声明被排除
