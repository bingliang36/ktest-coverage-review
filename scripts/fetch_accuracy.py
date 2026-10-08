#!/usr/bin/env python3
"""精准测试报告取数 —— 从 taskId 拉取精准测试(电商 QA-Accuracy)方法级覆盖率报告。

数据源: qa-itest.corp.kuaishou.com(快手 SSO/OBO 域,复用 KTestSession 浏览器会话)

为什么需要它:
  KTest 覆盖率(covfetch/fetch.py)拿的是【行级 + 变更方法】覆盖率;
  精准测试报告拿的是【方法级 + 影响链路】覆盖率,带 affectedLink/coveredLink、
  changeRanges(变更行)等 KTest 没有的维度。两者必须关联才能回答
  "哪些方法该测却没测"。

用法:
  # 已知 taskId
  uv run fetch_accuracy.py --task-id 939418

  # 从 KDev 日志文本里提取 taskId 再拉
  uv run fetch_accuracy.py --task-id 939418 --from-log data/kdev/270240/logs/150857501_精准测试.log

  # 只打印不落盘
  uv run fetch_accuracy.py --task-id 939418 --print-only

输出: <outdir>/accuracy_report_<taskId>.json

完整性规则(与 SKILL.md 一致):
  宁可失败并报告缺什么,也不输出一份看起来正常、实际缺字段的 JSON。
  拉不到 taskId / report 接口失败 / 关键字段缺失 => 明确报错,不落盘不编造。
"""
import argparse
import json
import os
import re
import sys

# 复用仓库内的浏览器会话封装(同域 SSO,自包含,不依赖外部 tools/covfetch)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPT_DIR)

try:
    from _ktest_session import KTestSession  # noqa: E402
except ImportError as e:
    raise SystemExit(
        f'[fetch_accuracy] 无法导入 KTestSession(需要 scripts/_ktest_session.py): {e}\n'
        '请确认该文件存在(cp tools/covfetch/ktest_session.py scripts/_ktest_session.py)。'
    )

WS = os.environ.get('COVFETCH_WS') or os.getcwd()
ACCURACY_BASE = 'https://qa-itest.corp.kuaishou.com'
ACCURACY_ANCHOR = f'{ACCURACY_BASE}/accuracy/ai/report'  # 打开即注入 SSO
ACCURACY_API = '/rest/api/accuracy/v1/report'            # 实测: POST, body 带 taskId

# 报告里"方法级覆盖率"关键字段(实测 cid=9735793 / taskId=939418)
REQUIRED_TOP = ['qualityPredictionReport', 'scopeToBeTestReport', 'testReport']
REQUIRED_TEST = ['testMethodList', 'testMethodCount', 'branch', 'methodAvgIncreCoverage']
METHOD_KEYS = ['fullName', 'increCoverage', 'branchCoverage', 'linkCoverage',
               'changeRanges', 'affectedLink', 'coveredLink', 'unCoveredLink']


def extract_task_id(log_text: str) -> int | None:
    """从 KDev 精准测试 job 日志里提取 taskId。返回 None 表示未找到。"""
    # 实测: 触发API返回结果里的 viewUrl 带 taskId=939418
    for pat in (r'taskId=(\d+)', r'"taskId"\s*:\s*"?(\d+)"?'):
        m = re.search(pat, log_text)
        if m:
            return int(m.group(1))
    return None


def fetch_report(session: KTestSession, task_id: int) -> dict:
    """POST 拉取精准测试报告,返回 data 部分。失败抛 RuntimeError(带明确原因)。

    注意: 不能直接用 session.post —— 它假设响应是 KTest 的 {status,data} 协议,
    而精准测试(qa-itest)返回 {result,data,success,...},没有 status 字段,
    会被 _parse 误判为失败。这里用 session._eval 拿原始文本自解析。
    """
    js = (
        "(async()=>{const r=await fetch('/rest/api/accuracy/v1/report',"
        "{method:'POST',headers:{'Content-Type':'application/json'},"
        "credentials:'include',body:JSON.stringify({taskId:%d})});"
        "return await r.text();})()" % task_id
    )
    text = session._eval(js)
    try:
        d = json.loads(text)
    except json.JSONDecodeError:
        raise RuntimeError(f'accuracy report 返回非 JSON: {text[:200]}')
    if not d.get('success') or d.get('result') != 1:
        raise RuntimeError(f'accuracy report 业务失败: {str(d)[:300]}')
    data = d.get('data')
    if not isinstance(data, dict) or not data.get('testReport'):
        raise RuntimeError(f'accuracy report 响应缺少 testReport: {str(d)[:300]}')
    return data


def validate_report(data: dict, task_id: int) -> list[str]:
    """完整性校验。返回错误列表;为空表示通过。"""
    errs = []
    for k in REQUIRED_TOP:
        if k not in data:
            errs.append(f'缺少顶层字段 {k}')
    tr = data.get('testReport') or {}
    for k in ['testMethodList', 'testMethodCount', 'branch', 'methodAvgIncreCoverage']:
        if k not in tr:
            errs.append(f'testReport 缺少字段 {k}')
    for i, m in enumerate(tr.get('testMethodList') or []):
        for k in METHOD_KEYS:
            if k not in m:
                errs.append(f'testMethodList[{i}] 缺少字段 {k}')
    return errs


def build_payload(data: dict, task_id: int) -> dict:
    """规范化落盘结构:保留原始 data + 提取关键汇总,便于分析层直接消费。"""
    tr = data.get('testReport') or {}
    st = data.get('scopeToBeTestReport') or {}
    return {
        'taskId': task_id,
        'source': {'accuracy': f'{ACCURACY_BASE}/accuracy/ai/report?taskId={task_id}'},
        'coverageReportId': data.get('coverageReportId'),
        'branch': tr.get('branch'),
        'summary': {
            'conclusion': (data.get('qualityPredictionReport') or {}).get('conclusion'),
            'testMethodCount': tr.get('testMethodCount'),
            'changeMethodCount': tr.get('changeMethodCount'),
            'testMethodCoverage': tr.get('testMethodCoverage'),
            'methodAvgIncreCoverage': tr.get('methodAvgIncreCoverage'),
            'methodAvgFullCoverage': tr.get('methodAvgFullCoverage'),
            'affectedChainCount': st.get('affectedChainCount'),
            'affectedEntranceCount': st.get('affectedEntranceCount'),
        },
        'testReport': tr,
        'scopeToBeTestReport': st,
        'qualityPredictionReport': data.get('qualityPredictionReport'),
        'affectEntranceReport': data.get('affectEntranceReport'),
    }


def main():
    p = argparse.ArgumentParser(description='拉取精准测试方法级覆盖率报告')
    p.add_argument('--task-id', type=int, help='精准测试 taskId;省略则从 --from-log 提取')
    p.add_argument('--from-log', help='KDev 精准测试 job 日志文件,自动提取 taskId')
    p.add_argument('--outdir', default=os.path.join(WS, 'data/coverage'))
    p.add_argument('--print-only', action='store_true', help='只打印不落盘')
    p.add_argument('-q', '--quiet', action='store_true')
    a = p.parse_args()
    v = not a.quiet

    task_id = a.task_id
    if not task_id and a.from_log:
        try:
            txt = open(a.from_log, encoding='utf-8', errors='replace').read()
        except OSError as e:
            print(f'[fetch_accuracy] 读取日志失败: {e}', file=sys.stderr)
            sys.exit(2)
        task_id = extract_task_id(txt)
        if not task_id:
            print(f'[fetch_accuracy] 在日志里未找到 taskId: {a.from_log} => 未获取精准测试报告', file=sys.stderr)
            sys.exit(2)
        if v:
            print(f'[fetch_accuracy] 从日志提取 taskId={task_id}', file=sys.stderr)
    if not task_id:
        p.error('需要 --task-id,或 --from-log 提供含 taskId 的日志')

    if v:
        print(f'[fetch_accuracy] 打开 SSO 会话锚点 {ACCURACY_ANCHOR}', file=sys.stderr)
    with KTestSession(anchor_url=ACCURACY_ANCHOR, verbose=v) as s:
        try:
            data = fetch_report(s, task_id)
        except RuntimeError as e:
            print(f'[fetch_accuracy] 精准测试报告拉取失败: {e} => 未获取 taskId={task_id} 的报告', file=sys.stderr)
            sys.exit(1)

    errs = validate_report(data, task_id)
    if errs:
        print(f'[fetch_accuracy] 报告不完整,拒绝落盘: {"; ".join(errs)}', file=sys.stderr)
        sys.exit(1)

    payload = build_payload(data, task_id)
    if v:
        print(f'[fetch_accuracy] 校验通过: {payload["summary"]}', file=sys.stderr)

    if a.print_only:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
        return

    os.makedirs(a.outdir, exist_ok=True)
    out = os.path.join(a.outdir, f'accuracy_report_{task_id}.json')
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(out)


if __name__ == '__main__':
    main()