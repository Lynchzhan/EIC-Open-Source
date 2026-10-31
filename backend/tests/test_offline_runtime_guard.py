"""保护器负控使用真实拦截入口，不把空计数摘要当作保护已加载。"""
import json
import os
from pathlib import Path
import subprocess
import sys


def test_external_network_is_denied_and_loopback_remains_available(tmp_path):
    root = Path(__file__).resolve().parents[2]
    guard_root = root / 'scripts/harness/offline_runtime'
    output = tmp_path / 'negative-controls.json'
    environment = {**os.environ, 'C3_GUARD_ROOT': str(guard_root)}
    environment['PYTHONPATH'] = os.pathsep.join([str(guard_root), environment.get('PYTHONPATH', '')])
    result = subprocess.run([sys.executable, '-B', str(guard_root / 'probe.py'), str(output)],
        env=environment, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode('utf-8', errors='replace')
    report = json.loads(output.read_text(encoding='utf-8'))
    assert report == {'guardLoaded': True, 'deniedNetworkAttempts': {'dns': 1, 'connect': 1, 'connect_ex': 1},
        'deniedHttpTransports': ['sync', 'async'], 'loopbackExchange': True}
