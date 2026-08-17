# 导航图提取器（libclang）实施设计

> 日期：2026-08-16。上游文档：总体设计 [`cpp-review-context-index-memory-design.md`](cpp-review-context-index-memory-design.md)（下称"总体设计"）§5——本仓留存的是只读快照，其演进主场在 open-code-review 仓，本文不再同步其变更。
>
> **2026-08-17 起本文迁入 navmap 仓库（docs/），后续演进以本仓库版本为准。** 本文是其 L2.5 导航图层的独立实施设计，覆盖到可以开工的颗粒度。
>
> 目标：从千万行级 C/C++ 电信代码中，**自动化提取四类导航图**——消息分发表、状态机表、全局变量读写清单、全局状态布局——产出 JSON + markdown，供 review 时注入 prompt 或经 MCP 查询。

## 0. 核心约束（先记住这三条）

1. **libclang 不扫仓**。整体形态是漏斗：全仓文本粗筛（分钟级）→ 候选文件（几百个）→ libclang 只解析候选文件（十几分钟级）。`compile_commands.json` 是**参数字典，不是扫描队列**——它的作用是回答"这个文件该用什么参数解析"，不是被逐 TU 遍历（逐 TU 全量解析是 `clangd-indexer` 干的事，正是本方案要避开的成本）。
2. **编译参数即真相**。每个候选文件必须用 `compile_commands.json` 里对应 TU 的原始参数解析，否则宏展开和 `#ifdef` 全错。头文件没有自己的 TU 条目，处理见 §5.2 末尾。
3. **每条产物带基线 commit 与来源标注**，支持按文件 hash 增量失效。

## 1. 技术选型

| 项 | 选择 | 理由 |
|---|---|---|
| 语言 | Python 3.10+ | 提取逻辑迭代快，团队上手快；性能不是瓶颈（只跑候选文件） |
| AST | `clang.cindex`（libclang Python 绑定） | 与 clangd 同前端，同一编译参数下所见一致 |
| 编译数据库 | `compile_commands.json` | `clang.cindex.CompilationDatabase` 直接加载 |
| 粗筛 | tree-sitter（首选）或 ripgrep 正则（MVP 可先用正则） | 只负责找候选文件，允许误报 |
| 符号解析 | clangd 静态索引（经 `clangd-index-tool` dump 或预查） | 补 file:line、做一致性校验；MVP 阶段可用 libclang 自身的 `cursor.referenced` + USR 替代 |
| 产物存储 | JSON（机器查询）+ markdown（prompt 注入），同源生成 | 双格式不漂移 |

MVP 阶段不强依赖 clangd 索引：USR（Unified Symbol Resolution）在 TU 间稳定，可先用 USR 做跨文件符号对齐，clangd 索引就绪后再切换。

## 2. 仓库结构（提案）

```
navmap/
├── pyproject.toml
├── navmap/
│   ├── cli.py              # 入口: extract / report / merge 子命令
│   ├── compdb.py           # compile_commands.json 加载、TU 参数查询
│   ├── scan.py             # 第 1 步: 候选粗筛
│   ├── extract/
│   │   ├── dispatch.py     # 消息分发表
│   │   ├── registry.py     # 注册式分发(RegisterHandler 类)
│   │   ├── statemachine.py # 状态机表
│   │   └── globalvar.py    # 全局变量读写分类 + 布局
│   ├── resolve.py          # USR/索引 符号解析、file:line 补全
│   ├── model.py            # 数据模型 + JSON schema 序列化
│   ├── render.py           # JSON → markdown 渲染
│   ├── incr.py             # 增量: hash 比对、受影响集合计算
│   └── quality.py          # 覆盖率、一致性校验、报告
├── config/
│   └── navmap.toml         # 子系统划分、注册 API 名单、命名词根等
└── tests/
    └── fixtures/           # 微型 C 项目样例(含宏表/X-Macro/注册式)
```

## 3. 流水线总览（漏斗形态）

```
全仓源文件 (千万行)
   │  [1] 文本粗筛 scan.py (ripgrep/tree-sitter, 分钟级)
   │      模式: 函数指针结构体 + 数组初始化器 + 命名词根
   ▼
候选文件清单 (几百个, .json)
   │  [2] compdb.py: 逐文件查 compile_commands.json 取编译参数
   │      (头文件 → 借包含它的 .c TU 的参数, 见 §5.2)
   ▼
libclang 按正确参数逐候选文件解析 AST (十几分钟级)
   │  [3] extract/(dispatch|registry|statemachine|globalvar) 并行
   ▼
[4] resolve: USR 对齐跨 TU 符号, 补 file:line
   ▼
[5] render: JSON + markdown (带基线 commit)
   ▼
[6] quality: 覆盖率 + 一致性 → 报告
```

clangd 索引（可选，就绪后接入）：加速全局变量引用发现、开启一致性校验，见 §5.5 与 §7。

增量模式（nightly）：`git diff <baseline>..HEAD` 得变更文件 → 只重跑受影响的提取单元 → 与上次产物 merge。受影响集合 = 变更文件本身 ∪ （变更文件实现的 handler 所在的表）∪（引用集变化的全局变量）。

## 4. 数据模型

```jsonc
// navmap-dispatch-<subsystem>.json
{
  "baseline_commit": "abc123",
  "generated_at": "2026-08-17T02:00:00+08:00",
  "subsystem": "ims",
  "tables": [
    {
      "name": "g_msgTable",
      "file": "protocol/ims/disp.c",
      "line": 120,
      "source_hash": "sha256:...",        // 表所在文件 hash, 增量失效键
      "entries": [
        {
          "msg_id": "MSG_1001",            // 源码宏拼写, 不是展开值
          "msg_id_value": "0x3EC",         // 展开值(可选, 供调试)
          "handler": "sess_handle_invite",
          "handler_loc": "sess/invite.c:880",
          "handler_usr": "c:@F@sess_handle_invite#",
          "cond": "FEATURE_IMS",           // #ifdef 条件, 无则 null
          "source": "ast"                  // ast | llm-extracted
        }
      ]
    }
  ]
}
```

状态机表同构：`{state, event, handler, next_state}` 四元组。全局变量清单：

```jsonc
{
  "variable": "g_sysConfig",
  "def_loc": "oam/config.c:45",
  "writers": [{"func": "cfg_apply", "loc": "oam/config.c:310", "kind": "assign"}],
  "readers_by_module": {"oam": 42, "ims": 17},
  "total_refs": 63
}
```

## 5. 各提取器设计

### 5.1 粗筛（scan.py）

模式（正则 MVP 版，tree-sitter 版后补）：

- 含函数指针成员的结构体定义：`typedef struct { ... void (*...)(...); ... }`
- 数组初始化器：`<type> <name>[] = {` 或 `[N] = {`，`name` 命中 `table|disp|map|hdlr|state|trans` 词根（大小写不敏感）
- 注册调用点：对配置的注册 API 名单做字面搜索

误报无所谓（第 2 步 AST 会过滤），漏报才要紧——词根名单先宽后收。

### 5.2 分发表（extract/dispatch.py）

AST 遍历逻辑：

1. `TranslationUnit` 的 cursor 中找 `VarDecl`，类型为数组且元素类型（`typedef` 解引用后）是含函数指针成员的结构体；
2. 取其 `InitListExpr`，逐子节点（每个元素初始化）：
   - 指定初始化器：clang 表现为 `InitListExpr` 内嵌字段顺序或（较新版本）`DesignatedInitExpr`——遍历字段初始化，按结构体字段名匹配 `handler`/`func`/`fp` 类成员；
   - 消息 ID 字段：取该初始化表达式**在源码中的原始拼写**（`cursor.extent` 截取源文本），保留宏名；展开值经 `cursor.evaluate()`（如可用）或常量折叠取；
   - handler 字段：`cursor.referenced` 得函数声明 → 记 USR；若是指针强转/取地址形式（`(void*)fn` / `&fn`），剥掉 `CStyleCastExpr`/`UnaryOperator` 再取 referenced；
3. 记录表项所处 `#ifdef` 条件：从源文本按行回溯条件编译栈（libclang 的 preprocessing 记录或简单文本扫描）。

X-Macro 表（`#include "msg.def"` 展开生成）：展开后的 `InitListExpr` 元素 location 指向 `.def` 文件，`cursor.location` 取 `spelling location` 即可得到真实来源文件——天然支持，无需特判。

**头文件中的表**：`compile_commands.json` 只记录 `.c` TU，头文件没有自己的条目。粗筛命中的候选若是 `.h`/`.def`，处理办法：在编译数据库里找一个包含它的 `.c` TU（libclang 解析任一包含者后经 inclusion 记录反查，或文本搜 `#include`），借该 TU 的参数直接 parse 头文件（libclang 支持对头文件手动传参）。注意同一头文件被多个变体 TU 包含时宏展开可能不同——取主变体的参数，并在产物中标注所用 TU 来源。

### 5.3 注册式分发（extract/registry.py）

无静态表、运行期注册的场景：

1. **发现注册 API**（半自动）：配置种子名单（首版人工配 10~20 个，如 `RegisterHandler`/`MsgReg`）；自动扩展——在索引/USR 库里找"某参数为函数指针类型、且调用点传入的不同函数数 > 阈值（如 10）"的函数，列给人工确认后入名单；
2. **提取调用点**：全仓文本搜 API 名得候选行 → 所在文件进 AST → 找 `CallExpr`，`referenced` USR 匹配注册 API → 提取第 1 参数（消息 ID 宏拼写）与函数指针参数（剥 cast）；
3. 产出与分发表同构的条目，挂到"虚拟表"`registry:<ApiName>` 下。

### 5.4 状态机表（extract/statemachine.py）

与分发表复用同一套 `InitListExpr` 遍历，模板差异仅在字段映射：`{state, event, handler, next_state}`。字段名配置化（`config/navmap.toml` 里按子系统配字段名映射），因为各团队命名不同。switch 式手写状态机**首版不做**，在报告中统计"疑似 switch 状态机"数量供后续决策。

### 5.5 全局变量读写（extract/globalvar.py）

1. 目标变量来源：`config/navmap.toml` 手工名单（L2 文档里的关键全局量）+ 自动候选（被引用次数 Top N 的全局变量）；
2. 有 clangd 索引时：索引查全部引用位置 → 只解析引用文件。无索引 MVP 期：文本搜变量名得候选文件（全局变量名在电信代码里几乎不撞名）；
3. AST 分类：对每处引用 cursor，沿父链判断——
   - **写**：`BinaryOperator` 赋值左值、`CompoundAssign`（`+=` 等）、`UnaryOperator` 的 `++/--`、`UnaryOperator(&)` 取地址（保守记写，可能经指针传出修改）；
   - **读**：其余；
   - 记录所在函数（沿父链到 `FunctionDecl`）与模块（路径前缀映射，配置化）；
4. 聚合：写者函数全量列出（通常个位数），读者按模块聚合计数。

### 5.6 全局状态布局

`globalvar` 的副产物：按模块分组列出其定义的全局变量（非 static、或被跨模块引用者），渲染成一张布局 markdown。零额外 AST 工作。

## 6. 增量与 nightly 集成

```bash
# nightly job（挂 Gerrit CI 同款基建）
navmap extract --compdb compile_commands.json \
               --baseline $(git rev-parse HEAD) \
               --since <上次基线> \
               --state .navmap/state.json \
               --out .navmap/out/
navmap report --out .navmap/out/ > navmap-quality.md
```

- `state.json`：上次每个提取单元的输入文件 hash 集合 → 本次 diff 后重算受影响单元；
- 未受影响单元的产物原样拷贝，只更新 `baseline_commit`；
- 产物按子系统分文件（`navmap-dispatch-ims.json`），便于按 diff 落点选择性注入。

## 7. 质检（quality.py）

- **覆盖率**：消息枚举（从协议头文件的 `enum`/`#define` 群提取）中有 handler 的 ID 占比，按子系统出表；
- **一致性**：每个 handler 的 USR 在索引中存在定义；表文件在 handler 的 callers 集合中（有索引后开启）；
- **孤儿 handler**：定义了但不出现在任何表/注册点的协议类函数（命名启发式 `handle_*`/`*_hdlr`）——潜在死代码信号；
- 报告分级：ERROR（提取器 bug，如 USR 无法解析）/ WARN（覆盖缺口）/ INFO（统计）。

## 8. 里程碑

| 里程碑 | 内容 | 验收 |
|---|---|---|
| M0 地基 | `compile_commands.json` 生成 + TU 覆盖率统计脚本 | 覆盖报告；目标子系统 ≥95% TU 可解析 |
| M1 分发表 MVP | scan + dispatch + render，一个子系统试点，fixture 单测 | 覆盖率报告；人工抽 20 条全对 |
| M2 全量分发表 + 质检 | 推全子系统；quality.py 三指标 | 全仓覆盖率报告 |
| M3 注册式 + 状态机 | registry.py + statemachine.py | 注册 API 名单评审入库 |
| M4 全局变量 | globalvar.py + 布局图 | 关键全局量读写清单人工评审 |
| M5 nightly 增量 | incr.py + CI 集成 + clangd 索引联动校验 | nightly 时长 <30min |

**进度（2026-08-17）**：M1~M5 主体已落地——

- **M1/M2**：scan + dispatch + render + fixture 单测全绿；五种表形态及头文件借参覆盖；`msg_id_value` 展开值已由 `clangeval.py`（ctypes 直调 `clang_Cursor_Evaluate`，白名单 kind 防 INIT_LIST_EXPR segfault）补上；quality.py 三指标（覆盖率/一致性/孤儿 handler，ERROR/WARN/INFO 分级）接入 `report`。
- **M3**：registry.py（注册 API 名单内调用点 → 虚拟表 `registry:<Api>` 合入 dispatch 产物）+ statemachine.py（字段映射配置化，四元组，命中表自动从 dispatch 产物剔除）。
- **M4**：globalvar.py（引用分类 assign/compound/incdec/addr/read，写者全量 + 读者按模块聚合 + 布局），父链分类经自顶向下祖先链实现（libclang 表达式 cursor 的 semantic_parent 为空）。
- **M5**：incr.py（`navmap refresh`，git diff 受影响集合五条规则，多产物 merge，globalvar 按 ref_files 失效整体重算）+ CI 集成文档（`docs/ci-integration.md`；clangd 索引联动校验留作后续）。
- **真实仓实测修正**：compdb 相对路径按 directory 绝对化、借参头文件补 `-x`、统一 `-Wno-error`、`[extract] extra_args` 逃生口。u-boot（176 万行 C）19 表 131 表项抽查全对；srsRAN（100 万行 C++）25 干净解析 0 表（现代 C++ 为注册式，待生产环境配 register_apis 验证）。

- **名单扩充（§5.3-1/§5.5-1）**：`suggest-apis`（函数指针参数 + 不同 handler 数 ≥ 阈值的调用点归集）与 `suggest-vars`（extern 声明全局变量按全仓引用次数 Top-N）出候选清单，人审后入配置。

已知遗留：clangd 索引一致性校验（§7 有索引后开启，待生产索引环境）。

M1 前可用现成开源 C 项目（如含消息表的协议栈实现）做 fixture 先行开发，不等 M0。

## 9. 与 review 侧的接口（预览）

- JSON 产物 → 封装 MCP 工具 `lookup_handler(msg_id)` / `get_dispatch_table(subsystem)` / `get_global_var_rw(var)`；
- markdown 产物 → 按 diff 落点子系统注入 plan 阶段上下文；
- 装配顺序见总体设计 §6：导航图优先、LSP 核对。
