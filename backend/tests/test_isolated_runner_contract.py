"""正式全量运行器的环境隔离和迁移检查不得遗漏。"""
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from harness.boundaries import HarnessError
from harness.gates import validate_tests
from release_policy import public_source_sha256


def test_runner_explicit_test_capabilities_and_no_inherited_exporter(monkeypatch):
    for key in ('EIC_DATABASE_URL', 'LANGFUSE_SECRET_KEY', 'OTEL_EXPORTER_OTLP_ENDPOINT'):
        monkeypatch.setenv(key, 'must-not-inherit')
    spec = importlib.util.spec_from_file_location('isolated_runner', ROOT / 'scripts/test-isolated.py')
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    env = runner.test_environment()
    assert all('must-not-inherit' != value for value in env.values())
    assert 'LANGFUSE_SECRET_KEY' not in env and 'OTEL_EXPORTER_OTLP_ENDPOINT' not in env
    assert env['EIC_TASK_EXECUTION_ENABLED'] == 'true'
    assert env['PYTHONUTF8'] == '1' and env['PYTHONIOENCODING'] == 'utf-8'
    assert env['EIC_LANGFUSE_ENABLED'] == env['EIC_OUTBOX_DISPATCH_ENABLED'] == env['EIC_GENERATION_RECOVERY_ENABLED'] == 'false'
    assert env['OTEL_SDK_DISABLED'] == 'true'


@pytest.mark.parametrize('mode', ['good', 'omitted', 'wrong_command', 'failed'])
def test_full_gate_requires_migration_check_module(tmp_path, mode):
    source, evidence = tmp_path / 'source', tmp_path / 'evidence'
    (source / 'backend/tests').mkdir(parents=True)
    (source / 'backend/migration_checks').mkdir()
    evidence.mkdir()
    rows = []
    for name in ('tests/test_alpha.py', 'migration_checks/test_sdk.py'):
        (source / 'backend' / name).write_text('pass\n')
        module = name.removeprefix('tests/')
        log = name.replace('/', '_') + '.log'
        (evidence / log).write_text('2 passed in 0.1s\n')
        rows.append({'module': module, 'exitCode': 0, 'log': log,
            'command': ['python', '-B', '-m', 'pytest', '-q', '-p', 'no:cacheprovider', name]})
    if mode == 'omitted':
        rows.pop()
    elif mode == 'wrong_command':
        rows[-1]['command'][-1] = 'tests/test_alpha.py'
    elif mode == 'failed':
        rows[-1]['exitCode'] = 1
    (evidence / 'result.json').write_text(json.dumps({'all_modules': True, 'exitCode': 0,
        'sourceSha256': public_source_sha256(source), 'modules': rows}))
    if mode == 'good':
        assert validate_tests(source, evidence, {'allowedSkips': {}})['modules'] == 2
    else:
        with pytest.raises(HarnessError):
            validate_tests(source, evidence, {'allowedSkips': {}})
