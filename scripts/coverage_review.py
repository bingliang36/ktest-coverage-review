#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "requests>=2.31.0,<3",
#   "ks-aimate>=1.0.30",
# ]
#
# [tool.uv.sources]
# "ks-aimate" = { index = "kuaishou" }
#
# [[tool.uv.index]]
# name = "kuaishou"
# url = "https://pypi.corp.kuaishou.com/kuaishou/prod/+simple/"
# publish = false
# ///
"""KTest 覆盖率核验脚本。

stdout 最后一行始终输出标准 JSON。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from ks_aimate.sso_login_client import SmartSSOSession
except Exception:  # pragma: no cover
    SmartSSOSession = None

KTEST = "https://ktest.corp.kuaishou.com"
KDEV = "https://kdev.corp.kuaishou.com"
GITLAB = "https://git.corp.kuaishou.com"


class SkillError(Exception):
    def __init__(self, error: str, msg: str, code: int = 1, next_action: str = "report_error"):
        super().__init__(msg)
        self.error = error
        self.msg = msg
        self.code = code
        self.next_action = next_action


def now_ms() -> int:
    return int(time.time() * 1000)


def emit(ok: bool, code: int, msg: str, error: Optional[str] = None,
         data: Optional[Dict[str, Any]] = None, start_ms: Optional[int] = None,
         next_action: str = "done") -> None:
    obj = {
        "ok": ok,
        "code": code,
        "error": error,
        "msg": msg,
        "data": data or {},
        "next_action": next_action,
        "duration_ms": now_ms() - (start_ms or now_ms()),
    }
    print(json.dumps(obj, ensure_ascii=False))


def ensure_out(path: str) -> Path:
    out = Path(path)
    if not str(out).startswith("tmp/") and str(out) != "tmp":
        raise SkillError("OUTPUT_ERROR", "输出目录必须位于 tmp/ 下", 1, "choose_tmp_output")
    out.mkdir(parents=True, exist_ok=True)
    return out


class InternalClient:
    def __init__(self):
        if SmartSSOSession is None:
            raise SkillError("SSO_AUTH_FAILED", "无法导入 SmartSSOSession，请确认运行环境已安装 ks-aimate", 2, "retry_after_environment_ready")
        self.session = SmartSSOSession()

    def request_json(self, method: str, url: str, **kwargs) -> Dict[str, Any]:
        try:
            resp = self.session.request(method, url, **kwargs)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            raise SkillError("API_ERROR", f"接口请求失败: {url}: {exc}", 1, "report_api_failure") from exc
        status = data.get("status")
        code = data.get("code")
        if status not in (None, 0, 200) or code not in (None, 0, 200):
            raise SkillError("API_ERROR", f"接口业务失败: {url}: {data}", 1, "report_api_failure")
        return data

    def get_data(self, base: str, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        data = self.request_json("GET", base + path, params=params)
        return data.get("data")

    def get_response(self, base: str, path: str, params: Optional[Dict[str, Any]] = None):
        url = base + path
        try:
            resp = self.session.request("GET", url, params=params)
            resp.raise_for_status()
            return resp
        except Exception as exc:
            raise SkillError("API_ERROR", f"接口请求失败: {url}: {exc}", 1, "report_api_failure") from exc


class KTestClient:
    def __init__(self, http: InternalClient):
        self.http = http

    def detail(self, cid: int) -> Dict[str, Any]:
        return self.http.get_data(KTEST, "/api/coverage/getCoverageReportDetail", {"id": cid})

    def status(self, cid: int) -> Dict[str, Any]:
        return self.http.get_data(KTEST, "/api/coverage/getReportStatus", {"cid": cid})

    def message_list(self, cid: int, node_id: Optional[int]) -> Dict[str, Any]:
        return self.http.get_data(KTEST, "/api/coverage/getCoverageMessageList", {
            "cid": cid,
            "id": "" if node_id is None else node_id,
            "searchWord": "",
            "elementType": "",
            "change": 1,
            "cover": "",
            "isInterface": "",
        })

    def method_detail(self, cid: int, node_id: int) -> Dict[str, Any]:
        return self.http.get_data(KTEST, "/api/coverage/getCoverageMethodDetail", {"cid": cid, "id": node_id})

    def summary(self, cid: int, kind: str) -> Dict[str, Any]:
        return self.http.get_data(KTEST, f"/api/coverage/change/get{kind}Info", {"id": cid})

    def threshold(self, project_id: int) -> Any:
        return self.http.get_data(KTEST, "/api/coverage/config/getConfig", {
            "projectId": project_id,
            "key": "incremental_coverage_expectation",
            "active": 1,
        })


def line_stats(line_list: List[Dict[str, Any]]) -> Dict[str, Any]:
    st = {
        "executable": 0, "ok": 0, "miss": 0, "part": 0,
        "diff": 0, "diffMiss": 0, "diffPart": 0, "diffOk": 0,
        "branch_total": 0, "branch_ok": 0, "branch_miss": 0, "branch_part": 0,
    }
    for row in line_list or []:
        covered = row.get("covered")
        diff = row.get("diff") == 1
        if covered in (1, 2, 3):
            st["executable"] += 1
        if covered == 1:
            st["miss"] += 1
        elif covered == 2:
            st["ok"] += 1
        elif covered == 3:
            st["part"] += 1
        if diff:
            st["diff"] += 1
            if covered == 1:
                st["diffMiss"] += 1
            elif covered == 2:
                st["diffOk"] += 1
            elif covered == 3:
                st["diffPart"] += 1
        # 分支覆盖率：lineList 的 branch=1 表示该行含分支，
        # 但未提供"该行未覆盖分支数"明细(cl 两个 cid 实测均为空)。
        # 用 covered 状态近似: covered=1 未覆盖、=2 已覆盖、=3 部分覆盖。
        # 这是"行覆盖率 100% 但分支只走了一半"缺口的唯一信号。
        if row.get("branch") == 1:
            st["branch_total"] += 1
            if covered == 1:
                st["branch_miss"] += 1
            elif covered == 2:
                st["branch_ok"] += 1
            elif covered == 3:
                st["branch_part"] += 1
    return st


def walk_classes(client: KTestClient, cid: int) -> List[Dict[str, Any]]:
    classes: List[Dict[str, Any]] = []

    def dfs(node_id: Optional[int], chain: List[str]):
        data = client.message_list(cid, node_id)
        for item in data.get("list", []) or []:
            name = item.get("element") or item.get("name") or str(item.get("id"))
            typ = item.get("type")
            nid = item.get("id")
            next_chain = chain + [name]
            if typ == 5:
                classes.append({"id": nid, "className": name, "chain": next_chain, "raw": item})
            elif item.get("goTarget") or typ in (2, 3, 4):
                dfs(nid, next_chain)

    dfs(None, [])
    return classes


def completeness_errors(report: Dict[str, Any], files: List[Dict[str, Any]], aggregate: Dict[str, Any]) -> List[str]:
    """抓取完整性校验。返回错误列表;为空表示通过。

    硬规则: 抓取层不产出"可能不完整"的数据。宁可失败并报告缺什么,
    也不输出一份看起来正常、实际缺文件的 JSON。

    口径说明(实测 cid=9735795 / 9773542 反推):
    - report.coveredLines / missedLines / totalLines / coverage 是【增量】口径,
      等于 aggregate 的 diff / diffMiss / diff / diffLineCoverage。
    - aggregate.executable/ok/miss/part 是【全量】口径, 由 lineList covered 1/2/3 求和。
    - files[].totalLines 是文件真实总行数; lineList 只含报告关注行, 二者不等是正常的。
    """
    errs: List[str] = []

    report_total = report.get("totalLines")
    report_missed = report.get("missedLines")
    if report_total is not None:
        if aggregate.get("diff") != report_total:
            errs.append(
                f"增量行数对账失败: 报告声明 totalLines={report_total}, 遍历统计 diff={aggregate.get('diff')}"
            )
    if report_missed is not None:
        if aggregate.get("diffMiss") != report_missed:
            errs.append(
                f"增量未覆盖行对账失败: 报告声明 missedLines={report_missed}, 遍历统计 diffMiss={aggregate.get('diffMiss')}"
            )

    # 文件级对账: 树遍历按 class 节点收集, 无法拿到服务端声明的文件总数,
    # 只能做"遍历结果自洽"校验: 每个文件必须有 lineList 和 fileContent,
    # 否则该文件的数据不完整, 不能静默通过。
    for f in files:
        if not f.get("lineList"):
            errs.append(f"文件 {f.get('className')} 缺少 lineList, 覆盖率数据不完整")
        if not f.get("fileContent"):
            errs.append(f"文件 {f.get('className')} 缺少 fileContent, 源码数据不完整")

    return errs


def coverage_cmd(args: argparse.Namespace, start_ms: int) -> None:
    cid = args.cid
    if not cid:
        raise SkillError("MISSING_ARGUMENT", "缺少 --cid", 2, "ask_for_cid")
    out = ensure_out(args.out or f"tmp/ktest-coverage-{cid}")
    http = InternalClient()
    client = KTestClient(http)
    report = client.detail(cid)
    classes = walk_classes(client, cid)
    files = []
    aggregate = {"executable": 0, "ok": 0, "miss": 0, "part": 0, "diff": 0, "diffMiss": 0, "diffPart": 0, "diffOk": 0, "branch_total": 0, "branch_ok": 0, "branch_miss": 0, "branch_part": 0}
    for cls in classes:
        detail = client.method_detail(cid, int(cls["id"]))
        st = line_stats(detail.get("lineList") or [])
        for k in aggregate:
            aggregate[k] += st[k]
        files.append({
            "classId": cls["id"],
            "className": cls["className"],
            "chain": cls["chain"],
            "fileName": detail.get("fileName"),
            "fileFullPath": detail.get("fileFullPath"),
            "fullPath": detail.get("fullPath"),
            "stats": st,
            "lineList": detail.get("lineList") or [],
            "fileContent": detail.get("fileContent"),
        })
    aggregate["fileCount"] = len(files)
    aggregate["lineCoverage"] = round(aggregate["ok"] / aggregate["executable"], 4) if aggregate["executable"] else 0
    aggregate["diffLineCoverage"] = round((aggregate["diff"] - aggregate["diffMiss"]) / aggregate["diff"], 4) if aggregate["diff"] else 0
    aggregate["branchLineCoverage"] = round(
        (aggregate["branch_ok"] + aggregate["branch_part"]) / aggregate["branch_total"], 4
    ) if aggregate["branch_total"] else None

    # —— 完整性断言: 不完整就 abort, 不落盘、不 emit ok ——
    errs = completeness_errors(report, files, aggregate)
    if errs:
        raise SkillError(
            "INCOMPLETE_COVERAGE_DATA",
            "覆盖率数据不完整: " + "; ".join(errs),
            1,
            "report_incomplete_data",
        )

    payload = {
        "cid": cid,
        "source": {"ktest": f"{KTEST}/web/cov/reportDetail?cid={cid}"},
        "report": report,
        "files": files,
        "aggregate": aggregate,
    }
    out_file = out / f"coverage_{cid}.json"
    out_file.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    emit(True, 0, "覆盖率数据已拉取", data={"output": str(out_file), "aggregate": aggregate}, start_ms=start_ms)


def verify_cmd(args: argparse.Namespace, start_ms: int) -> None:
    cid = args.cid
    if not cid:
        raise SkillError("MISSING_ARGUMENT", "缺少 --cid", 2, "ask_for_cid")
    out = ensure_out(args.out or f"tmp/ktest-verify-{cid}")
    http = InternalClient()
    client = KTestClient(http)
    detail = client.detail(cid)
    root = client.message_list(cid, None)
    first = (root.get("list") or [{}])[0]
    first_id = first.get("id")
    child = client.message_list(cid, first_id) if first_id else {"list": []}
    child_first = (child.get("list") or [{}])[0]
    method_candidate = child_first.get("id") or first_id
    method_detail = client.method_detail(cid, int(method_candidate)) if method_candidate else {}
    lines = [
        f"# KTest 路由与接口核验 - cid={cid}",
        "",
        "## 结论",
        "",
        "`/web/cov/...` 是前端页面路由；页面背后的数据接口是 `/api/coverage/...`。",
        "",
        "## 已验证接口",
        "",
        f"- `/api/coverage/getCoverageReportDetail?id={cid}` -> `{detail.get('name')}`",
        f"- `/api/coverage/getCoverageMessageList?cid={cid}&id=` -> 根节点 `{first.get('element')}` id={first_id}",
        f"- `/api/coverage/getCoverageMessageList?cid={cid}&id={first_id}` -> 子节点数量 {len(child.get('list') or [])}",
        f"- `/api/coverage/getCoverageMethodDetail?cid={cid}&id={method_candidate}` -> 文件 `{method_detail.get('fileName')}`",
        "",
        "## 前端路由示例",
        "",
        f"- `{KTEST}/web/cov/reportDetail?cid={cid}`",
        f"- `{KTEST}/web/cov/reportDetail?cid={cid}&id={first_id}&type=2`",
        f"- `{KTEST}/web/cov/fileReport?cid={cid}&id={method_candidate}&type=2`",
    ]
    out_file = out / f"verification_{cid}.md"
    out_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    emit(True, 0, "接口映射核验完成", data={"output": str(out_file), "report": detail.get("name")}, start_ms=start_ms)


class KDevClient:
    def __init__(self, http: InternalClient):
        self.http = http

    def feature_detail(self, feature_id: str) -> Dict[str, Any]:
        return self.http.get_data(KDEV, "/api/kdev/feature/detail", {"id": feature_id})

    def relation_team_list(self, feature_id: str) -> Dict[str, Any]:
        return self.http.get_data(KDEV, "/api/kdev/workbench/v2/feature/relation/team/list", {
            "bizType": "feature",
            "bizId": feature_id,
        })

    def pass_pipeline(self, feature_id: str, task_id: int) -> Dict[str, Any]:
        """拉取指定 feature 关联任务的准出流水线。

        task_id 必须来自本次输入或 feature 详情动态解析，不能在脚本中写死。
        线上实测表明该接口主要按 taskId 返回流水线，sourceId 不能作为唯一归属依据。
        """
        return self.http.get_data(KDEV, "/api/artemis/task/pass/pipeline", {
            "sourceId": feature_id,
            "relationType": 3,
            "testType": 1,
            "taskId": task_id,
            "disableCache": "true",
        })

    def job_meta(self, job_id: int) -> Dict[str, Any]:
        return self.http.get_data(KDEV, "/api/kdev/pipeline/pipelineJobLog", {"id": job_id})

    def job_log_download(self, job_id: int) -> str:
        """优先使用全量日志下载接口。失败时抛 SkillError，由调用方降级分页接口。"""
        resp = self.http.get_response(KDEV, "/api/kdev/pipeline/job/log/download", {"id": job_id})
        content_type = (resp.headers.get("content-type") or "").lower()
        text = resp.text or ""
        looks_json = "application/json" in content_type or text.lstrip().startswith("{")
        if looks_json:
            try:
                data = resp.json()
            except Exception as exc:
                raise SkillError("API_ERROR", f"job {job_id} 全量日志下载返回非法 JSON: {exc}", 1, "fallback_to_stream_log") from exc
            if isinstance(data, dict):
                status = data.get("status")
                code = data.get("code")
                if status not in (None, 0, 200) or code not in (None, 0, 200):
                    raise SkillError("API_ERROR", f"job {job_id} 全量日志下载业务失败: {data}", 1, "fallback_to_stream_log")
                payload = data.get("data")
                if isinstance(payload, str):
                    return payload
            raise SkillError("API_ERROR", f"job {job_id} 全量日志下载未返回日志文本: {data}", 1, "fallback_to_stream_log")
        return resp.content.decode(resp.encoding or "utf-8", errors="replace")

    def job_log_stream(self, job_id: int, error: bool = False, max_pages: int = 200) -> str:
        """分页读取日志流，直到 hasMore=false。

        KDev 日志接口返回 {content, offset, hasMore}；老版本只取 start=0 会截断。
        """
        path = "/api/kdev/pipeline/pipelineJobLog/errorLog" if error else "/api/kdev/pipeline/pipelineJobLog/log"
        start = 0
        parts: List[str] = []
        seen = set()
        for _ in range(max_pages):
            data = self.http.get_data(KDEV, path, {"id": job_id, "start": start}) or {}
            content = data.get("content") or ""
            if content:
                parts.append(content)
            has_more = bool(data.get("hasMore"))
            next_offset = data.get("offset")
            if not has_more:
                break
            try:
                next_start = int(next_offset)
            except (TypeError, ValueError):
                next_start = start + len(content.encode("utf-8"))
            if next_start in seen or next_start <= start:
                raise SkillError("INCOMPLETE_KDEV_LOG", f"job {job_id} 日志分页 offset 未前进，拒绝输出截断日志", 1, "retry_or_use_download")
            seen.add(next_start)
            start = next_start
        else:
            raise SkillError("INCOMPLETE_KDEV_LOG", f"job {job_id} 日志超过 {max_pages} 页，拒绝输出可能截断的数据", 1, "retry_or_use_download")
        return "".join(parts)

    def job_log(self, job_id: int, error: bool = False) -> str:
        if not error:
            try:
                return self.job_log_download(job_id)
            except SkillError:
                return self.job_log_stream(job_id, error=False)
        return self.job_log_stream(job_id, error=True)


def parse_feature_id(url: str) -> str:
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    fid = q.get("id", [None])[0]
    if not fid:
        raise SkillError("INVALID_URL", "KDev feature URL 中未找到 id 参数", 2, "ask_for_valid_url")
    return fid


def parse_task_id_from_url(url: str) -> Optional[int]:
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    for key in ("taskId", "task_id", "testTaskId"):
        val = q.get(key, [None])[0]
        if val and str(val).isdigit():
            return int(val)
    return None


def _walk_values(obj: Any):
    if isinstance(obj, dict):
        for value in obj.values():
            yield value
            yield from _walk_values(value)
    elif isinstance(obj, list):
        for item in obj:
            yield item
            yield from _walk_values(item)


def related_task_summary(relation_teams: Optional[Dict[str, Any]]) -> List[str]:
    if not relation_teams:
        return []
    rows: List[Dict[str, Any]] = []
    for group in ("devBranchTeams", "featureTeams", "releaseTeams"):
        for item in relation_teams.get(group) or []:
            if isinstance(item, dict) and item.get("taskId"):
                row = dict(item)
                row["_group"] = group
                rows.append(row)
    rows.sort(key=lambda x: x.get("createTime") or 0, reverse=True)
    return [str(x.get("taskId")) for x in rows]


def infer_task_id(feature_detail: Dict[str, Any], feature_url: str, explicit_task_id: Optional[int],
                  relation_teams: Optional[Dict[str, Any]] = None) -> int:
    """解析本次 feature 对应的 Artemis 内部 Long taskId。

    来源优先级: CLI 显式参数 > URL query > feature/detail 返回字段 > 关联任务列表中的数字 taskId。
    KDev 关联 Team 任务号常为 T123...，不能直接传给 pass/pipeline；只作为提示。
    """
    if explicit_task_id:
        return explicit_task_id
    from_url = parse_task_id_from_url(feature_url)
    if from_url:
        return from_url
    candidates: List[int] = []
    for key in ("taskId", "testTaskId", "artemisTaskId", "qaTaskId", "passTaskId"):
        val = feature_detail.get(key)
        if isinstance(val, int):
            candidates.append(val)
        elif isinstance(val, str) and val.isdigit():
            candidates.append(int(val))
    for value in _walk_values(feature_detail):
        if isinstance(value, dict):
            for key in ("taskId", "testTaskId", "artemisTaskId", "qaTaskId", "passTaskId"):
                val = value.get(key)
                if isinstance(val, int):
                    candidates.append(val)
                elif isinstance(val, str) and val.isdigit():
                    candidates.append(int(val))
    if relation_teams:
        for value in _walk_values(relation_teams):
            if isinstance(value, dict):
                val = value.get("taskId")
                if isinstance(val, int):
                    candidates.append(val)
                elif isinstance(val, str) and val.isdigit():
                    candidates.append(int(val))
    unique = []
    for val in candidates:
        if val not in unique:
            unique.append(val)
    if len(unique) == 1:
        return unique[0]
    if len(unique) > 1:
        raise SkillError(
            "AMBIGUOUS_TASK_ID",
            f"feature 详情中发现多个 taskId 候选 {unique}，无法判断本次应使用哪一个；请通过 --task-id 显式指定。",
            2,
            "ask_for_task_id",
        )
    related = related_task_summary(relation_teams)
    hint = f"；已自动发现关联 Team 任务号: {', '.join(related)}，但它不是 pass/pipeline 需要的 Artemis 内部 Long taskId" if related else ""
    raise SkillError(
        "MISSING_TASK_ID",
        "未能从 KDev feature 链接、feature 详情或关联任务列表解析到 Artemis 内部 Long taskId。禁止使用固定示例 taskId；请提供带 taskId 的链接或通过 --task-id 指定" + hint + "。",
        2,
        "ask_for_task_id",
    )


def safe_name(name: str) -> str:
    return re.sub(r"[\\/:*?\"<>|\s]+", "_", name or "job")


def kdev_cmd(args: argparse.Namespace, start_ms: int) -> None:
    if not args.feature_url:
        raise SkillError("MISSING_ARGUMENT", "缺少 --feature-url", 2, "ask_for_feature_url")
    fid = parse_feature_id(args.feature_url)
    out = ensure_out(args.out or f"tmp/kdev-feature-{fid}")
    logs_dir = out / "logs"
    logs_dir.mkdir(exist_ok=True)
    http = InternalClient()
    client = KDevClient(http)
    feature_detail = client.feature_detail(fid)
    relation_teams = client.relation_team_list(fid)
    task_id = infer_task_id(feature_detail, args.feature_url, args.task_id, relation_teams)
    data = client.pass_pipeline(fid, task_id)
    items = data.get("list") or []
    if not items:
        raise SkillError("API_ERROR", f"feature {fid} / taskId {task_id} 没有准出流水线数据", 1, "report_no_pipeline")
    branch = items[0]
    expected_branch = args.expected_branch
    if expected_branch and branch.get("branch") != expected_branch:
        raise SkillError(
            "KDEV_DATA_MISMATCH",
            f"KDev 返回分支 {branch.get('branch')} 与期望分支 {expected_branch} 不一致，拒绝落盘；请确认 taskId 是否属于该 feature。",
            1,
            "provide_correct_task_id_or_branch",
        )
    jobs = []
    for pipe in branch.get("pipelineList") or []:
        plog = pipe.get("pipelineLog") or {}
        for job in plog.get("jobLogList") or []:
            jid = int(float(job.get("id")))
            name = job.get("name") or str(jid)
            meta = client.job_meta(jid)
            content = client.job_log(jid, error=False)
            try:
                err = client.job_log(jid, error=True)
                err_error = None
            except SkillError as exc:
                err = ""
                err_error = {"error": exc.error, "msg": exc.msg, "next_action": exc.next_action}
            log_file = logs_dir / f"{jid}_{safe_name(name)}.log"
            log_file.write_text(content, encoding="utf-8")
            err_file = None
            if err:
                err_file = logs_dir / f"{jid}_{safe_name(name)}.error.log"
                err_file.write_text(err, encoding="utf-8")
            jobs.append({"jobLogId": jid, "name": name, "status": job.get("statusDesc") or job.get("status"), "meta": meta, "log": str(log_file), "errorLog": str(err_file) if err_file else None, "errorLogFetchError": err_error})
    payload = {"featureId": fid, "taskId": task_id, "featureDetail": feature_detail, "relationTeams": relation_teams, "branch": branch, "jobs": jobs}
    out_file = out / f"kdev_feature_{fid}.json"
    out_file.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    emit(True, 0, "KDev 日志已拉取", data={"output": str(out_file), "jobCount": len(jobs), "logsDir": str(logs_dir)}, start_ms=start_ms)


def git_clone_cmd(git_url: str, branch: Optional[str], dest: Path) -> Dict[str, Any]:
    token = os.environ.get("GITLAB_TOKEN")
    if not token:
        raise SkillError("GIT_AUTH_FAILED", "未设置 GITLAB_TOKEN，无法拉取 GitLab 代码", 2, "ask_for_git_token_or_skip")
    if dest.exists():
        return {"codeDir": str(dest), "reused": True}
    env = os.environ.copy()
    env["GL_T"] = token
    cmd = [
        "git",
        "-c", "credential.helper=!f(){ echo \"username=oauth2\"; echo \"password=$GL_T\"; };f",
        "clone",
    ]
    if branch:
        cmd += ["--branch", branch]
    cmd += [git_url, str(dest)]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise SkillError("GIT_AUTH_FAILED", f"Git clone 失败: {r.stderr[-500:]}", 2, "ask_for_git_credential_or_skip_code")
    return {"codeDir": str(dest), "reused": False}


def require_remote_agent(start_ms: int) -> Optional[int]:
    if not os.environ.get("MYFLICKER_REMOTE_AGENT_METHOD"):
        emit(False, 2, "该脚本可能长时间拉取内网数据，请通过 call_remote_agent(method=CALLBACK) 执行", error="REMOTE_AGENT_REQUIRED", start_ms=start_ms, next_action="RETRY_WITH_CALL_REMOTE_AGENT")
        return 2
    return None


def check_cmd(args: argparse.Namespace, start_ms: int) -> None:
    """四样数据完整性检查: 代码 / KDev 日志 / KTest 覆盖率 / 精准测试报告。

    不重新拉取,只检查落盘产物是否齐全且关键字段完整。
    任何一样缺失/不完整 => 明确报告"未获取 xxx",绝不编造。
    """
    root = Path(args.data_root)
    problems: List[str] = []
    ktest_branch = None
    ktest_commit = None
    kdev_branch = None
    kdev_commit = None
    acc_branch = None

    # 1) KTest 覆盖率 (coverage_<cid>.json)
    ktest = None
    if args.cid:
        ktest = root / "coverage" / f"coverage_{args.cid}.json"
    if ktest and ktest.exists():
        try:
            d = json.loads(ktest.read_text(encoding="utf-8"))
            report = d.get("report") or {}
            ktest_branch = report.get("branch")
            ktest_commit = report.get("commitId")
            agg = d.get("aggregate") or {}
            ok = d.get("report") and (agg.get("diff") is not None) and d.get("files")
            ktest_status = "完整 ✓" if ok else "不完整 ✗"
            if not ok:
                problems.append(f"KTest 覆盖率 {ktest} 缺 report/aggregate/files")
        except Exception as e:
            ktest_status = f"解析失败 ✗ ({e})"
            problems.append(f"KTest 覆盖率 {ktest} 无法解析: {e}")
    elif ktest and args.cid:
        ktest_status = "未获取 ✗ (缺 coverage_<cid>.json)"
        problems.append(f"未获取 KTest 覆盖率: 运行 coverage --cid {args.cid}")
    else:
        ktest_status = "跳过 (未指定 --cid)"

    # 2) KDev 日志 (kdev_feature_<fid>.json + logs/)
    kdev = None
    if args.feature_id:
        kdev = root / "kdev" / str(args.feature_id)
    if kdev and kdev.exists():
        logs = list((kdev / "logs").glob("*.log")) if (kdev / "logs").exists() else []
        meta = kdev / "feature.json"
        if not meta.exists():
            alt = kdev / f"kdev_feature_{args.feature_id}.json"
            meta = alt if alt.exists() else meta
        ok = meta.exists() and len(logs) > 0 and all(f.stat().st_size > 0 for f in logs)
        if ok:
            try:
                md = json.loads(meta.read_text(encoding="utf-8"))
                if not md.get("taskId") or not md.get("featureDetail"):
                    ok = False
                    problems.append(f"KDev 元信息 {meta} 缺 taskId/featureDetail,无法证明流水线归属本 feature；请用新版 kdev 命令重新拉取")
                b = md.get("branch") or {}
                if "branch" in b and isinstance(b.get("branch"), str):
                    kdev_branch = b.get("branch")
                    kdev_commit = b.get("commitId")
                elif isinstance(md.get("branch"), str):
                    kdev_branch = md.get("branch")
                    kdev_commit = md.get("commitId")
            except Exception as e:
                ok = False
                problems.append(f"KDev 元信息 {meta} 无法解析: {e}")
        kdev_status = f"完整 ✓ ({len(logs)} 份日志)" if ok else "不完整 ✗"
        if not ok:
            problems.append(f"KDev 日志 {kdev} 缺 feature.json/kdev_feature_<id>.json 或 logs 为空/存在空日志")
    elif kdev and args.feature_id:
        kdev_status = "未获取 ✗ (缺 data/kdev/<featureId>/)"
        problems.append(f"未获取 KDev 日志: 运行 kdev --feature-url <URL> 或 kdev_fetch.py")
    else:
        kdev_status = "跳过 (未指定 --feature-id)"

    # 3) 精准测试报告 (accuracy_report_<taskId>.json)
    acc = None
    if args.task_id:
        acc = root / "coverage" / f"accuracy_report_{args.task_id}.json"
    if acc and acc.exists():
        try:
            d = json.loads(acc.read_text(encoding="utf-8"))
            tr = d.get("testReport") or {}
            acc_branch = tr.get("branch")
            ok = tr.get("testMethodList") is not None and tr.get("branch")
            acc_status = "完整 ✓" if ok else "不完整 ✗"
            if not ok:
                problems.append(f"精准测试报告 {acc} 缺 testReport/testMethodList/branch")
        except Exception as e:
            acc_status = f"解析失败 ✗ ({e})"
            problems.append(f"精准测试报告 {acc} 无法解析: {e}")
    elif acc and args.task_id:
        acc_status = "未获取 ✗ (缺 accuracy_report_<taskId>.json)"
        problems.append(f"未获取精准测试报告: 运行 fetch_accuracy.py --task-id {args.task_id}")
    else:
        acc_status = "跳过 (未指定 --task-id)"

    # 4) 代码 (repos 目录下已 clone 且 HEAD 与 KTest 报告 commit 对齐)
    code_status = "跳过 (未指定 --code-dir)"
    if args.code_dir:
        cd = Path(args.code_dir)
        if cd.exists() and (cd / ".git").exists():
            r = subprocess.run(["git", "-C", str(cd), "rev-parse", "HEAD"],
                               capture_output=True, text=True, timeout=60)
            if r.returncode == 0:
                head = r.stdout.strip()
                commit = None
                if ktest and ktest.exists():
                    try:
                        d = json.loads(ktest.read_text(encoding="utf-8"))
                        commit = (d.get("report") or {}).get("commitId")
                    except Exception:
                        commit = None
                if commit and head != commit:
                    code_status = f"不完整 ✗ (代码 HEAD {head[:8]} ≠ 报告 commit {commit[:8]})"
                    problems.append(f"代码 HEAD({head[:8]}) 与 KTest 报告 commit({commit[:8]}) 不对齐")
                else:
                    code_status = f"完整 ✓ ({head[:8]})"
            else:
                code_status = "不完整 ✗ (git rev-parse 失败)"
                problems.append(f"代码目录 {cd} 无法读取 HEAD")
        else:
            code_status = "未获取 ✗ (目录不存在)"
            problems.append(f"未获取代码: {cd} 不存在,请先 clone")

    if kdev_branch and ktest_branch and kdev_branch != ktest_branch:
        problems.append(f"KDev 分支({kdev_branch}) 与 KTest 覆盖率分支({ktest_branch}) 不一致,拒绝分析")
        kdev_status += "；分支不一致 ✗"
    if acc_branch and ktest_branch and acc_branch != ktest_branch:
        problems.append(f"精准测试报告分支({acc_branch}) 与 KTest 覆盖率分支({ktest_branch}) 不一致,拒绝分析")
        acc_status += "；分支不一致 ✗"
    if kdev_commit and ktest_commit and kdev_commit != ktest_commit:
        problems.append(f"KDev commit({kdev_commit[:8]}) 与 KTest 覆盖率 commit({ktest_commit[:8]}) 不一致,拒绝分析")
        kdev_status += "；commit 不一致 ✗"

    lines = [
        "# 四样数据完整性检查",
        "",
        "| 数据 | 状态 | 说明 |",
        "|---|---|---|",
        f"| 1. KTest 覆盖率 | {ktest_status} | {ktest} |",
        f"| 2. KDev 日志 | {kdev_status} | {kdev} |",
        f"| 3. 精准测试报告 | {acc_status} | {acc} |",
        f"| 4. 代码 | {code_status} | {args.code_dir} |",
        "",
        "## 结论",
        "",
    ]
    if problems:
        lines += ["以下数据未获取或不完整(不允许编造):", ""]
        lines += [f"- {p}" for p in problems]
        lines += ["", "按上面的提示补拉后重新检查。"]
        ok = False
    else:
        lines += ["四样数据齐全且完整,可以进入覆盖率分析。"]
        ok = True

    out = root / f"data_check_{start_ms}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    emit(ok, 0 if ok else 1, "四样数据完整性检查完成" if ok else "存在未获取/不完整的数据",
         data={"output": str(out), "problems": problems}, start_ms=start_ms)


def main() -> int:
    start_ms = now_ms()
    remote_code = require_remote_agent(start_ms)
    if remote_code is not None:
        return remote_code
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("coverage")
    p.add_argument("--cid", type=int, required=True)
    p.add_argument("--out", default=None)
    p = sub.add_parser("verify")
    p.add_argument("--cid", type=int, required=True)
    p.add_argument("--out", default=None)
    p = sub.add_parser("kdev")
    p.add_argument("--feature-url", required=True)
    p.add_argument("--task-id", type=int, help="KDev/精准测试任务 id；必须来自本次链接或用户输入，脚本不会使用固定示例值")
    p.add_argument("--expected-branch", help="期望分支名；提供后会校验 KDev 返回分支，避免错拉其它 feature 的流水线")
    p.add_argument("--out", default=None)
    p = sub.add_parser("check")
    p.add_argument("--cid", type=int, help="KTest 报告 id")
    p.add_argument("--feature-id", type=str, help="KDev feature id")
    p.add_argument("--task-id", type=int, help="精准测试 taskId")
    p.add_argument("--code-dir", type=str, help="代码 clone 目录")
    p.add_argument("--data-root", default="data", help="数据根目录(默认 data)")
    p.add_argument("--out", default=None)
    args = parser.parse_args()
    try:
        if args.cmd == "coverage":
            coverage_cmd(args, start_ms)
        elif args.cmd == "verify":
            verify_cmd(args, start_ms)
        elif args.cmd == "kdev":
            kdev_cmd(args, start_ms)
        elif args.cmd == "check":
            check_cmd(args, start_ms)
        return 0
    except SkillError as exc:
        print(f"[error] {exc.error}: {exc.msg}", file=sys.stderr)
        emit(False, exc.code, exc.msg, error=exc.error, start_ms=start_ms, next_action=exc.next_action)
        return exc.code
    except KeyboardInterrupt:
        emit(False, 130, "用户中断", error="INTERRUPTED", start_ms=start_ms, next_action="stop")
        return 130
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        emit(False, 1, str(exc), error="UNEXPECTED_ERROR", start_ms=start_ms, next_action="report_diagnostic")
        return 1


if __name__ == "__main__":
    sys.exit(main())
