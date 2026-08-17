"""compile_commands.json 加载与 TU 参数查询（设计文档 §0.2 / §3[2]）。

compile_commands.json 是参数字典，不是扫描队列：它只回答
"这个文件该用什么参数解析"，不被逐 TU 遍历。

头文件没有自己的 TU 条目：借包含它的 .c TU 的参数（§5.2 末尾）。
"""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path


class CompilationDB:
    def __init__(self, compdb_path: str | Path):
        self.path = Path(compdb_path)
        with open(self.path) as f:
            raw = json.load(f)
        # file（绝对化）→ 参数列表
        self._args: dict[str, list[str]] = {}
        self._files: list[str] = []
        for ent in raw:
            directory = ent.get("directory", str(self.path.parent))
            fpath = ent["file"]
            if not os.path.isabs(fpath):
                fpath = os.path.normpath(os.path.join(directory, fpath))
            if "arguments" in ent:
                args = list(ent["arguments"])
            else:
                args = shlex.split(ent.get("command", ""))
            self._args[os.path.realpath(fpath)] = self._clean_args(args, fpath, directory)
            self._files.append(fpath)

    #: 值是路径、需要按 compdb directory 绝对化的 flag（分开写法）
    _PATH_FLAGS_SEP = ("-I", "-isystem", "-iquote", "-idirafter", "-include", "-imacros")
    #: 同上，合并写法（-Ifoo）
    _PATH_FLAGS_JOINED = ("-I", "-isystem", "-iquote", "-idirafter")

    @classmethod
    def _clean_args(cls, args: list[str], src: str, directory: str) -> list[str]:
        """剥掉编译器名、-c/-o 与输入文件本身，并把相对路径参数按
        compdb 条目的 directory 绝对化（libclang 解析时不保证 cwd）。"""
        src_real = os.path.realpath(src)
        out: list[str] = []
        skip_next = False
        for i, a in enumerate(args):
            if i == 0:  # 编译器名
                continue
            if skip_next:
                skip_next = False
                continue
            if a == "-c":
                continue
            if a == "-o":
                skip_next = True
                continue
            if a.startswith("-o") and len(a) > 2:
                continue
            # 输入文件本身（相对或绝对路径拼写都可能；相对路径相对 directory）
            cand = a if os.path.isabs(a) else os.path.join(directory, a)
            if not a.startswith("-") and os.path.realpath(cand) == src_real:
                continue
            # 路径类 flag 绝对化
            if a in cls._PATH_FLAGS_SEP:
                skip_next = False  # 下面手动消费下一个参数
                # 此处不 skip_next：下一个参数可能本身是路径，需要改写而非丢弃
                out.append(a)
                continue
            if i > 0 and args[i - 1] in cls._PATH_FLAGS_SEP and not os.path.isabs(a):
                out.append(os.path.normpath(os.path.join(directory, a)))
                continue
            joined = next((f for f in cls._PATH_FLAGS_JOINED
                           if a.startswith(f) and len(a) > len(f)), None)
            if joined and not os.path.isabs(a[len(joined):]):
                out.append(joined + os.path.normpath(
                    os.path.join(directory, a[len(joined):])))
                continue
            out.append(a)
        # navmap 只解析不编译：-Werror(-Werror=*) 会把告警抬成 error，
        # 触发 -ferror-limit 熔断成 fatal；尾部追加 -Wno-error 压制
        out.append("-Wno-error")
        return out

    def lookup(self, file: str | Path) -> list[str] | None:
        """候选文件的编译参数；没有条目（头文件）返回 None，走 borrow_args。"""
        return self._args.get(os.path.realpath(str(file)))

    _include_re_cache: dict[str, re.Pattern] = {}

    def _include_re(self, header_name: str) -> re.Pattern:
        pat = self._include_re_cache.get(header_name)
        if pat is None:
            pat = re.compile(
                r'^\s*#\s*include\s*[<"][^">]*' + re.escape(header_name) + r'[">]',
                re.MULTILINE,
            )
            self._include_re_cache[header_name] = pat
        return pat

    def borrow_args_for_header(self, header: str | Path) -> tuple[list[str], str] | None:
        """头文件候选：找一个文本上 #include 它的 .c TU，借其参数。

        同一头文件被多个变体 TU 包含时宏展开可能不同——取第一个命中的
        （主变体），产物里标注所用 TU 来源（返回值第二项）。
        """
        name = os.path.basename(str(header))
        pat = self._include_re(name)
        for src in self._files:
            try:
                text = Path(src).read_text(errors="replace")
            except OSError:
                continue
            if pat.search(text):
                args = self._args[os.path.realpath(src)]
                hdr_dir = os.path.dirname(os.path.abspath(str(header)))
                # .h 默认按 C 头解析；借 C++ TU 参数时必须显式指定语言，
                # 否则 -std=c++17 之类参数在 C 前端下直接判死（NULL TU）
                lang = "c++-header" if src.endswith((".cc", ".cpp", ".cxx")) else "c-header"
                return args + ["-I", hdr_dir, "-x", lang], src
        return None
