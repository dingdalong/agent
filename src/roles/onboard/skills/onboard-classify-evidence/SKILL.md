---
name: onboard-classify-evidence
description: 对 onboard 已就绪的证据卡与跨模块账本按指定维度归类，生成该维度报告；一次只处理一个维度，不重跑分片分析。
---

每次只处理指定的 dimension，从证据卡与跨模块账本归类，不读取其他并行维度的报告。

## 输入与输出

主 agent 每次调用必须提供：仓库快照、范围、深度、本次 `dimension`（∈ `conventions` / `runtime-flow` / `change-patterns` / `guardrails`）、本维度固定报告路径，以及证据来源：`.agent/onboard/cards/*.md`（全部证据卡）、`.agent/onboard/evidence/repository-map.md`、`.agent/onboard/evidence/cross-module.md`（跨模块事实账本）、`.agent/onboard/shard-plan.md`。

各维度固定报告路径（只允许写入指派维度对应的路径及同目录 `<name>.md.partial`）：

| dimension | 固定报告路径 |
| --- | --- |
| conventions | `.agent/onboard/evidence/conventions.md` |
| runtime-flow | `.agent/onboard/evidence/runtime-flow.md` |
| change-patterns | `.agent/onboard/evidence/change-patterns.md` |
| guardrails | `.agent/onboard/evidence/guardrails.md` |

## 共享归类契约（所有维度通用）

1. **以卡为样本单元做维度内归类**：读取全部证据卡中本维度对应的线索段（`conventions 线索` / `runtime-flow 线索` / `change-patterns 线索` / `guardrails 线索`）、「关键符号与引用」「未知项/需跨模块确认」，结合仓库地图与分片计划归类。不重新逐目录横扫源码。
2. **跨模块引用账本、不自行 join**：凡属跨模块关系（跨层依赖方向、公共库入口对端、注册点与消费者、原子跨模块链路）一律引用 `cross-module.md` 账本的已核实结论与等级；账本未覆盖的**本维度内**环节才用 `search_graph`/`query_graph`/`search_code`/`trace_path` 补齐。
3. **四级分类门槛**：跨 ≥3 张卡一致或框架契约 + 调用点确认 → `confirmed`；多数卡一致但存在卡级反例 → `dominant`；同一适用范围内多机制/多结论并存 → `conflict`；经账本或有界核对后仍**静态不可判定** → `unknown`。仅单卡出现属样本不足，按其适用范围记为带范围的 `dominant` 或「不入册」，不得仅因样本少判 `unknown`；也不得因跨分片未串起而判 `unknown`。`conflict` 不得被压缩为单一结论。
4. **有界核对**：仅当要把某条升级为 `confirmed`、而卡内线索不足以支撑时，才用 `get_code_snippet`/`read_file` 打开**有限**具体符号核对，只摘录理解结论所需的最短代码，绝不重新通读整个模块。项目实际不存在的机制不生成占位结论。
5. **统一证据字段**：每项证据发现包含 `finding_id`、classification、candidate_instruction、适用范围、函数/字段级引用（`module::symbol` 或 `file::field`，关键字可检索，可选附路径，不强制行号）、样本覆盖（覆盖的卡/模块）、反例和仓库快照。没有达到门槛的普遍印象不得写成规则。

## 按维度执行

按输入的 dimension 只读取对应参考，结合上述共享契约生成本维度报告：

- `conventions`：[分析方法与报告结构](references/conventions.md)
- `runtime-flow`：[分析方法与报告结构](references/runtime-flow.md)
- `change-patterns`：[分析方法与报告结构](references/change-patterns.md)
- `guardrails`：[分析方法与报告结构](references/guardrails.md)

## 写入与返回

发布正式报告前确认：报告头记录了仓库快照与本次实际读取的 shard 清单、shard/卡覆盖完整、全部固定章节齐全。发布用 `.partial` → `exec_command` 执行 `mv`；残留 `.partial` 不算完成。只向主 agent 返回本维度报告路径、覆盖摘要（卡/模块数）、各等级/各类数量统计与未覆盖项，不回传报告正文。
