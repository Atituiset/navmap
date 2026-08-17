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
            self._args[os.path.realpath(fpath)] = self._clean_args(args, fpath)
            self._files.append(fpath)

    @staticmethod
    def _clean_args(args: list[str], src: str) -> list[str]:
        """剥掉编译器名、-c/-o 与输入文件本身，得到可复用的解析参数。"""
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
            # 输入文件本身（相对或绝对路径拼写都可能）
            if a == src or (os.path.exists(a) and os.path.realpath(a) == os.path.realpath(src)):
                continue
            out.append(a)
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
                return args + ["-I", hdr_dir], src
        return None
