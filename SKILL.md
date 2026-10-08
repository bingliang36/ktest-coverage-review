---
name: ktest-coverage-review
description: 本技能用于核验和拉取 KTest 覆盖率报告、KDev 流水线日志与 GitLab 代码，并生成证据报告。当用户提供 KTest 覆盖率链接、cid、KDev feature 链接或 GitLab 分支链接，要求验证覆盖率数据、接口可信度、日志是否完整、代码是否对齐时使用。不触发于通用代码审查、KAT 用例明细分析、线上故障排查或批量处理多个报告。
---

# KTest 覆盖率核验

本技能把三类只读数据整理成可核查证据:

- KTest 覆盖率报告: 报告元信息、树形下钻、源码与行级覆盖状态。
- KDev feature 流水线: 准出流水线 job 清单、全量日志与错误日志。
- GitLab 代码: 分支代码、commit 对齐与文件清单。

不支持批量处理。一次只处理一个 cid、一个 KDev feature 链接或一个 GitLab 分支链接。

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

1. 识别用户输入中的 `cid`、KTest URL、KDev feature URL 和 GitLab URL。
2. 本技能脚本可能需要较长时间拉取源码与日志，必须用 `call_remote_agent` 执行命令，`method` 使用 `CALLBACK`。
3. 如目标是覆盖率报告，执行命令:

```bash
uv run <skill_directory>/scripts/coverage_review.py coverage --cid <cid> --out tmp/ktest-coverage-<cid>
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

脚本会把交付文件写到 `tmp/` 目录下，典型产物包括:

- `coverage_<cid>.json`: 覆盖率结构化数据。
- `verification_<cid>.md`: 前端路由与后端接口映射核验证据。
- `kdev_feature_<id>.json`: KDev feature 与流水线元信息。
- `logs/`: KDev 各 job 全量日志和错误日志。

## 参考资料

- 覆盖率接口说明: `references/coverage-api-notes.md`
- KDev 日志接口说明: `references/kdev-api-notes.md`
- cid=9735795 核验证据: `references/verification_9735795.md`

## 错误处理

脚本失败时先读取 `references/error-handling.md`。不要直接猜测原因；根据 stdout 最后一行 JSON 的 `error` 和 `next_action` 决定下一步。
