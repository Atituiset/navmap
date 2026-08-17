"""statemachine.py 提取测试（设计文档 §5.4）。"""

import pytest


@pytest.fixture(scope="module")
def sm_extracted(fixture_dir):
    from navmap import clangenv
    from navmap.compdb import CompilationDB
    from navmap.extract.statemachine import StatemachineExtractor

    clangenv.setup()
    ex = StatemachineExtractor(
        CompilationDB(fixture_dir / "compile_commands.json"),
        src_root=fixture_dir,
        state_fields=["state", "cur_state", "from"],
        event_fields=["event", "evt", "msg_id", "msg"],
        next_state_fields=["next_state", "next", "to"],
    )
    tables, failures = ex.extract_files([str(fixture_dir / "sm.c")])
    return tables, failures


def test_no_failures(sm_extracted):
    assert sm_extracted[1] == []


def test_table_found(sm_extracted):
    tables, _ = sm_extracted
    assert len(tables) == 1
    t = tables[0]
    assert t.name == "g_smTable"
    assert t.file == "sm.c"
    assert len(t.entries) == 3


def test_normal_row(sm_extracted):
    t = sm_extracted[0][0]
    e = t.entries[0]
    assert (e.state, e.event, e.handler, e.next_state) == (
        "ST_IDLE", "EV_INVITE", "sess_handle_invite", "ST_RING")
    assert e.handler_usr
    assert e.handler_loc and "handlers.h:" in e.handler_loc
    assert e.cond is None


def test_ifdef_row_cond(sm_extracted):
    t = sm_extracted[0][0]
    e = t.entries[1]
    assert e.cond == "FEATURE_IMS"
    assert e.handler == "sess_handle_bye"


def test_designated_init_pure_transition(sm_extracted):
    """指定初始化器乱序 + 无 handler 纯迁移行：字段按名配对。"""
    t = sm_extracted[0][0]
    e = t.entries[2]
    assert (e.state, e.event, e.next_state) == ("ST_TALK", "EV_BYE", "ST_IDLE")
    assert e.handler is None


def test_dispatch_table_not_claimed(fixture_dir):
    """分发表（无 state/event 字段）不应被状态机提取器认领。"""
    from navmap import clangenv
    from navmap.compdb import CompilationDB
    from navmap.extract.statemachine import StatemachineExtractor

    clangenv.setup()
    ex = StatemachineExtractor(
        CompilationDB(fixture_dir / "compile_commands.json"),
        src_root=fixture_dir,
        state_fields=["state", "cur_state", "from"],
        event_fields=["event", "evt", "msg_id", "msg"],
        next_state_fields=["next_state", "next", "to"],
    )
    tables, _ = ex.extract_files([str(fixture_dir / "disp.c")])
    assert tables == []
