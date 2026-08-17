# navmap — C/C++ 导航图提取器（libclang）

从大型 C/C++ 代码库自动提取**消息分发表**（M3+：状态机表、全局变量读写清单、
全局状态布局），产出 JSON + markdown，供 code review 注入 prompt 或经 MCP 查询。

实施设计：[`docs/navmap-libclang-extractor-design.md`](docs/navmap-libclang-extractor-design.md)（本仓库按其 §8 里程碑 M1 范围交付，后续演进以本仓库为准）。

## 版本纪律（重要）

**libclang.so 与 Python 绑定必须与生产 clangd 严格同版本（20.1.0）**，否则
AST 形态差异会直接污染产物。本项目：

- Python 绑定：pip `clang==20.1.0`（即 llvm 仓库 `clang/bindings/python/clang/cindex.py` 的打包件）；
- libclang 二进制：`vendor/libclang-20.1.0/lib/libclang.so.13`（conda-forge `libclang13-20.1.0`，
  soname 为 13 属正常——C API soname 自 clang 13 起冻结）。

`navmap` 启动时强制校验版本，不匹配直接报错（`navmap/clangenv.py`）。

### libclang 定位顺序

1. 环境变量 `NAVMAP_LIBCLANG=/path/to/libclang.so`
2. `config/navmap.toml` 的 `[libclang].path`
3. `vendor/*/lib/libclang.so*` 自动探测

生产环境已有 clangd 20.1.0 时，把同版本 `libclang.so` 路径指给 `NAVMAP_LIBCLANG` 即可。
重建 vendor 二进制的命令：

```bash
cd vendor
curl -sSLO 'https://conda.anaconda.org/conda-forge/linux-64/libclang13-20.1.0-default_h9c6a7e4_0.conda'
python3 - <<'EOF'
import zipfile, os, tarfile, zstandard
z = zipfile.ZipFile('libclang13-20.1.0-default_h9c6a7e4_0.conda')
pkg = [n for n in z.namelist() if n.startswith('pkg-')][0]
data = zstandard.ZstdDecompressor().stream_reader(z.open(pkg))
tarfile.open(fileobj=data, mode='r|').extractall('libclang-20.1.0', filter='data')
EOF
```

## 安装与使用

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'

navmap extract --src /path/to/src \
               --compdb compile_commands.json \
               --baseline $(git rev-parse HEAD) \
               --subsystem ims \
               --out .navmap/out/
navmap report --out .navmap/out/
```

产物：`navmap-dispatch-<subsystem>.json`（机器查询）+ 同名 `.md`（prompt 注入），同源生成。

## 流水线形态（漏斗）

```
全仓源文件 → [1] scan.py 正则粗筛（误报允许，漏报不允许）→ 候选文件清单
          → [2] compdb.py 按文件查 compile_commands.json 取原始编译参数
                （头文件无 TU 条目 → 借包含它的 .c TU 参数，产物标注 TU 来源）
          → [3] extract/dispatch.py libclang 只解析候选文件
          → [5] JSON + markdown（每条带 baseline_commit 与 source_hash）
```

`compile_commands.json` 是**参数字典，不是扫描队列**——逐 TU 全量解析是
`clangd-indexer` 的成本，正是本方案要避开的。

## 表形态支持（M1）

- 普通宏表 `{ MSG_1001, sess_handle_invite }`：msg_id 取源码宏拼写（extent 截取），不取展开值；
- 指定初始化器 `{ .msg_id = ..., .handler = ... }`：按字段类型而非位置配对，乱序安全；
- `#ifdef` 条件表项：文本回溯条件栈，记录 `cond`；
- `(cast)fn` / `&fn` 形式 handler：剥 CStyleCastExpr / UnaryOperator 后取 referenced + USR；
- X-Macro 表：表项 location 指向 `.def` 真实来源文件；实参 token extent 塌缩时
  对表项源码文本正则分词兜底取 msg_id。

## 已知限制（留给 M2+）

- `msg_id_value`（展开值）恒为 null：官方 cindex.py 未打包 `clang_Cursor_Evaluate`
  绑定，M2 用 ctypes 直调补；
- registry（注册式分发）/ statemachine / globalvar 提取器未实现（M3/M4）；
- 增量 merge（incr.py）、quality.py 三指标、clangd 索引一致性校验未实现（M2/M5）；
- switch 式手写状态机不做（按设计）。

## 测试

```bash
.venv/bin/python -m pytest tests/ -q
```

fixture `tests/fixtures/mini_ims/` 覆盖上述五种表形态；`compile_commands.json`
由 `gen_compdb.py` 按本机路径生成（conftest 自动调用）。
