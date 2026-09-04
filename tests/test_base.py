"""base.py cond_map 条件编译回溯测试：#elif/#else 排斥语义。"""

from navmap.extract.base import cond_map


def _conds(src: str) -> dict[int, str]:
    return cond_map(src.splitlines())


def test_ifdef_plain():
    c = _conds("#ifdef FEATURE_IMS\nint a;\n#endif\n")
    assert c == {2: "FEATURE_IMS"}


def test_ifndef_negation():
    c = _conds("#ifndef FEATURE_IMS\nint a;\n#endif\n")
    assert c == {2: "!FEATURE_IMS"}


def test_if_expr():
    c = _conds("#if CONFIG_VAL > 2\nint a;\n#endif\n")
    assert c == {2: "CONFIG_VAL > 2"}


def test_else_of_single_ifdef():
    c = _conds("#ifdef FEATURE_IMS\nint a;\n#else\nint b;\n#endif\n")
    assert c == {2: "FEATURE_IMS", 4: "!(FEATURE_IMS)"}


def test_elif_chain_exclusion_semantics():
    """#elif 分支条件 = 前面分支都不成立 && 本分支成立。"""
    c = _conds(
        "#ifdef A\n"
        "int a;\n"
        "#elif defined(B)\n"
        "int b;\n"
        "#elif defined(C)\n"
        "int c;\n"
        "#else\n"
        "int d;\n"
        "#endif\n"
    )
    assert c[2] == "A"
    assert c[4] == "!(A) && defined(B)"
    assert c[6] == "!(A) && !(defined(B)) && defined(C)"
    assert c[8] == "!(A) && !(defined(B)) && !(defined(C))"


def test_nested_ifdef():
    c = _conds("#ifdef A\n#ifdef B\nint a;\n#endif\nint b;\n#endif\n")
    assert c[3] == "A && B"
    assert c[5] == "A"


def test_no_cond_outside_ifdef():
    c = _conds("int a;\n")
    assert c == {}
