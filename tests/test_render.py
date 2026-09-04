"""render.py 测试：JSON 与 markdown 同源一致性。"""

from navmap.render import render_dispatch_md


def test_render_contains_baseline_and_tables(extracted):
    art, _ = extracted
    md = render_dispatch_md(art)
    assert "testbaseline" in md
    for t in art.tables:
        assert f"`{t.name}`" in md
        for e in t.entries:
            assert f"`{e.msg_id}`" in md
            assert f"`{e.handler}`" in md


def test_render_ifdef_cond(extracted):
    art, _ = extracted
    md = render_dispatch_md(art)
    assert "`FEATURE_IMS`" in md


def test_render_msg_id_value(extracted):
    """msg_id_value 展开值在 markdown 中可见（调试/人审用）。"""
    art, _ = extracted
    md = render_dispatch_md(art)
    # g_msgTable: MSG_1001 → 0x3e9
    assert "`0x3e9`" in md


def test_render_usr_column(extracted):
    """handler_usr 渲染进表列（USR 对齐核查用）。"""
    art, _ = extracted
    md = render_dispatch_md(art)
    assert "c:@F@sess_handle_invite" in md
