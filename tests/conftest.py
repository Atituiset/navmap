import json
import subprocess
import sys
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "mini_ims"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def fixture_dir() -> Path:
    """mini_ims fixture 目录，确保 compile_commands.json 已按本机路径生成。"""
    subprocess.run(
        [sys.executable, str(FIXTURE_DIR / "gen_compdb.py")],
        check=True,
        capture_output=True,
    )
    assert (FIXTURE_DIR / "compile_commands.json").is_file()
    return FIXTURE_DIR


@pytest.fixture(scope="session")
def extracted(fixture_dir):
    """对整个 fixture 跑一次完整 extract 流程，返回 (artifact, extractor)。"""
    from navmap import clangenv
    from navmap.compdb import CompilationDB
    from navmap.extract.dispatch import DispatchExtractor
    from navmap.model import DispatchArtifact
    from navmap.scan import scan

    clangenv.setup()
    candidates = scan(
        fixture_dir,
        name_roots=["table", "disp", "map", "hdlr", "state", "trans"],
        register_apis=[],
        extensions=[".c", ".h", ".def"],
    )
    compdb = CompilationDB(fixture_dir / "compile_commands.json")
    extractor = DispatchExtractor(compdb, src_root=fixture_dir)
    tables, failures = extractor.extract_files([c.file for c in candidates])
    art = DispatchArtifact(
        baseline_commit="testbaseline", subsystem="ims", tables=tables,
        parse_failures=failures,
    )
    return art, extractor
