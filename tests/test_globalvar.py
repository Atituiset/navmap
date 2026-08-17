"""globalvar.py 提取测试（设计文档 §5.5）。"""

import pytest


@pytest.fixture(scope="module")
def gv_extracted(fixture_dir):
    from navmap import clangenv
    from navmap.compdb import CompilationDB
    from navmap.extract.globalvar import GlobalvarExtractor

    clangenv.setup()
    ex = GlobalvarExtractor(
        CompilationDB(fixture_dir / "compile_commands.json"),
        src_root=fixture_dir,
        variables=["g_sysConfig"],
        modules={"ims": ["gvars.c"], "oam": ["gvars_user.c"]},
    )
    return ex, ex.extract()


def test_find_ref_files(gv_extracted):
    ex, _ = gv_extracted
    assert ex.find_ref_files() == {
        "g_sysConfig": ["gvars.c", "gvars_user.c"],
    }


def test_no_failures(gv_extracted):
    _, (_, failures) = gv_extracted
    assert failures == []


def test_def_loc(gv_extracted):
    _, (vars_, _) = gv_extracted
    assert len(vars_) == 1
    v = vars_[0]
    assert v.variable == "g_sysConfig"
    assert v.def_loc is not None
    file, _, line = v.def_loc.partition(":")
    assert file == "gvars.c"
    # 定义行确实写着 int g_sysConfig = 0;
    src = (gv_extracted[0].src_root / file).read_text().splitlines()
    assert "int g_sysConfig = 0;" in src[int(line) - 1]


def test_writers(gv_extracted):
    v = gv_extracted[1][0][0]
    by_func = {w.func: w for w in v.writers}
    assert set(by_func) == {"cfg_apply", "cfg_bump", "cfg_boost"}
    assert by_func["cfg_apply"].kind == "assign"
    assert by_func["cfg_apply"].loc.startswith("gvars.c:")
    assert by_func["cfg_bump"].kind == "incdec"
    assert by_func["cfg_boost"].kind == "compound"
    assert by_func["cfg_boost"].loc.startswith("gvars_user.c:")


def test_readers_by_module(gv_extracted):
    v = gv_extracted[1][0][0]
    # 唯一读者是 gvars_user.c 里的 cfg_is_set（return g_sysConfig > 0）
    assert v.readers_by_module == {"oam": 1}


def test_total_refs_and_ref_files(gv_extracted):
    v = gv_extracted[1][0][0]
    # assign + incdec + compound + read = 4 处引用（extern 声明与定义不算）
    assert v.total_refs == 4
    assert v.ref_files == ["gvars.c", "gvars_user.c"]


def test_modules_none_defaults(fixture_dir):
    """modules 未配置时读者全部归 default；写者清单不受模块配置影响。"""
    from navmap import clangenv
    from navmap.compdb import CompilationDB
    from navmap.extract.globalvar import GlobalvarExtractor

    clangenv.setup()
    ex = GlobalvarExtractor(
        CompilationDB(fixture_dir / "compile_commands.json"),
        src_root=fixture_dir,
        variables=["g_sysConfig"],
    )
    vars_, failures = ex.extract()
    assert failures == []
    v = vars_[0]
    # 无 modules 配置时读者归 default
    assert v.readers_by_module == {"default": 1}
    assert all(w.kind != "read" for w in v.writers)
