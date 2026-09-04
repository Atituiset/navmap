# navmap — C/C++ 导航图提取器（libclang）

从大型 C/C++ 代码库自动提取四类导航图——**消息分发表、注册式分发点、
状态机表、全局变量读写清单（含布局）**，产出 JSON + markdown，供 code review
注入 prompt 或经 MCP 查询。

实施设计：[`docs/navmap-libclang-extractor-design.md`](docs/navmap-libclang-extractor-design.md)；
CI 集成（nightly 增量）：[`docs/ci-integration.md`](docs/ci-integration.md)。

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

# 全量提取（首次 / 基线不可达时）
navmap extract --src /path/to/src \
               --compdb compile_commands.json \
               --baseline $(git -C /path/to/src rev-parse HEAD) \
               --subsystem ims \
               --out .navmap/out/

# 增量刷新（源码仓变更后；git diff ∩ 候选，只重提取受影响文件并 merge）
navmap refresh --src /path/to/src \
               --compdb compile_commands.json \
               --subsystem ims \
               --out .navmap/out/

# 产物质检报告（ERROR/WARN/INFO 分级；--src 开启孤儿 handler 与覆盖率检查）
navmap report --out .navmap/out/ --src /path/to/src

# 名单扩充候选（人审后入配置，不自动改配置；--out 归档 markdown 供人审）
navmap suggest-apis  --src /path/to/src --compdb compile_commands.json --out suggest-apis.md
navmap suggest-vars  --src /path/to/src --top 20 --out suggest-vars.md
```

产物（同源生成，JSON 机器查询 + 同名 `.md` prompt 注入）：

- `navmap-dispatch-<subsystem>.json/md`：静态分发表 + 注册式虚拟表
  （`registry:<ApiName>`）；
- `navmap-statemachine-<subsystem>.json/md`：状态机表（`{state, event,
  handler, next_state}` 四元组）；
- `navmap-globalvar-<subsystem>.json/md`：全局变量读写清单 + 布局（配置
  `[globalvar] variables` 后产出）；
- `navmap-candidates.json`：粗筛候选清单，refresh 的失效判断依据。

### refresh 的受影响集合（设计文档 §6 v1）

`git diff <baseline>..HEAD` 得变更文件后，按五条规则圈定重提取范围，
一条都不命中则只 bump `baseline_commit`、零重解析：

1. 变更文件本身是候选 → 重提取；
2. 变更/删除的头文件被某候选直接 `#include` → 该候选重提取；
3. 变更文件是某表项 `handler_loc` 所在文件（行号偏移）→ 该表所在文件重提取；
4. 原非候选的变更文件现命中粗筛 → 新候选，重提取；
5. 被删除的候选 → 直接摘表，零解析。

注意：`navmap-candidates.json` 每次 extract/refresh 覆盖，多子系统共用
一个 `--out` 目录时以最后一次为准。

### 配置（config/navmap.toml）

- `[scan]`：粗筛词根 `name_roots`、注册 API 种子 `register_apis`（registry
  提取器的 API 名单，配了才启用）、文件扩展名；
- `[statemachine]`：状态机字段映射 `state_fields`/`event_fields`/
  `next_state_fields`（各团队命名不同，按子系统配）；
- `[globalvar] variables`：关键全局变量名单（配了才启用 globalvar 提取）；
- `[quality] msgid_headers`：消息枚举来源头文件（report 覆盖率检查用）；
- `[subsystems]`：子系统名 → 路径前缀，globalvar 读者按此聚合模块；
- `[extract] extra_args`：追加给每次 libclang 解析的额外参数——toolchain
  头路径逃生口（同 clangd `--query-driver` 思路）。例：vendored conda
  libclang 不带 resource dir 时，`extra_args = ["-isystem", "/usr/lib/gcc/x86_64-linux-gnu/13/include"]`。
- `--config` 可指定替代配置（整体替换，不是叠加，建议拷贝默认配置再改）。

## 流水线形态（漏斗）

```
全仓源文件 → [1] scan.py 正则粗筛（误报允许，漏报不允许）→ 候选文件清单
          → [2] compdb.py 按文件查 compile_commands.json 取原始编译参数
                （头文件无 TU 条目 → 借包含它的 .c TU 参数，产物标注 TU 来源）
          → [3] extract/ 各提取器只解析候选文件：
                dispatch（静态分发表）+ registry（注册式调用点）
                + statemachine（状态机表）+ globalvar（配置变量名单后启用）
          → [5] JSON + markdown（每条带 baseline_commit 与 source_hash）
```

`compile_commands.json` 是**参数字典，不是扫描队列**——逐 TU 全量解析是
`clangd-indexer` 的成本，正是本方案要避开的。

日常变更走 `refresh` 增量路径（`incr.py`）：不重扫全仓，`git diff` 圈定
受影响候选，只重解析它们并与上次产物 merge。nightly 集成见
[`docs/ci-integration.md`](docs/ci-integration.md)。

## 提取器与形态支持

**dispatch（静态分发表）**：

- 普通宏表 `{ MSG_1001, sess_handle_invite }`：msg_id 取源码宏拼写（extent 截取），
  展开值经 `clang_Cursor_Evaluate`（ctypes 直调）记入 `msg_id_value`；
- 指定初始化器 `{ .msg_id = ..., .handler = ... }`：按字段类型而非位置配对，乱序安全；
- `#ifdef` 条件表项：文本回溯条件栈，记录 `cond`；
- `(cast)fn` / `&fn` 形式 handler：剥 CStyleCastExpr / UnaryOperator 后取 referenced + USR；
- X-Macro 表：表项 location 指向 `.def` 真实来源文件；实参 token extent 塌缩时
  对表项源码文本正则分词兜底取 msg_id。

**registry（注册式分发）**：配置 `[scan] register_apis` 后启用。函数体内
`MsgReg(MSG_1001, fn)` 调用点经 AST `CallExpr` 匹配，产出与分发表同构的
表项，挂到虚拟表 `registry:<ApiName>` 下合入 dispatch 产物。

**ops-struct（单结构体分发，M6）**：配置 `[extract] ops_structs = true`
后启用。非数组结构体的函数指针成员初始化器——curl `struct Curl_protocol` /
pjsip `mod_tsx_layer` / Linux `file_operations` 惯用法；指定初始化器按
成员名配对、按位初始化按声明顺序对齐，`msg_id` = 成员名，合入 dispatch
产物。

**statemachine（状态机表）**：与分发表同套 InitListExpr 遍历，字段映射
配置化（`[statemachine]`）；含 state+event 字段的表自动从 dispatch 产物
剔除、归入状态机产物。switch 式手写状态机不做（按设计）。

**globalvar（全局变量读写）**：配置 `[globalvar] variables` 后启用。文本
搜变量名 → 解析命中文件（含函数体）→ 引用分类（assign / compound /
incdec / addr 保守记写 / 其余为读），写者全量列出、读者按
`[subsystems]` 路径前缀聚合模块；副产物为按定义位置分组的布局清单。

## 已知限制

- `clang_Cursor_Evaluate` 仅对白名单表达式 kind 求值（INIT_LIST_EXPR 等
  聚合初始化会 segfault libclang，见 `navmap/clangeval.py` 注释与回归测试）；
- 名单扩充为半自动：`suggest-apis`/`suggest-vars` 出候选清单，人工确认后
  拷入配置（设计 §5.3-1/§5.5-1 的人审环节保留）；
- globalvar 引用分类不穿透函数调用（`f(&g_x)` 传参按 `addr` 保守记写）；
- clangd 索引联动校验（§7 一致性检查）未实现，MVP 期用 USR 对齐；
- refresh 只按 committed diff 判断（工作区未提交改动不参与）；头文件包含关系
  只看直接 `#include`，不递归；globalvar 任一 ref_file 变更即整体重算；
- compdb 覆盖面即产物覆盖面：单 defconfig/单 build 的 compile_commands.json
  之外的候选文件记为解析失败（借参也借不到时）。多配置覆盖率靠合并多份
  compdb 提升；
- vendored conda `libclang13-20.1.0` 只含 .so、不含 clang resource dir（内建头
  `limits.h` 等）：宿主解析依赖系统 gcc 头目录自动探测，探不到时用
  `[extract] extra_args` 显式补；
- switch 式手写状态机不做（按设计）。

## 真实仓实测（2026-08-17；2026-09-04 CI 复测）

- **u-boot**（C，176 万行，sandbox defconfig compdb 1246 TU）：1228 候选 →
  19 表 131 表项（dispatch），USR/位置零缺失，抽查表项与源码逐一吻合；
  statemachine 0 命中（命名词根下无 state+event 结构体表）、registry 0 命中
  （默认 API 名单面向电信代码）。923 个失败中 892 个是单配置 compdb 覆盖
  不到、31 个是跨配置借参/头文件不自包含。全量 extract 分钟级。
- **srsRAN_Project**（现代 C++，100 万行）：29 候选 → 25 干净解析、0 表——
  现代 C++ 的分发是注册式（成员函数注册器），生产接入时需按仓内实际
  注册 API 配 `[scan] register_apis` 后由 registry 提取器覆盖。
- 实测抓出并修复：compdb 相对路径解析、借参头文件 `-x` 语言、`-Werror`
  熔断、`clang_Cursor_Evaluate` 对 INIT_LIST_EXPR segfault（已加白名单
  与回归测试）。
- 名单扩充实测：`suggest-apis` 在 u-boot 发现 `efi_create_event`（8 handler）、
  `cyclic_register`（5）等真实注册 API；`suggest-vars` 全仓扫描约 6.5 分钟
  （176 万行），通用短名（`state`/`test` 类）有噪音，靠人审过滤——电信
  代码 `g_` 前缀命名下信噪比会好得多。
- **CI 复测（2026-09-04，GitHub Actions）**：u-boot v2026.07 复测 1212
  候选 → 20 表 124 表项，USR/位置/msg_id 全量断言通过；期间抓出并修复
  `U_BOOT_SUBCMD_MKENT` 宏展开塌缩 extent 的 msg_id 丢失（blkmap_subcmds
  9 表项），补三重兜底（宏调用首实参归一 / eval_str / 声明文本按序配对）。
  AetherStack 回归：构建目录（build*/_deps/googletest）零污染断言通过。

## 测试

```bash
.venv/bin/python -m pytest tests/ -q
```

CI（`.github/workflows/ci.yml`）四层：fixture 单测 → u-boot v2026.07 全量
提取回归（表数/表项/USR 完整性/refresh 短路断言）→ AetherStack master
提取回归（构建目录污染断言 + suggest-apis 冒烟）→ **种子仓矩阵**（fork 在
Atituiset 下的 SAST seed repos：freeDiameter/collectd/pjproject 验证 M3
registry、curl 验证 ops-struct、open5gs/usrsctp 作为 switch-FSM 负对照；
每个种子配真实注册 API 名单跑 extract 并断言产出，M4 globalvar 同场验证）。

fixture `tests/fixtures/mini_ims/` 覆盖：五种分发表形态（普通宏表/乱序指定
初始化/X-Macro/#ifdef 表项/cast 与 & 形式 handler）、状态机表（普通行/
#ifdef 行/纯迁移指定初始化行）、注册式调用点、全局变量读写分类、嵌套
聚合初始化（eval segfault 回归）；`compile_commands.json` 由
`gen_compdb.py` 按本机路径生成（conftest 自动调用）。
