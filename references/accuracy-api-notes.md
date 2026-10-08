# 精准测试(qa-itest)报告接口笔记

来源: 实测 taskId=939418 (locallife-aigc-genesis / feature_fix_compose_empty / commit 1cac94ca)

## 入口

- 页面: `https://qa-itest.corp.kuaishou.com/accuracy/ai/report?taskId=<taskId>` (前端路由;打开即注入 SSO)
- 触发来源: KDev 精准测试 job 日志里 `触发API返回结果.data.viewUrl`，形如
  `https://qa-itest.corp.kuaishou.com/accuracy/report?taskId=939418&from=kdev`
- 触发 API(在 KDev job 日志中): `POST https://qa-itest.corp.kuaishou.com/rest/api/accuracy/v1/plugin?kdevJobLogId=...&kdevJobLogToken=...`
  body 传 repo/devBranch/pipelineId 等,返回 `data.viewUrl` 即报告入口。

## 数据接口(核心)

- `POST https://qa-itest.corp.kuaishou.com/rest/api/accuracy/v1/report`
- body: `{"taskId": 939418}` (taskId 放 body, 不能放 query; GET 不支持, 会 405)
- 返回: `{"result":1, "success":true, "data":{...}}` (注意: 没有 KTest 那种 status 字段)
- data 关键结构:
  - `testReport`: 方法级核心。`testMethodList[]` 每个方法含:
    - `fullName` / `fullClazzName`: 完整方法名(含参数类型)
    - `increCoverage`: 增量覆盖率(0-1)
    - `coverage`: 集测行覆盖率, `branchCoverage`: 分支覆盖率, `linkCoverage`: 链路覆盖率(字符串)
    - `changeRanges[]`: 变更行范围 [{startLine,endLine}]
    - `affectedLink` / `coveredLink` / `unCoveredLink`: 影响链路数/已覆盖/未覆盖
    - `changeType`: 新增/修改/删除, `methodType`: 普通方法/构造函数等
    - `reportUrl`: `https://kdev.corp.kuaishou.com/web/cov/fileReport?cid=<全量cid>&id=<id>`
      **注意: 这个 cid 是全量报告 id, url 里的 id 不是文件级 id**,不能用它拉 KTest 文件级行数据
    - 其他风险字段: `hasFundRisk`/`hasTestRisk`/`isUpToStandard`/`methodCategory`/`inputArgs`/`outputArgs`
  - `testReport.testMethodCount` / `changeMethodCount` / `str` / `methodAvgIncreCoverage` / `methodAvgFullCoverage`
  - `testReport.unTestMethodList` / `ignoreMethodList`: 未测/无需测方法列表(本任务为空)
  - `scopeToBeTestReport`: 影响范围, `affectedChainCount`/`affectedEntranceCount`/`changeStatistics`
  - `qualityPredictionReport`: 风险结论 `conclusion`(pass/fail), `testRiskBO`(hasTestRisk 等)

## 抓取脚本

```bash
uv run <skill>/scripts/fetch_accuracy.py --task-id 939418
uv run <skill>/scripts/fetch_accuracy.py --from-log data/kdev/270240/logs/150857501_精准测试.log
```

- 复用 `scripts/_ktest_session.py`(已复制 KTestSession, 同域 SSO)
- 不能用 `session.post()`: 它假设 KTest 的 {status,data} 协议, 精准测试返回无 status → 用 `session._eval` 拿原始文本自解析
- 完整性校验: required 字段缺失即拒绝落盘, 明确报"未获取"

## 与 KTest 覆盖率的关系(重要)

精准测试报告和 KTest 覆盖率**必须关联用**:

- 精准测试 = 方法级 (增量覆盖率 + 分支 + 链路 + 变更范围), 但没有行级源码
- KTest = 行级 + 变更方法, 但没有影响链路
- 两者的 commit 必须一致 (如都是 1cac94ca) 才算对齐
- 精准测试的 `coverageReportId` = **全量** KTest 报告 cid (如 9735793), 不是增量 cid;
  行级分析要用同 commit 的**增量** cid (如 9735795, type=2)