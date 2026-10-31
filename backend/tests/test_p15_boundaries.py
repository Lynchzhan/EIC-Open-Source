import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))
from harness.boundaries import HarnessError, append_journal, read_journal, validate_lock


def lock_fixture(tmp_path):
    (tmp_path / "evaluator.py").write_text("locked = True\n")
    document = {"schemaVersion": 1, "surfaces": {
        name: [] for name in ["Locked", "Editable", "Append-only", "Human-controlled"]
    }, "files": {"evaluator.py": hashlib.sha256((tmp_path / "evaluator.py").read_bytes()).hexdigest()}}
    path = tmp_path / "lock.json"
    path.write_text(json.dumps(document))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_external_pin_detects_evaluator_and_manifest_changes(tmp_path):
    path, pin = lock_fixture(tmp_path)
    validate_lock(tmp_path, path, pin)
    (tmp_path / "evaluator.py").write_text("locked = False\n")
    with pytest.raises(HarnessError, match="Locked 文件"):
        validate_lock(tmp_path, path, pin)
    document = json.loads(path.read_text())
    document["files"]["evaluator.py"] = hashlib.sha256((tmp_path / "evaluator.py").read_bytes()).hexdigest()
    path.write_text(json.dumps(document))
    with pytest.raises(HarnessError, match="外部批准"):
        validate_lock(tmp_path, path, pin)


def test_new_file_inside_locked_directory_is_not_silently_allowed(tmp_path):
    path, _ = lock_fixture(tmp_path)
    document = json.loads(path.read_text())
    (tmp_path / "locked").mkdir()
    (tmp_path / "locked/known.py").write_text("pass\n")
    document["surfaces"]["Locked"] = ["locked/"]
    document["files"]["locked/known.py"] = hashlib.sha256((tmp_path / "locked/known.py").read_bytes()).hexdigest()
    path.write_text(json.dumps(document))
    pin = hashlib.sha256(path.read_bytes()).hexdigest()
    validate_lock(tmp_path, path, pin)
    (tmp_path / "locked/extra.py").write_text("not approved\n")
    with pytest.raises(HarnessError, match="未批准文件"):
        validate_lock(tmp_path, path, pin)


@pytest.mark.parametrize("name", ["../outside", "C:/outside", "/outside", "a\\b", "./evaluator.py"])
def test_locked_path_rejects_escape(tmp_path, name):
    path, _ = lock_fixture(tmp_path)
    document = json.loads(path.read_text())
    document["files"] = {name: "a" * 64}
    path.write_text(json.dumps(document))
    with pytest.raises(HarnessError):
        validate_lock(tmp_path, path, hashlib.sha256(path.read_bytes()).hexdigest())


def test_journal_appends_failures_and_detects_edit_and_partial_tail(tmp_path):
    path = tmp_path / "runs.jsonl"
    first = append_journal(path, {"runId": "r1", "passed": False, "failedCaseIds": ["c1"]})
    original = path.read_bytes()
    head = append_journal(path, {"runId": "r2", "passed": True}, expected_head=first)
    assert path.read_bytes().startswith(original)
    assert len(read_journal(path, expected_head=head)[0]) == 2
    path.write_bytes(original)
    with pytest.raises(HarnessError, match="外部锚点"):
        read_journal(path, expected_head=head)
    path.write_bytes(original.replace(b'"passed":false', b'"passed":true'))
    with pytest.raises(HarnessError, match="哈希链"):
        append_journal(path, {"runId": "r3"})
    path.write_bytes(original + b'{"unfinished":')
    with pytest.raises(HarnessError, match="尾部不完整"):
        append_journal(path, {"runId": "r3"})


def test_journal_rejects_private_body_and_secret_metadata(tmp_path):
    for event in [{"runId": "r1", "authorization": "PRIVATE"}, {"runId": "含正文或密码"},
                  {"runId": "r1", "candidateSha256": "PRIVATE"}]:
        with pytest.raises(HarnessError):
            append_journal(tmp_path / "runs.jsonl", event)
    assert not (tmp_path / "runs.jsonl").exists()


def test_three_process_journal_writers_keep_all_records(tmp_path):
    path = tmp_path / "runs.jsonl"
    program = (
        "from harness.boundaries import append_journal;"
        "import sys;append_journal(sys.argv[1],{'runId':sys.argv[2],'passed':False})"
    )
    environment = __import__("os").environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).parents[2] / "scripts")
    processes = [subprocess.Popen([sys.executable, "-B", "-c", program, str(path), f"r{n}"],
                                  env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for n in range(3)]
    for process in processes:
        _, error = process.communicate(timeout=30)
        assert process.returncode == 0, error.decode()
    records, _ = read_journal(path)
    assert len(records) == 3
    assert {row["event"]["runId"] for row in records} == {"r0", "r1", "r2"}
