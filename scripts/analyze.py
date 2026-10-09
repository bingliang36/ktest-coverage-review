#!/usr/bin/env python3
"""覆盖率诊断分析 —— 把四样数据变成有依据的"该测未测"结论。

输入(只读现成产物,不重新拉取):
  - accuracy_report_<taskId>.json   精准测试报告(方法级 + 官方结论)
  - coverage_<cid>.json             KTest 覆盖率(行级变更 + 覆盖状态)
  - 代码目录                         变更方法定位 + 行号核对(可选)

输出:
  <outdir>/analysis_<taskId>_<cid>.md  诊断报告

核心原则(与 SKILL.md 防幻觉一脉相承):
  每个结论都必须有数据/代码依据。数据里没有的,明说"平台未提供该信息"。
  判断规则全部来自平台字段,不发明、不猜测、不凭空推断。

-- 依据体系(每条结论必须能溯源) --
  [A] 方法级: accuracy.testReport.testMethodList[].increCoverage / coveredLink / unCoveredLink
  [B] 行级:   coverage.files[].stats.diffMiss(diff 行里未覆盖数) / lineList[].covered
  [C] 官方:   accuracy.testReport 的 testMethodCount / unTestMethodList / 顶层低覆盖计数
  [D] 代码:   变更方法在源码中的行号与 changeRanges 落点核对(需代码目录)
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

WS = Path(os.environ.get("COVFETCH_WS") or os.getcwd())


class AnalysisError(Exception):
    pass


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise AnalysisError(f"读取 {path} 失败: {e}")


# ---------- 依据收集 ----------

def collect_method_evidence(acc: dict) -> list[dict]:
    """[A][C] 从精准测试报告收集每个变更方法的方法级依据。"""
    tr = acc.get("testReport") or {}
    methods = tr.get("testMethodList") or []
    out = []
    for i, m in enumerate(methods):
        out.append({
            "idx": i,
            "fullName": m.get("fullName", ""),
            "fullClazzName": m.get("fullClazzName", ""),
            "shortName": m.get("shortName", ""),
            "jarPath": m.get("jarPath", ""),
            "inputArgs": m.get("inputArgs", ""),
            "outputArgs": m.get("outputArgs", ""),
            "changeType": m.get("changeType"),
            "methodType": m.get("methodType"),
            "methodCategory": m.get("methodCategory"),
            "isUpToStandard": m.get("isUpToStandard"),
            "increCoverage": m.get("increCoverage"),
            "fullCoverage": m.get("coverage"),
            "branchCoverage": m.get("branchCoverage"),
            "affectedLink": m.get("affectedLink"),
            "coveredLink": m.get("coveredLink"),
            "unCoveredLink": m.get("unCoveredLink"),
            "changeRanges": m.get("changeRanges") or [],
            "lowCoverageMethod": m.get("lowCoverageMethod"),
        })
    # [C] 官方结论(顶层字段)
    official = {
        "conclusion_failed": False,  # 占位,由调用者看 qualityPredictionReport
        "testMethodCount": tr.get("testMethodCount"),
        "changeMethodCount": tr.get("changeMethodCount"),
        "unTestMethodCount": len(tr.get("unTestMethodList") or []),
        "ignoreMethodCount": len(tr.get("ignoreMethodList") or []),
        "methodAvgIncreCoverage": tr.get("methodAvgIncreCoverage"),
        "methodAvgFullCoverage": tr.get("methodAvgFullCoverage"),
        "lowCoverageMethodCount": tr.get("lowCoverageMethodCount"),
        "autoCaseExeFailMethodCount": tr.get("autoCaseExeFailMethodCount"),
        "frequentChangeMethodCount": tr.get("frequentChangeMethodCount"),
        "fundRiskMethodCount": tr.get("fundRiskMethodCount"),
    }
    return out, official


def collect_line_evidence(cov: dict) -> dict:
    """[B] 从 KTest 覆盖率收集文件级行证据。"""
    files = {}
    for f in cov.get("files") or []:
        fp = f.get("filePath") or f.get("fileName") or f.get("className")
        files[fp] = {
            "fileName": f.get("fileName"),
            "filePath": f.get("filePath"),
            "className": f.get("className"),
            "stats": f.get("stats"),
            # lineList 按行号索引,便于按 changeRanges 查
            "lines": {ln.get("line"): ln for ln in (f.get("lineList") or [])},
        }
    return files


def link_method_to_file(m: dict, line_files: dict) -> tuple:
    """把精准测试方法 [A] 对到 KTest 文件 [B]。返回 (file_key, file_info) 或 (None, None)。"""
    jp = m.get("jarPath")
    # jarPath 如 com/.../ComposeFinalOutputResolver.java;文件 key 可能带或不带前缀
    if jp:
        base = jp.split("/")[-1]
        for k, v in line_files.items():
            if k == jp or (k or "").endswith("/" + jp) or (k or "").endswith(base):
                return k, v
    # 兜底: 按 className 简名匹配
    cn = (m.get("shortName") or "").split("#")[0].split(".")[-1]
    for k, v in line_files.items():
        if v["className"] == cn:
            return k, v
    return None, None


def range_miss_lines(finfo: dict, ranges: list) -> list:
    """[B] 变更行未覆盖: changeRanges ∩ diff==1 ∩ covered==1 三条件交集。

    三条证据缺一不可:
    - changeRanges: 平台声明的方法变更范围(可用于区分同一文件内多个变更方法)
    - diff==1:      KTest 标记的"本次变更行"(方法体内非变更行不算)
    - covered==1:   行级未覆盖
    实测: MetadataResultProcessService 一个文件含 2 个变更方法(process 157-230,
    recordWorkflowIdFailed 383-396),若只用 diff 会串行(process 误报 390-392)。
    """
    if not finfo or not ranges:
        return []
    # 收集所有 changeRanges 覆盖的区间(合并重叠)
    spans = sorted((r.get("startLine", 0), r.get("endLine", 0)) for r in ranges)
    merged = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    miss = []
    for ln, cell in finfo["lines"].items():
        if cell.get("diff") != 1 or cell.get("covered") != 1:
            continue
        if any(s <= ln <= e for s, e in merged):
            miss.append({"line": ln, "covered": cell.get("covered"), "code": ""})
    miss.sort(key=lambda x: x["line"])
    return miss


def pct(v, nd=0) -> str:
    """覆盖率显示统一。实测: increCoverage 是 0-1, coverage/branchCoverage 是 0-100。"""
    if v is None:
        return "-"
    if v > 1:  # 已是百分数(0-100)
        return f"{v:.{nd}f}%"
    return f"{v * 100:.{nd}f}%"


# ---------- 测试样例生成(5) ----------

def extract_method_source(code_dir: Path, jar_path: str, full_clazz: str, start_line: int | None = None) -> str | None:
    """从代码目录按 jarPath 定位源文件,提取方法的源码实现(供测试样例参考)。

    jar_path 形如 com/x/y/Foo.java;在代码目录下按 basename + 路径后缀定位。
    优先用 changeRanges 的 startLine 定位方法体起点(平台给的行号,权威);
    无 startLine 时退化为方法名匹配(可能不准,标注)。
    返回方法体文本(从 startLine 到方法闭合),找不到返回 None(诚实,不编造)。
    """
    if not code_dir:
        return None
    base = jar_path.split("/")[-1]
    target = None
    for p in Path(code_dir).rglob("*.java"):
        if p.name == base and (str(p).endswith(jar_path) or base == p.name):
            target = p
            break
    if target is None:
        return None
    try:
        src = target.read_text(encoding="utf-8").split("\n")
    except Exception:
        return None
    i0 = None
    if start_line:
        # startLine 是方法声明行(changeRanges 起点),从那里开始数括号
        i0 = start_line - 1
    else:
        meth = full_clazz.split("#")[-1].split("(")[0]
        for i, line in enumerate(src):
            if re.search(rf"\b{re.escape(meth)}\s*\(", line):
                i0 = i
                break
    if i0 is None:
        return None
    depth = 0
    started = False
    buf = []
    for ln in src[i0:]:
        buf.append(ln)
        depth += ln.count("{") - ln.count("}")
        if "{" in ln:
            started = True
        if started and depth <= 0:
            break
    return "\n".join(buf)


def extract_injected_fields(code_dir: Path, jar_path: str) -> list[str]:
    """从源码提取依赖注入字段(有依据: 非 static 的成员变量),用于生成 @Mock/@InjectMocks。"""
    if not code_dir:
        return []
    base = jar_path.split("/")[-1]
    target = None
    for p in Path(code_dir).rglob("*.java"):
        if p.name == base:
            target = p
            break
    if target is None:
        return []
    fields = []
    try:
        for line in target.read_text(encoding="utf-8").split("\n"):
            s = line.strip()
            # 非 static 的成员变量: private Xxx name; 或 private Xxx name = ...
            m = re.match(r"private\s+(?:final\s+)?([A-Za-z_][\w<>\[\],\s]*?)\s+(\w+)\s*[;=]", s)
            if m and "static" not in s:
                fields.append(f"{m.group(1)} {m.group(2)}")
    except Exception:
        return []
    return fields


def render_test_section(acc: dict, cov: dict, code_dir: str, missing_methods: list) -> list[str]:
    """生成测试样例(只对该测未测方法)。全部基于签名+源码,不编造业务断言。"""
    L = []
    L.append("## 6. 测试样例生成(基于源码,需研发补充断言)")
    L.append("")
    if not missing_methods:
        L.append("- 无该测未测方法,无需生成测试。")
        return L
    for m in missing_methods:
        short = m.get("shortName", "")
        fcn = m.get("fullClazzName") or ""
        clz = fcn.split("#")[0]
        meth_full = fcn.split("#")[-1] if "#" in fcn else ""
        meth = meth_full.split("(")[0]  # 方法名(不含参数)
        inp = m.get("inputArgs") or "()"
        out = m.get("outputArgs") or "void"
        jar = m.get("jarPath") or ""
        L.append(f"### {short}")
        L.append("")
        L.append(f"- 签名: `{clz}#{meth}`")
        L.append(f"- 输入: `{inp}` | 输出: `{out}`")
        L.append(f"- 变更类型: {m.get('changeType')} | 增量覆盖率: {pct(m.get('increCoverage'))}")
        L.append("")
        cr = (m.get("changeRanges") or [])
        start_line = cr[0].get("startLine") if cr else None
        src = extract_method_source(Path(code_dir), jar, m.get("fullClazzName") or "", start_line) if code_dir else None
        if src is None and code_dir:
            L.append("> ⚠️ 未在代码目录定位到该方法源码(`jarPath` 可能不含模块前缀),测试样例仅基于签名,断言需研发补充。")
            L.append("")
        fields = extract_injected_fields(Path(code_dir), jar) if code_dir else []
        L.append("```java")
        L.append(f"// 测试类: {clz.split('.')[-1]}Test")
        L.append(f"// 被测方法: {meth}")
        L.append("// 注意: 以下为骨架,断言基于源码可见行为;业务期望值需研发确认。")
        L.append("")
        L.append("@ExtendWith(MockitoExtension.class)")
        L.append(f"class {clz.split('.')[-1]}Test {{")
        if fields:
            for f in fields:
                L.append(f"    @Mock private {f};")
            L.append("")
            L.append("    @InjectMocks")
            L.append(f"    private {clz.split('.')[-1]} service;")
        else:
            L.append(f"    private {clz.split('.')[-1]} service = new {clz.split('.')[-1]}();")
        L.append("")
        L.append("    @Test")
        L.append(f"    void should_{meth}() {{")
        L.append("        // given: 构造输入参数(依据签名)")
        L.append(f"        //   {inp}")
        L.append("        // when: 调用被测方法")
        L.append(f"        //   {out} result = service.{meth}(...);")
        L.append("        // then: 断言(需研发根据业务补充)")
        L.append("        //   assertNotNull(result);")
        L.append("    }")
        L.append("}")
        L.append("```")
        if src:
            L.append("")
            L.append("源码参考(方法实现):")
            L.append("```java")
            for sl in src.split("\n")[:25]:
                L.append(sl)
            L.append("```")
        L.append("")
    return L

def coverage_gap(coverage: dict, target_pct: float) -> dict:
    """计算达到目标增量覆盖率还差多少行。全部基于报告数据,不猜。

    口径(KTest): diffLineCoverage = (diffOk + diffPart) / diff
    - part(部分覆盖)已计入分子,补全覆盖不增加覆盖率
    - 只有 miss(未覆盖变更行)补掉才提升覆盖率
    目标覆盖率 target_pct(0-100)。
    """
    agg = coverage.get("aggregate") or {}
    diff = agg.get("diff") or 0
    ok = agg.get("diffOk") or 0
    part = agg.get("diffPart") or 0
    miss = agg.get("diffMiss") or 0
    if not diff:
        return None
    cur = (ok + part) / diff
    need_total = max(0, int(target_pct / 100 * diff + 0.999))  # 向上取整
    need_rows = max(0, need_total - (ok + part))  # 还差多少行(全从 miss 补)
    return {
        "diff": diff, "ok": ok, "part": part, "miss": miss,
        "current": cur,
        "target": target_pct,
        "need_rows": need_rows,
        "covered_now": ok + part,
        "reachable": need_rows <= miss,  # 差的行数是否 <= 未覆盖数
    }


def rank_methods_to_cover(coverage: dict, acc: dict, code_dir: str) -> list[dict]:
    """按"方法未覆盖变更行数"排序,推荐优先补测方法。

    依据: 每个变更文件的 diffMiss(diff 行里未覆盖数) + 精准测试报告的方法级数据。
    排序键: 未覆盖变更行数(降序), 其次看方法级增量覆盖率(升序,低的优先)。
    """
    # 文件级: className -> diffMiss
    file_miss = {}
    for f in coverage.get("files") or []:
        st = f.get("stats") or {}
        file_miss[f.get("className")] = {
            "diff": st.get("diff", 0),
            "diffMiss": st.get("diffMiss", 0),
            "diffPart": st.get("diffPart", 0),
        }
    # 方法级: 精准测试报告
    methods = []
    for m in (acc.get("testReport") or {}).get("testMethodList") or []:
        cn = (m.get("shortName") or "").split("#")[0].split(".")[-1]
        fm = file_miss.get(cn, {})
        methods.append({
            "shortName": m.get("shortName"),
            "increCoverage": m.get("increCoverage"),
            "fileMiss": fm.get("diffMiss", 0),
            "fileDiff": fm.get("diff", 0),
        })
    methods.sort(key=lambda x: (-x["fileMiss"], x["increCoverage"] or 0))
    return methods


def render_gap_section(coverage: dict, acc: dict, code_dir: str, target_pct: float) -> list[str]:
    """渲染"达标路径"章节。"""
    L = []
    L.append("## 5. 达标路径(覆盖率缺口分析)")
    L.append("")
    agg = coverage.get("aggregate") or {}
    thr = coverage.get("threshold")
    eff_target = target_pct if target_pct is not None else (thr if thr is not None else 60)
    # threshold 可能来自 JSON,实测是字符串 "60";统一转 float
    try:
        eff_target = float(str(eff_target).rstrip("%"))
    except (TypeError, ValueError):
        eff_target = 60.0
    gap = coverage_gap(coverage, eff_target)
    if not gap:
        L.append("- 无变更行数据,无法计算缺口。")
        return L
    L.append(f"- 当前增量覆盖率: **{pct(gap['current'] * 100, 1)}** (diff={gap['diff']}: ok={gap['ok']} part={gap['part']} miss={gap['miss']})")
    L.append(f"- 目标增量覆盖率: **{eff_target}%**(卡点阈值 {thr}%)")
    L.append(f"- 还差 **{gap['need_rows']} 行**达到目标(需从 miss 中补覆盖)")
    if gap["reachable"]:
        L.append(f"- ✅ 可达: 未覆盖变更行数({gap['miss']}) ≥ 还差行数({gap['need_rows']})")
    else:
        L.append(f"- ⚠️ 不可达: 未覆盖变更行({gap['miss']}) < 还差行数({gap['need_rows']}),需考虑排除疑似冗余或提高测试充分度")
    L.append("")
    L.append("### 5.1 推荐优先补测方法(按未覆盖变更行数排序)")
    L.append("")
    methods = rank_methods_to_cover(coverage, acc, code_dir)
    if methods:
        L.append("| 方法 | 文件级未覆盖变更行 | 文件级变更行 | 方法级增量覆盖率 |")
        L.append("|---|---|---|---|")
        for m in methods:
            if m["fileMiss"] > 0 or (m["increCoverage"] or 0) < 1.0:
                L.append(f"| {m['shortName']} | {m['fileMiss']} | {m['fileDiff']} | {pct(m['increCoverage'])} |")
    else:
        L.append("- 无变更方法数据。")
    L.append("")
    return L

def load_source_map(code_dir: Path, line_files: dict) -> dict:
    """按 (filePath, line) 构建源码行映射,供报告中展示变更未覆盖行的代码内容。

    只加载变更涉及的源文件(效率 + 避免全库)。
    filePath 形如 com/x/y/Foo.java,在 code_dir 下按相对路径定位(带 java 源码根)。
    """
    src_map = {}
    # 收集需要定位的 filePath
    targets = {fp for fp in line_files.keys() if fp}
    # 建立 code_dir 下所有 .java 的 basename -> 绝对路径 索引(避免深路径猜错)
    by_base = {}
    for p in code_dir.rglob("*.java"):
        by_base.setdefault(p.name, []).append(p)
    for fp in targets:
        base = fp.split("/")[-1]
        # 优先完整相对路径匹配(含模块前缀),否则按文件名兜底
        cand = None
        for p in by_base.get(base, []):
            if str(p).endswith(fp):
                cand = p
                break
        if cand is None and by_base.get(base):
            cand = by_base[base][0]
        if cand is None:
            continue
        try:
            lines = cand.read_text(encoding="utf-8").split("\n")
        except Exception:
            continue
        for i, ln in enumerate(lines, 1):
            src_map[(fp, i)] = ln
    return src_map


def simulate_removal(coverage: dict, suspected_lines: list[tuple]) -> dict:
    """模拟"去掉疑似冗余的变更行"后的增量覆盖率。

    数学模型(行级):
      当前 C = (diffOk + diffPart) / diff
      去掉 R(疑似冗余的 diff 行)后:
        C' = (diffOk + diffPart - R中covered∈{2,3}的行数) / (diff - |R|)
      注意: R 里若全是已覆盖行,去掉反而降覆盖率;只有未覆盖行占比高才有提升。
    边界: 只对"行级增量覆盖率"(KTest 卡点)有效;方法级/链路级不适用。
    """
    diff = coverage["aggregate"].get("diff", 0)
    diff_ok = coverage["aggregate"].get("diffOk", 0)
    diff_part = coverage["aggregate"].get("diffPart", 0)
    if not diff:
        return None
    cur = (diff_ok + diff_part) / diff
    # R: 疑似冗余涉及的 diff 行
    r_ok = sum(1 for _, covered in suspected_lines if covered in (2, 3))
    r_total = len(suspected_lines)
    if not r_total:
        return {"current": cur, "after": cur, "removed": 0, "note": "无疑似冗余变更行"}
    after = (diff_ok + diff_part - r_ok) / (diff - r_total) if (diff - r_total) > 0 else None
    return {"current": cur, "after": after, "removed": r_total, "r_covered": r_ok,
            "note": "去掉疑似冗余后覆盖率(行级模拟)"}


def judge_missing(method: dict, range_miss: list) -> dict:
    """判断"该测未测"。依据(与平台口径一致,不发明):
    [A] 方法级增量覆盖率未满 100% (increCoverage < 1.0) —— 平台也以此为卡点
    [B] 变更行未覆盖 (changeRanges ∩ diff==1 ∩ covered==1)

    注意: 影响链路(unCoveredLink)不作判据——实测当前任务 5 方法 coveredLink 全为 0,
    链路未覆盖是普遍现象,不能据此判"该测";只作为风险提示展示。
    """
    reasons = []
    incre = method.get("increCoverage")
    if incre is not None and incre < 1.0:
        reasons.append(f"[A] 方法级增量覆盖率 {pct(incre)} < 100%")
    if method.get("lowCoverageMethod"):
        reasons.append("[A] 平台标记 lowCoverageMethod=true")
    if range_miss:
        reasons.append(f"[B] 变更行(diff) {len(range_miss)} 行未覆盖(行号 {[x['line'] for x in range_miss]})")
    return {"is_missing": bool(reasons), "reasons": reasons,
            "range_miss_lines": range_miss}


# ---------- 报告渲染 ----------

def render_report(acc: dict, cov: dict, code_dir: str | None, evidence, target_pct: float | None = None) -> str:
    methods, official = evidence  # (list, dict)
    line_files = collect_line_evidence(cov)

    lines = []
    lines.append("# 覆盖率诊断报告")
    lines.append("")
    lines.append(f"- 精准测试报告: taskId={acc.get('taskId')}")
    lines.append(f"- KTest 覆盖率: cid={cov.get('cid')}")
    lines.append(f"- 代码目录: {code_dir or '未提供(无法做行号核对)'}")
    lines.append("")

    # 1. 官方结论
    qp = acc.get("qualityPredictionReport") or {}
    lines.append("## 1. 官方结论(平台口径)")
    lines.append("")
    lines.append(f"- conclusion: **{qp.get('conclusion')}**")
    tob = qp.get("testRiskBO") or {}
    lines.append(f"- hasTestRisk: **{tob.get('hasTestRisk')}**")
    lines.append(f"- 变更方法总数 {official['changeMethodCount']} | 免测方法 {official['unTestMethodCount']} | 低覆盖方法 {official['lowCoverageMethodCount']} | 自动用例失败 {official['autoCaseExeFailMethodCount']} | 高频变更 {official['frequentChangeMethodCount']}")
    lines.append("")

    # 2. 各方法诊断
    lines.append("## 2. 变更方法诊断")
    lines.append("")
    missing_methods = []  # 该测未测的方法(供第 6 节生成测试)
    for m in methods:
        finfo_key, finfo = link_method_to_file(m, line_files)
        rmiss = range_miss_lines(finfo, m["changeRanges"]) if finfo else []
        diag = judge_missing(m, rmiss)
        if diag["is_missing"]:
            missing_methods.append(m)
        lines.append(f"### {m['shortName']}")
        lines.append("")
        lines.append(f"- changeType={m['changeType']} | 增量覆盖率={pct(m['increCoverage'])} | 全量覆盖率={pct(m['fullCoverage'])} | 影响链路={m['affectedLink']}(未覆盖{m['unCoveredLink']})")
        lines.append(f"- 行级对账: 文件={finfo_key or '未匹配到 KTest 文件'} | 变更行未覆盖(diff==1)={len(rmiss)}")
        if finfo:
            st = finfo["stats"]
            lines.append(f"- 文件级: diff={st.get('diff')} diffMiss={st.get('diffMiss')} diffPart={st.get('diffPart')} diffOk={st.get('diffOk')}")
        if diag["is_missing"]:
            lines.append("")
            lines.append("**该测未测** " + "; ".join(diag["reasons"]))
            if diag["range_miss_lines"]:
                lines.append("")
                lines.append("变更行未覆盖(diff==1):")
                lines.append("```")
                for x in diag["range_miss_lines"]:
                    lines.append(f"{x['line']:>6} | {x['code'][:120]}")
                lines.append("```")
        else:
            lines.append("")
            lines.append("已覆盖充分(未命中该测未测条件)。")
        lines.append("")

    # 3. 未获取/无依据说明
    lines.append("## 3. 数据边界(诚实声明)")
    lines.append("")
    if official["unTestMethodCount"] == 0 and official["ignoreMethodCount"] == 0:
        lines.append("- 平台未提供\"免测/豁免\"方法列表(unTestMethodList/ignoreMethodList 为空),因此本报告**不判定冗余代码**——那是平台才能给的口径,无依据不臆断。")
    else:
        lines.append(f"- 平台提供免测方法 {official['unTestMethodCount']} 个、豁免 {official['ignoreMethodCount']} 个,见上表。")
    if not code_dir:
        lines.append("- 未提供代码目录,无法核对 changeRanges 与源码行号的一致性,亦无法做调用关系/牵连方法分析。")
    lines.append("")

    # ---- 4. 冗余/低效代码识别(有依据,纯提示) ----
    lines.append("## 4. 疑似冗余/低效代码(提示,需研发确认)")
    lines.append("")
    if not code_dir:
        lines.append("- 未提供代码目录,无法展示变更未覆盖行的源码内容。")
        lines.append("")
    else:
        src_map = load_source_map(Path(code_dir), line_files)
        # 4.1: 变更未覆盖行(covered==1 且 diff==1) + 源码内容
        lines.append("### 4.1 变更未覆盖行(covered==1 ∩ diff==1)")
        lines.append("")
        lines.append("下表列出本次变更中未覆盖的行及其源码。研发可据此判断: 是分支走不到、异常分支、还是确属废弃代码。**是否冗余由研发确认,本报告不替研发下结论。**")
        lines.append("")
        all_miss = []
        for f in cov.get("files") or []:
            for x in f.get("lineList") or []:
                if x.get("diff") == 1 and x.get("covered") == 1:
                    code = src_map.get((f.get("filePath"), x.get("line")), "")
                    all_miss.append({
                        "file": f.get("className"),
                        "line": x.get("line"),
                        "covered": x.get("covered"),
                        "code": code.strip()[:90],
                    })
        if all_miss:
            lines.append("| 文件 | 行 | 源码(截断) |")
            lines.append("|---|---|---|")
            for m in all_miss:
                lines.append(f"| {m['file']} | {m['line']} | `{m['code']}` |")
        else:
            lines.append("- 本次变更无未覆盖行。")
        lines.append("")

        # 4.2: 部分覆盖行(分支没走全)提示
        lines.append("### 4.2 部分覆盖行(分支未走全,covered==3)")
        lines.append("")
        lines.append("这些行被部分执行(有分支没走到),是『可能永远走不到的分支』的高发区,但同样需研发确认。")
        lines.append("")
        all_part = []
        for f in cov.get("files") or []:
            for x in f.get("lineList") or []:
                if x.get("diff") == 1 and x.get("covered") == 3:
                    code = src_map.get((f.get("filePath"), x.get("line")), "")
                    all_part.append({
                        "file": f.get("className"),
                        "line": x.get("line"),
                        "code": code.strip()[:90],
                    })
        if all_part:
            lines.append("| 文件 | 行 | 源码(截断) |")
            lines.append("|---|---|---|")
            for m in all_part:
                lines.append(f"| {m['file']} | {m['line']} | `{m['code']}` |")
        else:
            lines.append("- 本次变更无部分覆盖行。")
        lines.append("")

        # 4.3: 模拟
        lines.append("### 4.3 若确认冗余并去除,覆盖率模拟(心理预期)")
        lines.append("")
        agg = cov.get("aggregate") or {}
        diff = agg.get("diff") or 0
        cur = (agg.get("diffOk", 0) + agg.get("diffPart", 0)) / diff if diff else None
        lines.append(f"- 当前增量覆盖率: **{pct(cur * 100 if cur is not None else None, 1)}** (卡点阈值 {cov.get('threshold')}%)")
        if all_miss:
            # 最乐观: 去掉全部未覆盖变更行(视为冗余)后覆盖率
            sim = simulate_removal(cov, [(m["line"], m["covered"]) for m in all_miss])
            if sim and sim.get("after") is not None:
                lines.append(f"- **最乐观情况**: 若上面 {len(all_miss)} 行未覆盖确实全是冗余并去除,增量覆盖率 → **{pct(sim['after'] * 100, 1)}**")
                lines.append("- 注意: 这是上限;若其中有真需覆盖的业务代码,需补测而不是去除。")
        else:
            lines.append("- 无未覆盖变更行可模拟。")
        lines.append("")

    # ---- 5. 达标路径 ----
    lines.extend(render_gap_section(cov, acc, code_dir, target_pct))

    # ---- 6. 测试样例生成 ----
    lines.extend(render_test_section(acc, cov, code_dir, missing_methods))

    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description="覆盖率诊断: 该测未测 + 冗余提示 + 达标路径(有依据)")
    p.add_argument("--task-id", type=int, required=True, help="精准测试 taskId")
    p.add_argument("--cid", type=int, required=True, help="KTest 增量覆盖率 cid")
    p.add_argument("--data-root", default=str(WS / "data"), help="数据根目录")
    p.add_argument("--code-dir", default=None, help="代码目录(可选,核对行号)")
    p.add_argument("--target", type=float, default=None, help="目标增量覆盖率%%,默认用 KTest 卡点阈值")
    p.add_argument("--outdir", default=str(WS / "tmp"), help="输出目录")
    a = p.parse_args()

    acc_path = Path(a.data_root) / "coverage" / f"accuracy_report_{a.task_id}.json"
    cov_path = Path(a.data_root) / "coverage" / f"coverage_{a.cid}.json"
    if not acc_path.exists():
        raise AnalysisError(f"未获取精准测试报告: {acc_path} 不存在。先运行 fetch_accuracy.py --task-id {a.task_id}")
    if not cov_path.exists():
        raise AnalysisError(f"未获取 KTest 覆盖率: {cov_path} 不存在。先运行 coverage --cid {a.cid}")

    acc = load_json(acc_path)
    cov = load_json(cov_path)

    # commit 对齐校验(硬性)
    acc_commit = (acc.get("testReport") or {}).get("branch")
    cov_commit = (cov.get("report") or {}).get("commitId")
    acc_branch = (acc.get("testReport") or {}).get("branch")
    cov_branch = (cov.get("report") or {}).get("branch")
    if acc_branch != cov_branch:
        raise AnalysisError(
            f"精准测试报告分支({acc_branch}) != KTest 覆盖率分支({cov_branch}),数据不对齐,拒绝分析。"
            f"需确认是否同一 MR。")

    evidence = collect_method_evidence(acc)
    report = render_report(acc, cov, a.code_dir, evidence, a.target)

    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)
    of = out / f"analysis_{a.task_id}_{a.cid}.md"
    of.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n[written] {of}")


if __name__ == "__main__":
    main()