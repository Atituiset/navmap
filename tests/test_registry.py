"""registry.py 提取测试（设计文档 §5.3）。"""

import pytest


@pytest.fixture(scope="module")
def reg_extracted(fixture_dir):
    from navmap import clangenv
    from navmap.compdb import CompilationDB
    from navmap.extract.registry import RegistryExtractor

    clangenv.setup()
    ex = RegistryExtractor(
        CompilationDB(fixture_dir / "compile_commands.json"),
        src_root=fixture_dir,
        register_apis=["MsgReg"],
    )
    tables, failures = ex.extract_files([str(fixture_dir / "reg.c")])
    return tables, failures


def test_no_failures(reg_extracted):
    assert reg_extracted[1] == []


def test_virtual_table(reg_extracted):
    tables, _ = reg_extracted
    assert len(tables) == 1
    t = tables[0]
    assert t.name == "registry:MsgReg"
    assert t.file == "reg.c"
    assert t.source_hash.startswith("sha256:")
    assert len(t.entries) == 3


def test_normal_entry(reg_extracted):
    t = reg_extracted[0][0]
    e = t.entries[0]
    assert e.msg_id == "MSG_1001"
    assert e.handler == "sess_handle_invite"
    assert e.cond is None
    assert e.handler_usr
    assert e.handler_loc and "handlers.h:" in e.handler_loc
    assert e.source == "ast"


def test_ifdef_entry_cond(reg_extracted):
    t = reg_extracted[0][0]
    e = t.entries[1]
    assert e.msg_id == "MSG_1002"
    assert e.handler == "sess_handle_bye"
    assert e.cond == "FEATURE_IMS"


def test_cast_entry(reg_extracted):
    """(msg_handler_t)fn 强转形式：handler 剥 cast 后正确解析。"""
    t = reg_extracted[0][0]
    e = t.entries[2]
    assert e.msg_id == "MSG_1003"
    assert e.handler == "sess_handle_refer"
    assert e.handler_usr
    assert e.cond is None
