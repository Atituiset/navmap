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
