---
name: onboard-verify-evidence
description: 独立核实 onboard 四维度报告中的 dominant/conflict/unknown，生成核实侧车；不编写候选或代替最终发布审核。
---

本阶段由新建的 evidence-reviewer 独立实例执行，委派使用 shared_context="none"。逐项复核残留桶，不编辑原始维度报告或发布候选。

## 固定输入与唯一输出

必须读取:

- `.agent/onboard/evidence/conventions.md`
- `.agent/onboard/evidence/runtime-flow.md`
- `.agent/onboard/evidence/change-patterns.md`
- `.agent/onboard/evidence/guardrails.md`
- `.agent/onboard/evidence/cross-module.md`
- `.agent/onboard/evidence/repository-map.md`
- `.agent/onboard/shard-plan.md`
- `.agent/onboard/cards/*.md`(按需按点名符号回查)

唯一允许写入的正式文件是 `.agent/onboard/evidence/verification.md`,另可写同目录的 `verification.md.partial`。不得编辑维度报告、跨模块账本、根 `AGENTS.md` 或项目技能。

输入还包含仓库快照、范围、深度;报告头必须记录本轮仓库快照,供主 agent 验收比对。

## 上下文预算(硬约束)

- 只对残留桶发现、按其已给出的 `module::symbol`/`file::field` 打开**定向**源码核对(`get_code_snippet`/`trace_path`/`search_code`/`search_graph`,Git 可用时 `shell` 只跑只读 `git log`/`show`/`diff`),**绝不重新通读整个模块或重跑 REDUCE**。
- `confirmed` 发现不复核(其活跃规则线由 候选审核执行器 逐条验),除非它与某 `conflict` 直接对立、需一并判定。

## 核实方法

1. 枚举四份报告里**全部** `dominant`、`conflict`、`unknown` 发现,逐条按其证据引用打开有界源码:
   - `unknown`:尝试判定。跨模块类的先查 `cross-module.md` 账本,账本已定的直接采纳;账本未覆盖或维度内聚合类的,打开点名符号核对。能判定则改判为 `confirmed`、`dominant` 或 `conflict`;适用范围不同导致的"未解"拆成分范围规则。**只有打开源码后代码仍静态不可判定(如运行时拼接目标)才保留 `unknown`,并写明原因**;不得因"只在某片出现/跨了分片边界"而保留 `unknown`。
   - `conflict`:核对是否真·同范围互斥。若两端其实属不同层/不同场景,拆成两条各自带范围的规则,改判为对应等级,不再算冲突;确属同范围互斥才保留 `conflict`。
   - `dominant`:核对"多数"与"反例"是否都真实存在。反例不存在则升 `confirmed`;多数不成立则降级或改判。
2. 归一维度间分歧:同一关系或约定在不同维度被判不同等级时,以打开源码的核实结论统一,并记录被覆盖的原判。
3. 每条结论给出打开过的点名符号,使 候选审核执行器 能据此复核;无法给出已开符号的结论不成立。

## 侧车结构

```markdown
---
snapshot: <仓库快照>
findings_reviewed: <复核发现总数>
reclassified: <改判条数>
kept_unknown: <保留 unknown 条数>
---

# 分类核实侧车

## 核实范围与方法
## 逐项核实结论

| finding_id | 维度 | 原等级 | 核实后等级 | 已开符号 | 原因 |
|---|---|---|---|---|---|

## 保留为 unknown 的静态不可判定项
```

- 「逐项核实结论」:残留桶每个发现一行;`已开符号` 用函数/字段级引用,`原因` 一句话说明改判或保留依据。
- 「保留为 unknown 的静态不可判定项」:单列全部仍为 `unknown` 的发现,写明已开符号与不可判定原因,供 候选编写者 据实写入 `decisions.md`。

## 写入与返回

发布正式侧车前确认全部固定章节齐全。只返回:侧车路径、复核发现总数、改判条数、保留 `unknown` 条数。
