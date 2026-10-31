import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from harness.boundaries import HarnessError, read_journal
from harness.gates import compare_evaluations, release_gate, validate_evaluation, validate_tests
from harness.suite import run_suite, score_case, summarize, STRATA
from release_policy import public_source_sha256

CASES = json.loads((ROOT / "evals/agent-cases.json").read_text(encoding="utf-8"))
CORPUS = json.loads((ROOT / "evals/corpus.json").read_text(encoding="utf-8"))
POLICY = json.loads((ROOT / "evals/policy.json").read_text(encoding="utf-8"))
PIN = "a" * 64


@pytest.fixture(scope="module")
def evaluated():
    result = asyncio.run(run_suite(CASES, CORPUS))
    assert not result["summary"]["failedCaseIds"]
    result.update(lockSha256=PIN, sourceSha256="b" * 64)
    return result


def recalculate(result):
    result["summary"] = summarize(result["cases"])
    result["strata"] = {name: summarize([row for row in result["cases"] if row["stratum"] == name]) for name in STRATA}


def test_baseline_comparison_is_per_metric_and_stratum(evaluated):
    assert compare_evaluations(evaluated, evaluated, CASES, CORPUS, POLICY, PIN)["passed"]
    bad = copy.deepcopy(evaluated)
    row = next(item for item in bad["cases"] if item["stratum"] == "long_context")
    row["measurements"]["withinBudget"] = False
    fixture = next(case for case in CASES["cases"] if case["id"] == row["id"])
    row.update(score_case(fixture, row["measurements"], row["elapsedMs"]))
    recalculate(bad)
    result = compare_evaluations(bad, evaluated, CASES, CORPUS, POLICY, PIN)
    assert not result["passed"]
    assert any(value.startswith("stratum_regression:long_context") for value in result["failures"])
    gate = release_gate(result, {"passed": True}, "b" * 64, PIN)
    assert gate["isolatedGate"] is False and gate["fullReleaseGate"] is False


@pytest.mark.parametrize("mutation", ["empty", "missing", "duplicate", "nan", "summary", "version", "denominator"])
def test_incomplete_or_forged_evaluations_fail_closed(evaluated, mutation):
    bad = copy.deepcopy(evaluated)
    if mutation == "empty":
        bad["cases"] = []
    elif mutation == "missing":
        bad["cases"].pop()
    elif mutation == "duplicate":
        bad["cases"][-1] = bad["cases"][0]
    elif mutation == "nan":
        bad["cases"][0]["observations"][0]["latencyMs"] = float("nan")
    elif mutation == "summary":
        bad["summary"]["metrics"]["taskSuccessRate"] = .5
    elif mutation == "version":
        bad["caseSetSha256"] = "0" * 64
    else:
        for row in bad["cases"]:
            for observation in row["observations"]:
                observation.pop("toolArgumentValidity", None)
        recalculate(bad)
        with pytest.raises(HarnessError, match="原始测量"):
            compare_evaluations(bad, evaluated, CASES, CORPUS, POLICY, PIN)
        return
    with pytest.raises(HarnessError):
        validate_evaluation(bad, CASES, CORPUS, POLICY, PIN)


def _test_evidence_fixture(tmp_path):
    root, evidence = tmp_path / "source", tmp_path / "evidence"
    (root / "backend/tests").mkdir(parents=True)
    evidence.mkdir()
    modules = []
    for name in ["test_alpha.py", "test_ocr.py"]:
        (root / "backend/tests" / name).write_text("pass\n")
        log = Path(name).stem + ".log"
        (evidence / log).write_text("2 passed" + (", 4 skipped" if name == "test_ocr.py" else "") + " in 0.5s\n")
        modules.append({"module": name, "log": log, "exitCode": 0,
                        "command": ["python", "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/"+name]})
    report = {"all_modules": True, "exitCode": 0, "modules": modules, "sourceSha256": public_source_sha256(root)}
    (evidence / "result.json").write_text(json.dumps(report))
    return root, evidence, report


@pytest.mark.parametrize("mutation", ["good", "missing", "failed", "command", "skip", "log", "source"])
def test_full_tests_require_modules_source_commands_and_literal_logs(tmp_path, mutation):
    root, evidence, report = _test_evidence_fixture(tmp_path)
    if mutation == "missing":
        report["modules"].pop()
    elif mutation == "failed":
        report["modules"][0]["exitCode"] = 1
    elif mutation == "command":
        report["modules"][0]["command"] = ["echo", "passed"]
    elif mutation == "skip":
        (evidence / "test_alpha.log").write_text("2 passed, 1 skipped in 0.5s\n")
    elif mutation == "log":
        (evidence / "test_alpha.log").write_text("PASS\n")
    elif mutation == "source":
        (root / "backend/tests/test_alpha.py").write_text("changed = True\n")
    (evidence / "result.json").write_text(json.dumps(report))
    if mutation == "good":
        result = validate_tests(root, evidence, POLICY)
        assert result["modules"] == 2 and result["skippedCount"] == 4
    else:
        with pytest.raises(HarnessError):
            validate_tests(root, evidence, POLICY)


def test_isolated_success_never_opens_production_gate(evaluated):
    comparison = compare_evaluations(evaluated, evaluated, CASES, CORPUS, POLICY, PIN)
    gate = release_gate(comparison, {"passed": True}, "b" * 64, PIN)
    assert gate["isolatedGate"] is True
    assert gate["fullReleaseGate"] is False and gate["missingProductionEvidence"]


def test_result_flags_cannot_override_wrong_raw_measurement(evaluated):
    bad = copy.deepcopy(evaluated)
    row = next(item for item in bad["cases"] if item["kind"] == "tool")
    row["measurements"]["answerSha256"] = "0" * 64
    with pytest.raises(HarnessError, match="原始测量"):
        validate_evaluation(bad, CASES, CORPUS, POLICY, PIN)


def test_cli_runs_real_suite_keeps_append_log_and_rejects_wrong_pin(tmp_path):
    pin = hashlib.sha256((ROOT / "evals/LOCK.json").read_bytes()).hexdigest()
    output, journal = tmp_path / "evaluation.json", tmp_path / "runs.jsonl"
    command = [sys.executable, "-B", str(ROOT / "scripts/evaluate-agent.py"), "evaluate",
               "--expected-lock-sha256", pin, "--output", str(output),
               "--journal", str(journal), "--run-id", "cli-fixed"]
    run = subprocess.run(command, capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr
    result = json.loads(output.read_text())
    assert result["summary"]["caseCount"] == 60 and not result["summary"]["failedCaseIds"]
    original = output.read_bytes()
    repeat = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert repeat.returncode != 0 and output.read_bytes() == original
    rows, head = read_journal(journal)
    assert rows[0]["event"]["passed"] is True and rows[-1]["event"]["passed"] is False
    command[command.index(pin)] = "0" * 64
    invalid = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert invalid.returncode != 0 and "外部批准" in invalid.stderr
    assert read_journal(journal)[1] == head
    if os.environ.get("EIC_TEST_ARTIFACT_DIR"):
        Path(os.environ["EIC_TEST_ARTIFACT_DIR"], "cli-fixed-evaluation.json").write_bytes(original)


def test_release_requires_true_production_evidence_not_mock_pass(tmp_path, evaluated):
    import release_deploy
    from harness.boundaries import canonical, digest
    members = {"backend/src/eic/models.py": "a" * 64}
    gate = release_gate(compare_evaluations(evaluated, evaluated, CASES, CORPUS, POLICY, PIN),
                        {"passed": True}, digest(canonical(members).encode()), PIN)
    path = tmp_path / "gate.json"
    path.write_text(json.dumps(gate))
    with pytest.raises(ValueError, match="完整发布门"):
        release_deploy.require_harness_release(path, hashlib.sha256(path.read_bytes()).hexdigest(), members)
    with pytest.raises(ValueError, match="外部批准"):
        release_deploy.require_harness_release(None, None, members)
    gate.update(fullReleaseGate=True, scope="production_verified", missingProductionEvidence=[])
    path.write_text(json.dumps(gate))
    with pytest.raises(ValueError, match="真实生产证据"):
        release_deploy.require_harness_release(path, hashlib.sha256(path.read_bytes()).hexdigest(), members)


def test_release_schema_head_is_from_archive_not_old_constant(tmp_path):
    import io
    import tarfile
    import release_deploy
    path = tmp_path / "source.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name, data in [
            ("0001.py", b"revision='0001'\ndown_revision=None\n"),
            ("0025.py", b"revision='0025'\ndown_revision='0001'\n"),
        ]:
            item = tarfile.TarInfo("backend/migrations/versions/" + name)
            item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
    assert release_deploy.source_schema_head(path) == "0025"
