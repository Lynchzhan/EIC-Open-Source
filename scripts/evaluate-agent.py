"""中文 Harness 入口；规则由外部 pin 固定，结果只写源码之外。"""
import argparse
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from harness.boundaries import HarnessError, append_journal, canonical, digest, read_json, validate_lock
from harness.gates import compare_evaluations, release_gate, validate_tests
from harness.suite import run_suite
from release_policy import public_source_sha256


def source_digest(root):
    if not (root / "backend/src/eic/models.py").is_file():
        raise HarnessError("候选源码目录无效")
    return public_source_sha256(root)


def write_new(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(canonical(document) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["evaluate", "gate"])
    parser.add_argument("--expected-lock-sha256", required=True)
    parser.add_argument("--candidate-source", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--expected-journal-head")
    parser.add_argument("--run-id", default=uuid.uuid4().hex)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--test-evidence", type=Path)
    args = parser.parse_args()
    root = args.candidate_source.resolve()
    output = args.output.resolve()
    if output.is_relative_to(ROOT) or args.journal.resolve().is_relative_to(ROOT):
        raise HarnessError("运行结果和追加日志必须存放在源码之外")
    validate_lock(ROOT, ROOT / "evals/LOCK.json", args.expected_lock_sha256)
    cases = read_json((ROOT / "evals/agent-cases.json").read_bytes())
    corpus = read_json((ROOT / "evals/corpus.json").read_bytes())
    policy = read_json((ROOT / "evals/policy.json").read_bytes())
    source_sha = source_digest(root)
    event = {"runId": args.run_id, "candidateSha256": source_sha,
             "policySha256": args.expected_lock_sha256,
             "caseSetSha256": digest(canonical(cases).encode()), "stage": args.action}
    try:
        if args.action == "evaluate":
            if output.exists():
                raise HarnessError("评测输出已存在，禁止覆盖")
            sys.path.insert(0, str(root / "backend/src"))
            for key in list(os.environ):
                if key.upper().startswith("EIC_"):
                    del os.environ[key]
            os.environ.update(EIC_ENVIRONMENT="testing", EIC_DATABASE_URL="sqlite+aiosqlite:///:memory:",
                EIC_JWT_SECRET="synthetic-evaluation-only-secret-at-least-32-characters",
                EIC_SECRET_ENCRYPTION_KEY="synthetic-evaluation-only-key-at-least-32-characters",
                EIC_HARNESS_CANDIDATE_SOURCE=str(root), EIC_SEED_DEMO_DATA="false")
            with tempfile.TemporaryDirectory(prefix="eic-harness-cwd-") as directory:
                previous = Path.cwd()
                try:
                    os.chdir(directory)
                    result = asyncio.run(run_suite(cases, corpus))
                finally:
                    os.chdir(previous)
            result.update(sourceSha256=source_sha, lockSha256=args.expected_lock_sha256)
            validate_lock(ROOT, ROOT / "evals/LOCK.json", args.expected_lock_sha256)
            if source_digest(root) != source_sha:
                raise HarnessError("运行期间候选源码改变")
            write_new(output, result)
            success = not result["summary"]["failedCaseIds"]
            event.update(passed=success, failedCaseIds=result["summary"]["failedCaseIds"],
                         metricsSha256=digest(output.read_bytes()))
        else:
            if not args.baseline or not args.candidate or not args.test_evidence:
                raise HarnessError("门禁需要基线、候选和全量测试证据")
            baseline, candidate = read_json(args.baseline.read_bytes()), read_json(args.candidate.read_bytes())
            if candidate.get("sourceSha256") != source_sha:
                raise HarnessError("候选评测不是当前源码")
            comparison = compare_evaluations(candidate, baseline, cases, corpus, policy, args.expected_lock_sha256)
            tests = validate_tests(root, args.test_evidence, policy)
            result = release_gate(comparison, tests, source_sha, args.expected_lock_sha256)
            result["inputs"] = {"baselineSha256": digest(args.baseline.read_bytes()),
                               "candidateSha256": digest(args.candidate.read_bytes())}
            write_new(output, result)
            success = result["isolatedGate"]
            event.update(passed=success, gate="PASS_ISOLATED" if success else "FAIL",
                         failedCaseIds=comparison["failedCaseIds"], metricsSha256=digest(output.read_bytes()))
        head = append_journal(args.journal, event, expected_head=args.expected_journal_head)
        print(canonical({"阶段": args.action, "隔离结果": "通过" if success else "失败",
                         "生产发布门": False, "output": str(output), "journalHead": head}))
        return 0 if success else 1
    except Exception as error:
        event.update(passed=False, errorCode=type(error).__name__)
        append_journal(args.journal, event, expected_head=args.expected_journal_head)
        raise


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (HarnessError, ValueError, KeyError, OSError) as error:
        print("评测未通过：" + str(error), file=sys.stderr)
        raise SystemExit(2)
