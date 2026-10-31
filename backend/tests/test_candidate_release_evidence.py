"""候选发布校验器负例；合成 JSON 只存 pytest 临时目录，不生成发布证据。"""
import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from harness.boundaries import canonical
from production_evidence import REQUIRED_KINDS, contained_file, verify_records
from source_secrets import require_public_content


@pytest.mark.parametrize("name", ["../escape", "/absolute", "a\\b", "x:y", "", "a/../b"])
def test_evidence_paths_reject_escape_and_noncanonical_names(tmp_path, name):
    with pytest.raises(ValueError):
        contained_file(tmp_path.resolve(), name)


def synthetic_records(tmp_path):
    now = datetime.now(UTC)
    members = {"fixture.py": "a" * 64}
    source = hashlib.sha256(canonical(members).encode()).hexdigest()
    images = {"fixture": "sha256:" + "b" * 64}
    artifact = tmp_path / "synthetic.txt"
    artifact.write_text("unit test only", encoding="utf-8")
    gate = {"runId": "synthetic-unit-only", "imageDigests": images, "productionEvidence": {}}
    for kind in sorted(REQUIRED_KINDS):
        record = {"schemaVersion": 1, "kind": kind, "passed": True,
            "scope": "production_verified", "sourceMembersSha256": source, "imageDigests": images,
            "runId": gate["runId"], "startedAt": (now - timedelta(seconds=2)).isoformat(),
            "finishedAt": (now - timedelta(seconds=1)).isoformat(), "expiresAt": (now + timedelta(hours=1)).isoformat(),
            "artifacts": [{"path": artifact.name, "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}],
            "approval": {"decision": "approved", "actor": "synthetic-unit-fixture-not-human",
                "sourceMembersSha256": source, "imageDigests": images}}
        path = tmp_path / (kind + ".json")
        path.write_text(json.dumps(record), encoding="utf-8")
        gate["productionEvidence"][kind] = {"passed": True, "recordPath": path.name,
            "recordSha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return gate, members, images


@pytest.mark.parametrize("mutation", ["artifact", "record", "image", "missing_kind", "run", "expired", "approval_scope"])
def test_tampering_is_rejected_after_positive_control(tmp_path, mutation):
    gate, members, images = synthetic_records(tmp_path)
    path = tmp_path / "not-written-gate.json"
    verify_records(path, gate, members, images)
    if mutation == "artifact":
        (tmp_path / "synthetic.txt").write_text("changed", encoding="utf-8")
    elif mutation == "image":
        images["fixture"] = "sha256:" + "c" * 64
    elif mutation == "missing_kind":
        gate["productionEvidence"].pop("human_release_approval")
    else:
        reference = gate["productionEvidence"]["human_release_approval"]
        record_path = tmp_path / reference["recordPath"]
        record = json.loads(record_path.read_text(encoding="utf-8"))
        if mutation == "run":
            record["runId"] = "other-run"
        elif mutation == "expired":
            record["expiresAt"] = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        elif mutation == "approval_scope":
            record["approval"]["sourceMembersSha256"] = "c" * 64
        else:
            record["passed"] = False
        record_path.write_text(json.dumps(record), encoding="utf-8")
        if mutation != "record":
            reference["recordSha256"] = hashlib.sha256(record_path.read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        verify_records(path, gate, members, images)


def test_secret_scan_error_contains_only_member_name():
    # 分开构造合成特征，避免该测试源码本身匹配公开源码扫描规则。
    value = b"-----BEGIN " + b"PRIVATE KEY-----\nsynthetic-unit-only"
    with pytest.raises(ValueError) as error:
        require_public_content("fixture.txt", value)
    assert "fixture.txt" in str(error.value) and "PRIVATE KEY" not in str(error.value)
