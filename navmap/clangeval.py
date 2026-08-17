"""ctypes 直调 clang_Cursor_Evaluate（官方 cindex.py 未打包该绑定）。

设计文档 §4 msg_id_value / M2 待办：给 dispatch 表项补展开值（供调试）。
只取整型结果（CXEval_Int = 1），其余类别返回 None。
"""

from __future__ import annotations

import ctypes

_ready = False

_CXEVAL_INT = 1


def _ensure() -> object:
    global _ready
    from clang.cindex import Cursor, conf

    lib = conf.lib  # 触发加载；须 clangenv.setup() 先行
    if not _ready:
        lib.clang_Cursor_Evaluate.argtypes = [Cursor]
        lib.clang_Cursor_Evaluate.restype = ctypes.c_void_p
        lib.clang_EvalResult_getKind.argtypes = [ctypes.c_void_p]
        lib.clang_EvalResult_getKind.restype = ctypes.c_int
        lib.clang_EvalResult_getAsLongLong.argtypes = [ctypes.c_void_p]
        lib.clang_EvalResult_getAsLongLong.restype = ctypes.c_longlong
        lib.clang_EvalResult_dispose.argtypes = [ctypes.c_void_p]
        lib.clang_EvalResult_dispose.restype = None
        _ready = True
    return lib


def eval_int(cursor) -> str | None:
    """常量整数表达式求值，返回十六进制拼写（如 0x3ec）；非常量返回 None。

    注意：clang_Cursor_Evaluate 对 INIT_LIST_EXPR 等聚合初始化 cursor
    会直接 segfault（libclang 20.1.0 实测，u-boot cmd/ethsw.c 复现），
    调用前按 kind 白名单过滤。"""
    from clang.cindex import CursorKind

    _SAFE = {
        CursorKind.INTEGER_LITERAL,
        CursorKind.DECL_REF_EXPR,
        CursorKind.BINARY_OPERATOR,
        CursorKind.UNARY_OPERATOR,
        CursorKind.CONDITIONAL_OPERATOR,
        CursorKind.PAREN_EXPR,
        CursorKind.CSTYLE_CAST_EXPR,
        CursorKind.UNEXPOSED_EXPR,
        CursorKind.CALL_EXPR,
        CursorKind.MEMBER_REF_EXPR,
    }
    if cursor.kind not in _SAFE:
        return None
    try:
        lib = _ensure()
        res = lib.clang_Cursor_Evaluate(cursor)
        if not res:
            return None
        try:
            if lib.clang_EvalResult_getKind(res) != _CXEVAL_INT:
                return None
            return hex(lib.clang_EvalResult_getAsLongLong(res))
        finally:
            lib.clang_EvalResult_dispose(res)
    except Exception:  # 求值失败不阻塞提取
        return None
