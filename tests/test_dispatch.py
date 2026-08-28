"""dispatch.py 提取测试：五种表形态的正确性。"""

import pytest


def _tables_by_name(art):
    return {t.name: t for t in art.tables}


def test_no_parse_failures(extracted):
    art, _ = extracted
    assert art.parse_failures == []


def test_all_tables_found(extracted):
    art, _ = extracted
    names = set(_tables_by_name(art))
    assert {"g_msgTable", "g_dispTable", "g_xmsgTable", "g_oamTable"} <= names


def test_normal_table_entries(extracted):
    """普通宏表：msg_id 必须是源码宏拼写，handler 有 USR 与 file:line。"""
    art, _ = extracted
    t = _tables_by_name(art)["g_msgTable"]
    assert t.file == "disp.c"
    assert t.source_hash.startswith("sha256:")
    by_msg = {e.msg_id: e for e in t.entries}
    assert set(by_msg) == {"MSG_1001", "MSG_1002", "MSG_1003"}

    e = by_msg["MSG_1001"]
    assert e.handler == "sess_handle_invite"
    assert e.handler_usr  # USR 存在
    assert e.handler_loc and "handlers.h:" in e.handler_loc
    assert e.cond is None
    assert e.source == "ast"


def test_ifdef_entry_cond(extracted):
    """#ifdef 条件表项：cond 记录源码拼写的条件宏。"""
    art, _ = extracted
    by_msg = {e.msg_id: e for e in _tables_by_name(art)["g_msgTable"].entries}
    assert by_msg["MSG_1002"].cond == "FEATURE_IMS"


def test_cast_handler(extracted):
    """(msg_handler_t)fn 强转形式：剥掉 CStyleCastExpr 后取到 handler。"""
    art, _ = extracted
    by_msg = {e.msg_id: e for e in _tables_by_name(art)["g_msgTable"].entries}
    assert by_msg["MSG_1003"].handler == "sess_handle_refer"


def test_designated_init(extracted):
    """指定初始化器：字段乱序也能正确配对 msg_id 与 handler。"""
    art, _ = extracted
    t = _tables_by_name(art)["g_dispTable"]
    pairs = {(e.msg_id, e.handler) for e in t.entries}
    assert ("MSG_1004", "sess_handle_notify") in pairs  # &fn 取地址形式
    assert ("MSG_1001", "sess_handle_bye") in pairs     # 乱序指定初始化


def test_xmacro_locations(extracted):
    """X-Macro 表：展开元素的拼写来自 msg.def（msg_id 取宏名）。"""
    art, _ = extracted
    t = _tables_by_name(art)["g_xmsgTable"]
    assert {e.msg_id for e in t.entries} == {"MSG_1001", "MSG_1004"}
    assert {e.handler for e in t.entries} == {"sess_handle_invite", "sess_handle_notify"}


def test_header_table_borrowed_args(extracted):
    """头文件中的表：借包含它的 .c TU 参数解析，并标注 TU 来源。"""
    art, extractor = extracted
    t = _tables_by_name(art)["g_oamTable"]
    assert t.file == "include/oam_table.h"
    assert [e.handler for e in t.entries] == ["oam_handle_stats"]
    # 借参标注
    assert any(k.endswith("oam_table.h") for k in extractor.args_source)
    assert any(v.endswith("oam_user.c") for v in extractor.args_source.values())


def test_artifact_json_roundtrip(extracted):
    from navmap.model import DispatchArtifact

    art, _ = extracted
    art2 = DispatchArtifact.from_dict(__import__("json").loads(art.to_json()))
    assert art2.baseline_commit == "testbaseline"
    assert len(art2.tables) == len(art.tables)
    names = {t.name for t in art2.tables}
    assert "g_msgTable" in names


def test_bare_fnptr_array(extracted):
    """裸函数指针数组（无结构体包装）：提取为分发表，msg_id = 数组下标。"""
    art, _ = extracted
    t = _tables_by_name(art)["FP_HANDLER_TBL"]
    assert t.file == "bare_fnptr.c"
    by_idx = {e.msg_id: e for e in t.entries}
    assert set(by_idx) == {"0", "1", "2", "3"}
    assert by_idx["0"].handler == "alpha_handler"
    assert by_idx["1"].handler == "beta_handler"
    assert by_idx["0"].handler_usr
    assert "bare_fnptr.c:" in by_idx["0"].handler_loc
    assert by_idx["0"].msg_id_value == "0"
