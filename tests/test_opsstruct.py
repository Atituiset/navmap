"""opsstruct.py 提取测试：单 ops-struct 分发形态（curl Curl_protocol 同构）。"""

import pytest


@pytest.fixture(scope="module")
def ops_tables(fixture_dir):
    from navmap import clangenv
    from navmap.compdb import CompilationDB
    from navmap.extract.opsstruct import OpsStructExtractor

    clangenv.setup()
    ex = OpsStructExtractor(
        CompilationDB(fixture_dir / "compile_commands.json"),
        src_root=fixture_dir,
    )
    tables, failures = ex.extract_files([str(fixture_dir / "ops_struct.c")])
    return tables, failures


def test_no_failures(ops_tables):
    assert ops_tables[1] == []


def test_both_ops_structs_found(ops_tables):
    tables, _ = ops_tables
    names = {t.name for t in tables}
    assert {"g_opsProtoFull", "g_opsProtoDesignated"} <= names


def test_positional_init(ops_tables):
    """按位初始化：msg_id=成员名，ZERO_NULL 占位被跳过。"""
    tables, _ = ops_tables
    t = next(t for t in tables if t.name == "g_opsProtoFull")
    by_member = {e.msg_id: e for e in t.entries}
    assert set(by_member) == {"setup_connection", "do_it",
                              "connect_it", "disconnect"}
    assert by_member["do_it"].handler == "ops_do_full"
    assert by_member["setup_connection"].handler == "ops_setup_full"
    for e in t.entries:
        assert e.handler_usr
        assert e.handler_loc and "ops_struct.c:" in e.handler_loc


def test_designated_init(ops_tables):
    """指定初始化器：按成员名配对（乱序安全）。"""
    tables, _ = ops_tables
    t = next(t for t in tables if t.name == "g_opsProtoDesignated")
    by_member = {e.msg_id: e for e in t.entries}
    assert set(by_member) == {"do_it", "connect_it"}
    assert by_member["do_it"].handler == "ops_do_designated"
    assert by_member["connect_it"].handler == "ops_connect_designated"


def test_array_tables_not_claimed(ops_tables):
    """数组形态（dispatch 的领域）不被 ops-struct 提取器认领。"""
    from navmap import clangenv
    from navmap.compdb import CompilationDB
    from navmap.extract.opsstruct import OpsStructExtractor

    clangenv.setup()
    ex = OpsStructExtractor(
        CompilationDB(fixture_dir / "compile_commands.json"),
        src_root=fixture_dir,
    )
    tables, _ = ex.extract_files([str(fixture_dir / "disp.c")])
    assert tables == []
