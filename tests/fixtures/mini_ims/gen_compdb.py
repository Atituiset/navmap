#!/usr/bin/env python3
"""生成 mini_ims 的 compile_commands.json（绝对路径随 checkout 位置变化）。"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))

sources = ["disp.c", "disp2.c", "xmacro.c", "handlers.c", "oam_user.c", "sm.c",
           "reg.c", "gvars.c", "gvars_user.c", "nested.c"]
entries = [
    {
        "directory": HERE,
        "file": os.path.join(HERE, src),
        "arguments": [
            "clang",
            "-std=c11",
            "-DFEATURE_IMS",
            "-I",
            os.path.join(HERE, "include"),
            "-c",
            os.path.join(HERE, src),
            "-o",
            src.replace(".c", ".o"),
        ],
    }
    for src in sources
]

with open(os.path.join(HERE, "compile_commands.json"), "w") as f:
    json.dump(entries, f, indent=2)
print("written:", os.path.join(HERE, "compile_commands.json"))
