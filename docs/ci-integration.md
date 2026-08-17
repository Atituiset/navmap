# navmap CI 集成（nightly 增量 + 周级全量）

> 设计文档 §6。目标：产物跟随源码仓演进，nightly 时长 < 30min，失败可回退。

## 作业拓扑

```
周级（周末低峰）:  navmap extract   全量重算，校正增量漂移（借参改向、候选膨胀）
nightly:           navmap refresh   git diff 增量，分钟级
按需（review 前）:  navmap report    质检分级，ERROR 报警
```

## nightly job 参考脚本

```bash
#!/usr/bin/env bash
set -euo pipefail

SRC=/path/to/src                  # 被测源码仓
NAVMAP=/path/to/navmap            # 本仓 checkout
OUT=$SRC/.navmap/out              # 产物目录（进 .gitignore）
SUB=ims                           # 子系统名，可循环多个

cd "$SRC"
git fetch && git checkout <nightly 分支> && git pull

BASELINE=$(git rev-parse HEAD)
COMPDB=$SRC/compile_commands.json  # compdb 需与源码同步重建（见下）

if [ -f "$OUT/navmap-dispatch-$SUB.json" ]; then
    # 增量：diff 圈定受影响候选，零命中时只 bump baseline（分钟级）
    "$NAVMAP/.venv/bin/navmap" refresh \
        --src "$SRC" --compdb "$COMPDB" \
        --subsystem "$SUB" --out "$OUT"
else
    # 首跑 / 产物丢失：全量
    "$NAVMAP/.venv/bin/navmap" extract \
        --src "$SRC" --compdb "$COMPDB" --baseline "$BASELINE" \
        --subsystem "$SUB" --out "$OUT"
fi

"$NAVMAP/.venv/bin/navmap" report --out "$OUT" --src "$SRC" > "$OUT/navmap-quality.md"
```

## 回退策略

- `refresh` 依赖 `git diff <baseline>..HEAD`：baseline 不可达（rebase/force-push/
  浅克隆）时命令直接报错退出——捕获后回退到全量 `extract` 即可（上脚本可包
  一层 `|| extract ...`）。
- 产物 JSON 自带 `baseline_commit` 与每条 `source_hash`，消费方（review 注入/
  MCP 工具）应先校验 baseline 与当前 diff base 的关系，超期产物降级为参考。

## compile_commands.json 纪律（生产最容易踩的坑）

产物正确性上限 = compdb 与源码的同步度：

- compdb 必须随构建配置变更重建（defconfig/cmake 选项变更、大规模文件增删），
  建议与 nightly 同 job 前置重建；
- 候选文件不在 compdb 且借不到参 → 记解析失败并在 report 的 ERROR 级露头，
  数量突增即 compdb 过期信号；
- 多 defconfig/多 build 的仓：分别生成 compdb 后**合并**（JSON 数组拼接 +
  按 file 去重），可显著提升覆盖率（u-boot 实测单 defconfig 只覆盖 1246/13405
  个文件）；
- GCC 专属参数无需手工清洗——navmap 已处理（相对路径绝对化、`-x` 补全、
  `-Wno-error` 压制）；特殊 toolchain 头路径用 `[extract] extra_args` 补。

## Gerrit CI 挂接（可选）

- 参照 open-code-review 仓 `examples/gerrit_ci/` 的同款基建：nightly job 定时
  触发即可，无需挂 Gerrit 事件；
- 若要在 review 时按 diff 落点子系统注入 markdown 产物：Gerrit hook → 取
  change 的文件清单 → 匹配 `[subsystems]` 路径前缀 → 选对应
  `navmap-*-<sub>.md` 注入 plan 阶段上下文（总体设计 §6：导航图优先、LSP 核对）。

## 资源与时长基线（实测，供容量规划）

| 仓 | 规模 | 候选 | extract 耗时 | refresh（无表变更） |
|---|---|---|---|---|
| u-boot | 176 万行 C | 1228 | 分钟级（后台任务） | 秒级短路 |
| srsRAN | 100 万行 C++ | 29 | 秒级 | 秒级短路 |

libclang 解析是主要成本，与候选文件数线性相关；`refresh` 只解析受影响候选，
nightly < 30min 的验收标准在千万行级仓上按"候选数 × 单文件解析耗时"估算即可。
