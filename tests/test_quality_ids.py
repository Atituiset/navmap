"""quality.py 消息枚举提取测试：#define 常量表达式与函数宏排除。"""

from navmap.quality import extract_msg_ids


def test_define_numeric():
    ids = extract_msg_ids_str("#define MSG_1001 0x3E9\n")
    assert "MSG_1001" in ids


def test_define_const_expr():
    """宏运算右值（(1<<2)、MSG_BASE+5）不得漏。"""
    ids = extract_msg_ids_str(
        "#define MSG_BASE 0x100\n"
        "#define MSG_A (1 << 2)\n"
        "#define MSG_B MSG_BASE + 5\n"
        "#define MSG_C MSG_A\n"
    )
    assert {"MSG_BASE", "MSG_A", "MSG_B", "MSG_C"} <= ids


def test_define_function_macro_excluded():
    """函数宏（#define FOO(x) ...）不是消息 ID。"""
    ids = extract_msg_ids_str(
        "#define MIN(a, b) ((a) < (b) ? (a) : (b))\n"
        "#define MSG_1001 1\n"
    )
    assert "MIN" not in ids
    assert "MSG_1001" in ids


def test_enum_members():
    ids = extract_msg_ids_str(
        "enum msg_id {\n"
        "    MSG_INVITE = 0x1,\n"
        "    MSG_BYE,\n"
        "};\n"
    )
    assert {"MSG_INVITE", "MSG_BYE"} <= ids


def test_missing_file_returns_empty(tmp_path):
    assert extract_msg_ids(tmp_path / "nope.h") == set()


def extract_msg_ids_str(text: str):
    import pathlib
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".h", delete=False) as f:
        f.write(text)
        path = f.name
    try:
        return extract_msg_ids(pathlib.Path(path))
    finally:
        import os
        os.unlink(path)
