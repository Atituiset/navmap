"""libclang 运行时定位与绑定加载。

版本纪律：绑定（pip `clang` 包，即 llvm 仓库 clang/bindings/python/clang/cindex.py）
与 libclang.so 必须与生产 clangd 严格同版本（当前 20.1.0）。

加载顺序：
1. 显式参数（config [libclang].path）
2. 环境变量 NAVMAP_LIBCLANG
3. 项目 vendor/ 目录自动探测（vendor/*/lib/libclang.so*）
"""

from __future__ import annotations

import ctypes
import glob
import os
from pathlib import Path

EXPECTED_VERSION = "20.1.0"

_configured = False
_configured_path = ""


class _CXStringRaw(ctypes.Structure):
    _fields_ = [("data", ctypes.c_void_p), ("flags", ctypes.c_uint)]


def _vendor_candidates() -> list[str]:
    root = Path(__file__).resolve().parent.parent / "vendor"
    patterns = [
        str(root / "*" / "lib" / "libclang.so*"),
        str(root / "*" / "lib" / "libclang-*.so*"),
        str(root / "libclang.so*"),
    ]
    out: list[str] = []
    for pat in patterns:
        out.extend(sorted(glob.glob(pat)))
    return out


def setup(libclang_path: str | None = None) -> str:
    """定位并加载 libclang，返回实际使用的库路径。进程内幂等。"""
    global _configured, _configured_path
    import clang.cindex as cindex

    if _configured or cindex.Config.loaded:
        _configured = True
        return _configured_path

    candidates: list[str] = []
    if libclang_path:
        candidates.append(libclang_path)
    env_path = os.environ.get("NAVMAP_LIBCLANG")
    if env_path:
        candidates.append(env_path)
    candidates.extend(_vendor_candidates())

    for path in candidates:
        if path and os.path.isfile(path):
            version = libclang_version(path)
            if EXPECTED_VERSION not in version:
                raise RuntimeError(
                    f"libclang 版本不符：期望 {EXPECTED_VERSION}，实际 {version}（{path}）。"
                    "绑定与库必须与生产 clangd 严格同版本，否则产物不可信。"
                )
            cindex.Config.set_library_file(path)
            _configured = True
            _configured_path = path
            return path

    raise RuntimeError(
        f"找不到 libclang {EXPECTED_VERSION}。请设置 NAVMAP_LIBCLANG=/path/to/libclang.so，"
        "或将 LLVM 20.1.0 的 libclang 放入 vendor/（见 README）。"
        f"已尝试: {candidates or '(无候选)'}"
    )


def libclang_version(path: str) -> str:
    """独立 ctypes 句柄读取 libclang 版本字符串（不经绑定，避免生命周期纠缠）。"""
    lib = ctypes.CDLL(path)
    lib.clang_getClangVersion.restype = _CXStringRaw
    lib.clang_getCString.restype = ctypes.c_char_p
    lib.clang_getCString.argtypes = [_CXStringRaw]
    lib.clang_disposeString.argtypes = [_CXStringRaw]
    s = lib.clang_getClangVersion()
    try:
        return (lib.clang_getCString(s) or b"").decode()
    finally:
        lib.clang_disposeString(s)
