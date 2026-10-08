---
name: ktest-coverage-review
description: 本技能用于核验和拉取 KTest 覆盖率报告、精准测试报告、KDev 流水线日志与 GitLab 代码，并生成证据报告。当用户提供 KTest 覆盖率链接、cid、精准测试 taskId/链接、KDev feature 链接或 GitLab 分支链接，要求验证覆盖率数据、接口可信度、日志是否完整、代码是否对齐时使用。不触发于通用代码审查、KAT 用例明细分析、线上故障排查或批量处理多个报告。
---

# KTest 覆盖率核验

本技能把四类只读数据整理成可核查证据:

- KTest 覆盖率报告: 报告元信息、树形下钻、源码与行级覆盖状态。
- 精准测试报告: 方法级增量覆盖率、影响链路、变更范围(changeRanges)。
- KDev feature 流水线: 准出流水线 job 清单、全量日志与错误日志。
- GitLab 代码: 分支代码、commit 对齐与文件清单。

不支持批量处理。一次只处理一个 cid、一个 taskId、一个 KDev feature 链接或一个 GitLab 分支链接。

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

5. 如目标是验证页面路由和后端接口映射，执行命令:

```bash
uv run <skill_directory>/scripts/coverage_review.py verify --cid <cid> --out tmp/ktest-verify-<cid>
```

6. 返回结果时列出生成文件路径、核心统计、证据来源和仍未覆盖的边界。

## 输出

脚本会把交付文件写到 `tmp/` 目录下(精准测试报告默认 `data/coverage/`)，典型产物包括:

- `coverage_<cid>.json`: 覆盖率结构化数据。
- `accuracy_report_<taskId>.json`: 精准测试方法级报告(含 changeRanges/影响链路)。
- `verification_<cid>.md`: 前端路由与后端接口映射核验证据。
- `kdev_feature_<id>.json`: KDev feature 与流水线元信息。
- `logs/`: KDev 各 job 全量日志和错误日志。

## 参考资料

- 覆盖率接口说明: `references/coverage-api-notes.md`
- 精准测试报告接口说明: `references/accuracy-api-notes.md`
- KDev 日志接口说明: `references/kdev-api-notes.md`
- cid=9735795 核验证据: `references/verification_9735795.md`

## 错误处理

脚本失败时先读取 `references/error-handling.md`。不要直接猜测原因；根据 stdout 最后一行 JSON 的 `error` 和 `next_action` 决定下一步。
