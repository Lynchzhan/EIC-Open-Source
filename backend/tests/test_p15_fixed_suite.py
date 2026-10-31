import asyncio
import copy
import json
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from harness.boundaries import HarnessError
from harness.suite import run_suite, score_case, validate_cases

CASES = json.loads((ROOT / "evals/agent-cases.json").read_text(encoding="utf-8"))
CORPUS = json.loads((ROOT / "evals/corpus.json").read_text(encoding="utf-8"))


def test_all_fixed_cases_run_real_application_code_and_preserve_strata():
    result = asyncio.run(run_suite(CASES, CORPUS))
    output = os.environ.get("EIC_TEST_ARTIFACT_DIR")
    if output:
        Path(output, "fixed-agent-evaluation.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                                              encoding="utf-8")
    assert len(result["cases"]) == 60
    assert result["summary"]["failedCaseIds"] == []
    assert all(row["caseCount"] == 15 for row in result["strata"].values())
    assert result["summary"]["metrics"]["providerCost"] is None
    assert result["summary"]["metrics"]["inputTokens"] is None
    assert result["summary"]["metrics"]["ndcgAtK"] == 1
    assert result["summary"]["metrics"]["duplicateSideEffectRate"] == 0
    assert result["summary"]["metrics"]["crashRecoveryRate"] == 1
    assert result["summary"]["sampleCounts"]["crashRecoveryRate"] == 1
    probe = next(row["crashProbe"] for row in result["cases"] if "crashProbe" in row)
    assert probe["writerExit"] == 17 and probe["readerExit"] == 0


def test_known_bad_rag_cannot_pass_and_gold_not_forwarded_to_candidate():
    case = CASES["cases"][0]
    observed = score_case(case, {"ranked": ["unknown"], "citations": [
        {"claimId": "refund", "sourceId": "unknown"}]}, 1)
    assert observed["passed"] is False
    assert observed["ranking"]["hitAtK"] == 0
    assert observed["citations"]["citationPrecision"] == 0
    bad = copy.deepcopy(CASES)
    bad["cases"].append(bad["cases"][0])
    with pytest.raises(HarnessError, match="ID 重复"):
        validate_cases(bad)
