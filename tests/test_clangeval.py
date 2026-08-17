"""clangeval / 嵌套初始化回归测试：INIT_LIST_EXPR 不得触发 segfault。"""

import pytest


@pytest.fixture(scope="module")
def nested_tables(fixture_dir):
    from navmap import clangenv
    from navmap.compdb import CompilationDB
    from navmap.extract.dispatch import DispatchExtractor

    clangenv.setup()
    ex = DispatchExtractor(CompilationDB(fixture_dir / "compile_commands.json"),
                           src_root=fixture_dir)
    tables, failures = ex.extract_files([str(fixture_dir / "nested.c")])
    return tables, failures


def test_nested_init_no_crash(nested_tables):
    """表项含嵌套 {1, 2} 聚合：提取正常完成（进程存活即回归通过）。"""
    tables, failures = nested_tables
    assert failures == []
    names = {t.name for t in tables}
    assert "g_nestedTable" in names


def test_nested_init_eval_none(nested_tables):
    """聚合初始化字段不求值：msg_id_value 为 None 而不是崩溃。"""
    tables, _ = nested_tables
    t = next(t for t in tables if t.name == "g_nestedTable")
    assert len(t.entries) == 1
    assert t.entries[0].msg_id_value is None
    assert t.entries[0].handler == "sess_handle_invite"
