"""从逐项观测重算结果；缺证据与回归均使门禁关闭。"""
from pathlib import Path
import re

from .boundaries import HarnessError, canonical, digest, read_json, safe_file
from .metrics import number
from .suite import STRATA, summarize, validate_cases, validate_case_measurement


def validate_evaluation(result, cases_document, corpus, policy, lock_sha256):
    cases = validate_cases(cases_document)
    if (result.get("schemaVersion") != 1 or result.get("scope") != policy["scope"]
            or result.get("caseSetSha256") != digest(canonical(cases_document).encode())
            or result.get("corpusSha256") != digest(canonical(corpus).encode())
            or result.get("lockSha256") != lock_sha256
            or not re.fullmatch(r"[a-f0-9]{64}", result.get("sourceSha256", ""))):
        raise HarnessError("评测来源、固定集或 Locked 版本不一致")
    rows = result.get("cases")
    if not isinstance(rows, list) or len(rows) != len(cases) or len(rows) < policy["minCases"]:
        raise HarnessError("评测用例缺失或总量不满足")
    expected = {row["id"]: row for row in cases}
    if len({row["id"] for row in rows}) != len(rows) or {row["id"] for row in rows} != set(expected):
        raise HarnessError("评测用例 ID 缺失、重复或额外")
    for row in rows:
        fixture = expected[row["id"]]
        if (type(row.get("passed")) is not bool or row.get("stratum") != fixture["stratum"]
                or row.get("kind") != fixture["kind"]
                or row.get("inputSha256") != digest(canonical(fixture["input"]).encode())
                or not isinstance(row.get("observations"), list) or not row["observations"]):
            raise HarnessError("逐项观测结构或输入绑定不完整")
        if row["passed"] and (row.get("errorCode") or
            not any(item.get("taskSuccessRate") is True for item in row["observations"])):
            raise HarnessError("用例成功与实测终态矛盾")
        validate_case_measurement(fixture, row)
    summary = summarize(rows)
    strata = {name: summarize([row for row in rows if row["stratum"] == name]) for name in STRATA}
    if summary != result.get("summary") or strata != result.get("strata"):
        raise HarnessError("聚合结果与逐项观测不一致")
    for name, minimum in policy["strata"].items():
        if strata[name]["caseCount"] < minimum:
            raise HarnessError("评测分层数量不足：" + name)
    return summary, strata


def compare_evaluations(candidate, baseline, cases_document, corpus, policy, lock_sha256):
    current, current_strata = validate_evaluation(candidate, cases_document, corpus, policy, lock_sha256)
    prior, prior_strata = validate_evaluation(baseline, cases_document, corpus, policy, lock_sha256)
    failures, dimensions = [], {}
    if current["failedCaseIds"]:
        failures.append("fixed_cases_failed")
    for name, rule in policy["requiredMetrics"].items():
        a, b = current["metrics"].get(name), prior["metrics"].get(name)
        ac, bc = current["sampleCounts"].get(name, 0), prior["sampleCounts"].get(name, 0)
        if a is None or b is None or ac < rule["minSamples"] or ac != bc:
            failures.append("metric_missing_or_denominator_changed:" + name)
            continue
        a, b = number(a, name, maximum=1), number(b, name, maximum=1)
        passing = a >= rule["limit"] if rule["direction"] == "higher" else a <= rule["limit"]
        regression = a < b if rule["direction"] == "higher" else a > b
        dimensions[name] = {"candidate": a, "baseline": b, "samples": ac,
                            "thresholdPassed": passing, "regression": regression}
        if not passing or regression:
            failures.append("metric_regression_or_threshold:" + name)
        for stratum in STRATA:
            x, y = current_strata[stratum]["metrics"].get(name), prior_strata[stratum]["metrics"].get(name)
            xc, yc = current_strata[stratum]["sampleCounts"].get(name, 0), prior_strata[stratum]["sampleCounts"].get(name, 0)
            if xc != yc or (x is None) != (y is None):
                failures.append("stratum_measurement_changed:" + stratum + ":" + name)
            elif x is not None and ((x < y) if rule["direction"] == "higher" else (x > y)):
                failures.append("stratum_regression:" + stratum + ":" + name)
    return {"passed": not failures, "failures": failures, "dimensions": dimensions,
            "failedCaseIds": current["failedCaseIds"],
            "baselineSourceSha256": baseline["sourceSha256"], "candidateSourceSha256": candidate["sourceSha256"]}


def validate_tests(root, evidence, policy):
    from release_policy import public_source_sha256
    evidence = Path(evidence)
    data = safe_file(evidence, "result.json").read_bytes()
    report = read_json(data)
    expected = {path.name for path in (Path(root) / "backend/tests").glob("test_*.py")}
    expected.update("migration_checks/" + path.name for path in (Path(root) / "backend/migration_checks").glob("test_*.py"))
    rows = report.get("modules")
    if (report.get("all_modules") is not True or type(report.get("exitCode")) is not int
            or report["exitCode"] != 0 or not isinstance(rows, list) or not rows):
        raise HarnessError("全量确定性测试记录缺失或失败")
    if report.get("sourceSha256") != public_source_sha256(Path(root)):
        raise HarnessError("确定性测试证据不属于当前源码")
    if len({row["module"] for row in rows}) != len(rows) or {row["module"] for row in rows} != expected:
        raise HarnessError("确定性测试模块遗漏或重复")
    total_passed, total_skipped, bindings = 0, 0, {"result.json": digest(data)}
    for row in rows:
        name = row["module"]
        command = row.get("command", [])
        if (type(row.get("exitCode")) is not int or row["exitCode"] != 0
                or not isinstance(command, list) or len(command) != 8
                or command[1:] != ["-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                    name if name.startswith("migration_checks/") else "tests/" + name]):
            raise HarnessError("测试退出码或实际命令无效：" + name)
        log = safe_file(evidence, row.get("log", "")).read_bytes()
        text = log.decode("utf-8").replace("\r\n", "\n")
        # pytest 在超过一分钟时追加 HH:MM:SS；合法时长后缀不是失败日志。
        summaries = re.findall(
            r"(?m)^([^\n]*\d+ passed[^\n]* in [0-9.]+s)"
            r"(?: \([0-9]+:[0-5][0-9]:[0-5][0-9]\))?[ \t]*$", text,
        )
        if not summaries or re.search(r"\d+ (?:failed|errors?)", summaries[-1]):
            raise HarnessError("测试日志没有成功汇总：" + name)
        line = summaries[-1]
        passed = int(re.search(r"(\d+) passed", line).group(1))
        skipped_match = re.search(r"(\d+) skipped", line)
        skipped = int(skipped_match.group(1)) if skipped_match else 0
        if skipped > policy.get("allowedSkips", {}).get(name, 0):
            raise HarnessError("测试有未批准跳过：" + name)
        if "ISOLATED_TEST_PROCESS_TIMEOUT" in text:
            raise HarnessError("测试曾超时：" + name)
        total_passed += passed
        total_skipped += skipped
        bindings[row["log"]] = digest(log)
    return {"passed": True, "modules": len(rows), "passedCount": total_passed,
            "skippedCount": total_skipped, "evidenceSha256": digest(canonical(bindings).encode())}


def release_gate(comparison, tests, source_members_sha256, lock_sha256):
    ready = comparison["passed"] is True and tests["passed"] is True
    return {
        "schemaVersion": 1, "isolatedGate": ready, "fullReleaseGate": False,
        "sourceMembersSha256": source_members_sha256, "lockSha256": lock_sha256,
        "comparison": comparison, "deterministicTests": tests,
        "scope": "deterministic_synthetic_not_real_model_quality",
        "missingProductionEvidence": [
            "real_model_quality", "real_database_and_dependencies", "fresh_lock_build",
            "frontend_browser_acceptance", "database_backup_restore",
            "capacity_and_budget_approval", "human_release_approval",
        ],
    }
