---
name: ktest-coverage-review
description: 本技能用于核验和拉取 KTest 覆盖率报告、精准测试报告、KDev 流水线日志与 GitLab 代码，并基于这些证据进行覆盖率诊断、达标路径分析和测试样例生成。当用户提供任意项目的 KTest 覆盖率链接/cid、精准测试 taskId/链接、KDev feature 链接或 GitLab 分支/仓库地址，要求验证数据完整性、分析覆盖率、定位未覆盖代码或生成测试建议时使用。不触发于通用代码审查、KAT 用例明细分析、线上故障排查或批量处理多个报告。
---

# KTest 覆盖率核验

本技能把四类只读数据整理成可核查证据:

- KTest 覆盖率报告: 报告元信息、树形下钻、源码与行级覆盖状态。
- 精准测试报告: 方法级增量覆盖率、影响链路、变更范围(changeRanges)。
- KDev feature 流水线: 准出流水线 job 清单、全量日志与错误日志。
- GitLab 代码: 分支代码、commit 对齐与文件清单。

不支持批量处理。一次只处理一个项目的一组数据：一个增量 cid、一个 taskId、一个 KDev feature 链接和一个代码目录。命令中的项目 ID、分支、仓库、cid、taskId 都必须来自用户输入或已成功拉取的数据，不得写死示例项目值。

## 分析原则与能力

分析层只消费已通过完整性检查的四样数据，不重新猜测或补造数据。每个结论必须能回溯到报告字段、源码行或明确的计算公式；无法从现有数据确认的内容直接标记“无法判断”，不输出确定性结论。

- **官方结论**：原样展示 KTest/精准测试平台的 pass、风险和阈值，不用自定义结论覆盖官方口径。
- **该测未测**：依据精准测试方法级增量覆盖率，以及 KTest `diff==1 && covered==1` 的变更行交集定位。
- **疑似冗余提示**：只展示变更未覆盖行和部分覆盖行及源码，供研发确认；不擅自认定代码冗余，不做全仓库调用链溯源。
- **达标路径**：按 KTest 增量行覆盖率公式计算目标缺口，按未覆盖变更行数排序推荐补测方法；支持自定义目标覆盖率。
- **测试样例**：只针对诊断出的未覆盖方法生成基于真实签名、依赖字段和源码的 JUnit 骨架；业务断言必须由研发补充。

## 四样数据与对应命令

| 数据 | 命令 | 产物 |
|---|---|---|
| KTest 覆盖率(增量) | `coverage_review.py coverage --cid <增量cid>` | `coverage_<cid>.json` |
| 精准测试报告 | `fetch_accuracy.py --task-id <taskId>` | `accuracy_report_<taskId>.json` |
| KDev 日志 | `coverage_review.py kdev --feature-url <url>` | `kdev_feature_<id>.json` + `logs/` |
| GitLab 代码 | 手动 clone 或 `kdev_fetch.py --with-code` | 代码目录 |

**cid 增量/全量坑(实测重要)**：KTest 报告分 `type=1`(全量) 和 `type=2`(增量)。
- 覆盖率分析**只用增量报告**(`type=2`,如 cid=9735795),对应精准测试报告的变更方法。
- 全量报告(`type=1`,如 cid=9735793)可能 12 万+ 行,`getCoverageMethodDetail` 会超时/500,不要拉。
- 精准测试报告的 `coverageReportId` 字段指向的是**全量**报告 id,**别拿它当增量 cid 拉**。

## 完整性硬规则

抓取层不产出"可能不完整"的数据。宁可失败并报告缺什么，也不输出一份看起来正常、实际缺文件的 JSON。取数层撒谎一次，分析层再准也是幻觉。

`coverage` 命令在落盘前会做完整性校验，任一失败即 abort（不写文件、不 emit ok）：

- 报告声明与遍历统计对账：`report.totalLines` 必须等于遍历统计的 `diff`，`report.missedLines` 必须等于 `diffMiss`
- 每个文件必须有 `lineList` 和 `fileContent`，缺任一即判定该文件数据不完整

若遇到 `INCOMPLETE_COVERAGE_DATA` 错误，说明拉取有缺口，优先排查树遍历是否漏了节点、接口是否被截断。不要忽略错误继续分析。

**防幻觉总闸**: 若某样数据因任何原因拉不到/不完整,不允许编造或猜测,直接向用户说明"未获取 xxx 信息",并给出补齐命令。可运行 `check` 统一检查四样数据:

```bash
uv run <skill_directory>/scripts/coverage_review.py check \
  --cid <增量cid> --feature-id <featureId> --task-id <taskId> \
  --code-dir <代码目录> --data-root data
```

<!-- skill-creator: askuser-with-text-fallback-v1 -->

## 用户交互：AskUser 优先与文本降级

凡需向用户收集信息、请求确认或要求选择时，优先调用 `askUser` 工具，并以结构化问题和可选项取得答复；关联问题应合并提问。

若 `askUser` 在当前运行环境不存在、调用失败或返回无效结果，才允许降级为普通聊天文本提问。降级时必须明确说明“当前 AskUser 不可用，以下为兼容提问”，并在收到用户明确答复前停止后续依赖该答复的操作；不得猜测、默认代填或把沉默视为同意。

## 何时使用

使用本技能处理以下问题:

- “这个 KTest 覆盖率报告的数据可信么？”
- “`/web/cov/reportDetail` 和 `/api/coverage` 到底是什么关系？”
- “给我拉取某个 cid 的覆盖率源码和 lineList。”
- “根据 KDev feature 链接拉完整流水线日志。”
- “确认 GitLab 分支 commit 是否和覆盖率报告、KDev 流水线对齐。”

不要用于:

- 只要求解释 Java 代码逻辑。
- 只要求做普通 MR 代码审查。
- 只要求分析 KAT 失败用例逐条明细。
- 同时处理多个 cid 或多个 feature 链接。

## 执行流程

1. 识别用户输入中的 `cid`、精准测试 taskId/链接、KTest URL、KDev feature URL 和 GitLab URL。
2. 本技能脚本可能需要较长时间拉取源码与日志，必须用 `call_remote_agent` 执行命令，`method` 使用 `CALLBACK`。
3. 如目标是覆盖率报告，执行命令:

```bash
uv run <skill_directory>/scripts/coverage_review.py coverage --cid <增量cid> --out tmp/ktest-coverage-<cid>
```

   或(拉精准测试方法级报告):

```bash
uv run <skill_directory>/scripts/fetch_accuracy.py --task-id <taskId>
# 或从 KDev 精准测试 job 日志自动提取 taskId:
uv run <skill_directory>/scripts/fetch_accuracy.py --from-log data/kdev/<featureId>/logs/<精准测试job>.log
```

4. 如目标是 KDev feature 日志，执行命令:

```bash
uv run <skill_directory>/scripts/coverage_review.py kdev --feature-url '<kdev_feature_url>' --out tmp/kdev-feature-<id>
```

5. 在进入分析前，必须执行四样数据完整性检查:

```bash
uv run <skill_directory>/scripts/coverage_review.py check \
  --cid <增量cid> --feature-id <featureId> --task-id <taskId> \
  --code-dir <代码目录> --data-root <data目录>
```

   只有 check 返回“四样数据齐全且完整”后，才允许进入分析；任何一项未获取或不完整，都必须直接告知用户缺少哪项。

6. 完整性检查通过后，执行覆盖率诊断:

```bash
uv run <skill_directory>/scripts/analyze.py \
  --task-id <taskId> --cid <增量cid> \
  --data-root <data目录> --code-dir <代码目录>
```

   如需评估自定义目标覆盖率:

```bash
uv run <skill_directory>/scripts/analyze.py \
  --task-id <taskId> --cid <增量cid> \
  --data-root <data目录> --code-dir <代码目录> --target <百分比>
```

   分析报告包括：官方结论、该测未测方法、变更未覆盖行、部分覆盖行、疑似冗余提示、达标路径和测试样例骨架。

7. 如目标是验证页面路由和后端接口映射，执行命令:

```bash
uv run <skill_directory>/scripts/coverage_review.py verify --cid <cid> --out tmp/ktest-verify-<cid>
```

8. 返回结果时列出生成文件路径、核心统计、证据来源和仍未覆盖的边界。所有命令中的 `<...>` 均为本次用户输入或取数结果，不得替换成固定示例值。

## 输出

脚本会把交付文件写到 `tmp/` 目录下(精准测试报告默认 `data/coverage/`)，典型产物包括:

- `coverage_<cid>.json`: 覆盖率结构化数据。
- `accuracy_report_<taskId>.json`: 精准测试方法级报告(含 changeRanges/影响链路)。
- `verification_<cid>.md`: 前端路由与后端接口映射核验证据。
- `kdev_feature_<id>.json`: KDev feature 与流水线元信息。
- `logs/`: KDev 各 job 全量日志和错误日志。
- `analysis_<taskId>_<cid>.md`: 覆盖率诊断报告(含该测未测/疑似冗余/达标路径/测试样例)。
- `data_check_*.md`: 四样数据完整性检查报告。

## 通用性

本技能是**通用型工具**，可处理任意项目的 KTest 覆盖率报告。SKILL.md 中出现的具体 `cid`/`taskId` 等值仅为接口实测记录，不构成对任何特定项目的绑定。所有参数必须由用户本次输入或取数结果提供。

## 参考资料

- 覆盖率接口说明: `references/coverage-api-notes.md`
- 精准测试报告接口说明: `references/accuracy-api-notes.md`
- KDev 日志接口说明: `references/kdev-api-notes.md`
- cid=9735795 核验证据: `references/verification_9735795.md`

## 错误处理

脚本失败时先读取 `references/error-handling.md`。不要直接猜测原因；根据 stdout 最后一行 JSON 的 `error` 和 `next_action` 决定下一步。
