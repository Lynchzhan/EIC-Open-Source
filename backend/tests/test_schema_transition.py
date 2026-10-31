"""SQLite目录迁移故障演练；不连接云端，不启动真实服务。"""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import sys

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import schema_transition as transition


def connect(path):
    return closing(sqlite3.connect(path, isolation_level=None))


def plan():
    return {'schemaVersion': 1, 'fromHead': transition.FROM_HEAD, 'toHead': transition.TO_HEAD,
            'strategy': 'sqlite_directory_swap', 'commitBoundary': 'before_candidate_start',
            'postCommitFailure': 'stop_writers_keep_new_data'}


class Operations:
    def __init__(self, root, *, fail=None):
        self.root, self.fail = root, fail
        self.called = []
        self.quiet, self.old_started, self.new_started = False, False, False

    def point(self, name):
        self.called.append(name)
        if self.fail == name:
            raise RuntimeError('synthetic_failure')

    def quiesce(self):
        self.point('quiesce')
        self.quiet = True

    def assert_quiesced(self):
        self.point('assert_quiesced')
        assert self.quiet

    def snapshot(self, folder):
        self.point('snapshot')
        return {'path': str(folder), 'manifestSha256': 'a' * 64}

    def verify_backup(self, record):
        self.point('verify_backup')

    def upgrade(self, stage, attempt):
        self.point('upgrade_' + str(attempt))
        with connect(stage / 'eic.db') as connection:
            for name in sorted(transition.ADDED_TABLES):
                connection.execute('CREATE TABLE IF NOT EXISTS ' + name + '(id INTEGER PRIMARY KEY)')
            connection.execute('UPDATE alembic_version SET version_num=?', (transition.TO_HEAD,))

    def assert_migrator_stopped(self):
        self.point('assert_migrator_stopped')

    def prepare_candidates(self):
        self.point('prepare_candidates')

    def start_candidates(self):
        # 模拟启动回复丢失前，后台已经产生一条业务写入。
        self.new_started = True
        self.quiet = False
        with connect(self.root / 'persistent/data/eic.db') as connection:
            connection.execute('INSERT INTO business VALUES(2, ?)', ('after_start',))
        self.point('start_candidates')

    def check_candidates(self):
        self.point('check_candidates')

    def open_gateway(self):
        self.point('open_gateway')

    def check_gateway(self):
        self.point('check_gateway')

    def finalize(self):
        self.point('finalize')

    def contain_failure(self):
        self.point('contain_failure')
        self.quiet = True

    def resume_original(self):
        self.point('resume_original')
        assert transition.database_record(self.root / 'persistent/data/eic.db')['head'] == transition.FROM_HEAD
        self.old_started = True


@pytest.fixture
def case(tmp_path):
    root = tmp_path / 'eic'
    data = root / 'persistent/data'
    release = root / 'releases/r001'
    data.mkdir(parents=True)
    release.mkdir(parents=True)
    with connect(data / 'eic.db') as connection:
        connection.execute('CREATE TABLE alembic_version(version_num TEXT PRIMARY KEY)')
        connection.execute('INSERT INTO alembic_version VALUES(?)', (transition.FROM_HEAD,))
        connection.execute('CREATE TABLE business(id INTEGER PRIMARY KEY, value TEXT)')
        connection.execute('INSERT INTO business VALUES(1, ?)', ('synthetic',))
    (data / 'unrelated.bin').write_bytes(b'synthetic ancillary data')
    manifest = {'schema_head': transition.TO_HEAD, 'schema_transition': plan(),
                'images': {'backend': {'digest': 'sha256:' + 'a' * 64}}}
    return root, data, release, manifest


def execute(case, operations):
    root, _, release, manifest = case
    return transition.execute({'root': str(root)}, manifest, release, operations)


def test_success_keeps_original_and_commits_before_any_background_write(case):
    root, data, release, _ = case
    operations = Operations(root)
    result = execute(case, operations)
    assert result['productionSwitchPerformed'] and result['commitBoundaryCrossed']
    assert not result['rollbackPerformed'] and result['userAcceptance'] == 'pending'
    assert transition.database_record(data / 'eic.db')['head'] == transition.TO_HEAD
    assert transition.database_record(data.with_name('data-original-r001') / 'eic.db')['head'] == transition.FROM_HEAD
    rows = [json.loads(row) for row in (release / 'migration-journal.jsonl').read_text().splitlines()]
    assert [row['phase'] for row in rows].index('commit_before_candidate_start') < [row['phase'] for row in rows].index('candidates_ready')
    assert operations.called.count('upgrade_0') == operations.called.count('upgrade_1') == 1
    assert not (root / 'persistent/.schema-transition.lock').exists()


@pytest.mark.parametrize('failure', ['quiesce', 'snapshot', 'verify_backup', 'upgrade_0', 'upgrade_1', 'prepare_candidates'])
def test_precommit_failures_restore_original_directory(case, failure):
    root, data, release, _ = case
    before = transition.database_record(data / 'eic.db')
    operations = Operations(root, fail=failure)
    with pytest.raises(RuntimeError):
        execute(case, operations)
    result = json.loads((release / 'migration-result.json').read_text())
    assert result['rollbackPerformed'] and not result['commitBoundaryCrossed']
    assert operations.old_started and not operations.new_started
    assert transition.database_record(data / 'eic.db') == before


@pytest.mark.parametrize('failure', ['start_candidates', 'check_candidates', 'open_gateway', 'check_gateway', 'finalize'])
def test_postcommit_failures_never_restore_old_snapshot(case, failure):
    root, data, release, _ = case
    operations = Operations(root, fail=failure)
    with pytest.raises(RuntimeError):
        execute(case, operations)
    result = json.loads((release / 'migration-result.json').read_text())
    assert result['manualRecoveryRequired'] and result['commitBoundaryCrossed']
    assert not result['rollbackPerformed'] and not operations.old_started
    with connect(data / 'eic.db') as connection:
        assert connection.execute('SELECT value FROM business WHERE id=2').fetchone() == ('after_start',)
    assert (root / 'persistent/.schema-transition.lock').exists()


@pytest.mark.parametrize('failed_part', ['contain_failure', 'assert_migrator_stopped', 'resume_original'])
def test_recovery_failure_keeps_lock_and_does_not_claim_rollback(case, failed_part):
    root, _, release, _ = case
    operations = Operations(root, fail=failed_part)
    def broken_prepare():
        raise RuntimeError('synthetic_prepare_failure')
    operations.prepare_candidates = broken_prepare
    with pytest.raises(RuntimeError):
        execute(case, operations)
    result = json.loads((release / 'migration-result.json').read_text())
    assert not result['rollbackPerformed'] and result['manualRecoveryRequired']
    assert (root / 'persistent/.schema-transition.lock').exists()


def test_original_rename_failure_is_recoverable(case, monkeypatch):
    root, data, release, _ = case
    rename = Path.rename
    def fail_stage(path, target):
        if path.name == 'data-candidate-r001':
            raise OSError('synthetic_rename_interruption')
        return rename(path, target)
    monkeypatch.setattr(Path, 'rename', fail_stage)
    with pytest.raises(OSError):
        execute(case, Operations(root))
    assert transition.database_record(data / 'eic.db')['head'] == transition.FROM_HEAD
    assert json.loads((release / 'migration-result.json').read_text())['rollbackPerformed']


def test_modified_old_rows_reject_candidate_and_preserve_original(case):
    root, data, _, _ = case
    operations = Operations(root)
    upgrade = operations.upgrade
    def corrupt(stage, attempt):
        upgrade(stage, attempt)
        with connect(stage / 'eic.db') as connection:
            connection.execute('UPDATE business SET value=?', ('corrupt',))
    operations.upgrade = corrupt
    with pytest.raises(ValueError, match='原业务表'):
        execute(case, operations)
    with connect(data / 'eic.db') as connection:
        assert connection.execute('SELECT value FROM business').fetchone() == ('synthetic',)


@pytest.mark.parametrize('field,value', [('fromHead', 'unexpected'), ('toHead', 'unexpected'),
    ('commitBoundary', 'after_gateway_open'), ('postCommitFailure', 'restore_snapshot')])
def test_transition_plan_rejects_unsafe_options(case, field, value):
    root, _, _, manifest = case
    manifest['schema_transition'][field] = value
    operations = Operations(root)
    with pytest.raises(ValueError, match='范围'):
        execute(case, operations)
    assert operations.called == []


def test_existing_lock_prevents_takeover(case):
    root, _, _, _ = case
    (root / 'persistent/.schema-transition.lock').write_text('previous_incomplete_release')
    operations = Operations(root)
    with pytest.raises(FileExistsError):
        execute(case, operations)
    assert not operations.called


def test_no_original_run_starts_when_migrator_has_uncertain_outcome(case):
    root, _, release, _ = case
    operations = Operations(root)
    operations.upgrade = lambda *_: (_ for _ in ()).throw(TimeoutError('synthetic_timeout'))
    operations.assert_migrator_stopped = lambda: (_ for _ in ()).throw(RuntimeError('still_running'))
    with pytest.raises(TimeoutError):
        execute(case, operations)
    result = json.loads((release / 'migration-result.json').read_text())
    assert not operations.old_started and result['manualRecoveryRequired']


def test_real_versioned_migrations_preserve_business_tables_twice(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from eic.core.config import get_settings
    database = tmp_path / 'versioned.db'
    monkeypatch.setenv('EIC_DATABASE_URL', 'sqlite+aiosqlite:///' + database.as_posix())
    get_settings.cache_clear()
    config = Config(str(ROOT / 'backend/alembic.ini'))
    config.set_main_option('script_location', str(ROOT / 'backend/migrations'))
    config.set_main_option('prepend_sys_path', str(ROOT / 'backend/src'))
    config.set_main_option('path_separator', 'os')
    try:
        command.upgrade(config, transition.FROM_HEAD)
        before = transition.database_record(database)
        assert len(before['tables']) > 30
        for _ in range(2):
            command.upgrade(config, transition.TO_HEAD)
            transition.assert_upgraded(before, transition.database_record(database))
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize('part', ['data', 'data-candidate-r001'])
def test_rename_succeeded_but_response_interrupted_restores_by_real_identity(case, monkeypatch, part):
    root, data, release, _ = case
    rename = Path.rename
    injected = False
    def interrupted(path, target):
        nonlocal injected
        result = rename(path, target)
        if path.name == part and not injected:
            injected = True
            raise OSError('synthetic_response_lost_after_rename')
        return result
    monkeypatch.setattr(Path, 'rename', interrupted)
    with pytest.raises(OSError):
        execute(case, Operations(root))
    result = json.loads((release / 'migration-result.json').read_text())
    assert result['rollbackPerformed'] and not result['manualRecoveryRequired']
    assert transition.database_record(data / 'eic.db')['head'] == transition.FROM_HEAD


def test_host_migrator_uses_network_none_and_only_candidate_data(tmp_path, monkeypatch):
    from schema_transition_host import ExistingHostOperations
    import existing_host_release as host
    operations = object.__new__(ExistingHostOperations)
    operations.migrators = ['migrate-0', 'migrate-1']
    operations.stamp = 'synthetic'
    operations.env_file = tmp_path / 'private.env'
    operations.layout = {'containers': {'eic-v4-backend': {'memory': 1000000}}}
    operations.manifest = {'images': {'backend': {'digest': 'sha256:' + 'a' * 64}}}
    commands = []
    monkeypatch.setattr(host, 'run', lambda *argv, **_: commands.append(argv) or '')
    monkeypatch.setattr(host, 'inspect', lambda *_: [{'State': {'Running': False, 'ExitCode': 0}}])
    candidate = tmp_path / 'candidate-only'
    operations.upgrade(candidate, 0)
    create = commands[0]
    assert create[create.index('--network') + 1] == 'none'
    assert create[create.index('--restart') + 1] == 'no'
    assert create[create.index('--mount') + 1] == 'type=bind,src=' + str(candidate) + ',dst=/app/data'
    assert 'sha256:' + 'a' * 64 in create
    assert create[-2:] == ('eic.core.schema', 'upgrade')
    assert '--publish' not in create and create.count('--mount') == 1


@pytest.mark.parametrize('failed_role', ['gateway', 'backend', 'frontend'])
def test_containment_attempts_all_original_services_after_individual_failure(monkeypatch, failed_role):
    from schema_transition_host import ExistingHostOperations
    operations = object.__new__(ExistingHostOperations)
    operations.layout = {'containers': {'eic-v4-' + role: {'id': role}
                         for role in ('gateway', 'backend', 'frontend')}}
    attempted = []
    def stop(identifier):
        attempted.append(identifier)
        if identifier == failed_role:
            raise RuntimeError('synthetic_stop_failure')
    operations._stop = stop
    operations._candidates = lambda: [{'Id': 'candidate', 'State': {'Running': True}}]
    with pytest.raises(ValueError, match='收口'):
        operations.contain_failure()
    assert set(attempted) == {'gateway', 'backend', 'frontend', 'candidate'}


def test_formal_entrypoint_cannot_enter_migration_before_full_release_gate(monkeypatch):
    import existing_host_release as host
    import release_deploy
    import schema_transition_host
    entered = []
    def refuse(*args, **kwargs):
        raise ValueError('synthetic_full_gate_denied')
    monkeypatch.setattr(release_deploy, 'require_harness_release', refuse)
    monkeypatch.setattr(schema_transition_host, 'apply_transition', lambda *_: entered.append(True))
    payload = {'layout': {}, 'manifest': {'source_members': {}, 'images': {}, 'schema_transition': plan()},
               'gate': 'synthetic-gate.json', 'gateSha256': 'a' * 64}
    with pytest.raises(ValueError, match='full_gate_denied'):
        host.apply_remote(payload)
    assert entered == []


def test_vector_backup_is_actually_restored_and_compared(tmp_path, monkeypatch):
    from schema_transition_host import ExistingHostOperations
    import existing_host_release as host
    operations = object.__new__(ExistingHostOperations)
    operations.release, operations.stamp = tmp_path, 'fixture'
    operations.layout = {'containers': {'eic-v4-vector': {'id': 'original-vector',
                           'memory': 1000000, 'image': 'sha256:' + 'b' * 64}}}
    operations.environment = {'EIC_VECTOR_DATABASE_URL': 'postgresql://fixture:fixture@vector/fixture'}
    commands, exported, stopped = [], [], []
    def run(*argv, **kwargs):
        commands.append(argv)
        return 'isolated-vector' if argv[:3] == ('docker', 'ps', '-aq') else ''
    def inspect(identifier):
        if identifier == 'original-vector':
            return [{'Config': {'Env': ['POSTGRES_USER=fixture']}}]
        return [{'Id': 'isolated-vector', 'Image': 'sha256:' + 'b' * 64,
                 'Config': {'Labels': {'eic.release.id': 'fixture', 'eic.release.role': 'restore'}},
                 'State': {'Running': True}, 'Mounts': [{'Type': 'bind', 'Source': str(tmp_path / 'vector-restore-data')}]}]
    monkeypatch.setattr(host, 'run', run)
    monkeypatch.setattr(host, 'inspect', inspect)
    operations._stop = stopped.append
    operations.vector_data_digest = lambda *args: exported.append(args) or 'a' * 64
    assert operations.restore_vector(tmp_path / 'snapshot.dump') == 'a' * 64
    create = next(row for row in commands if row[:2] == ('docker', 'create'))
    assert create[create.index('--network') + 1] == 'none'
    assert 'type=bind,src=' + str(tmp_path / 'vector-restore-data') + ',dst=/var/lib/postgresql/data' in create
    assert any('pg_restore' in row and '--single-transaction' in row and '--exit-on-error' in row for row in commands)
    assert [row[0] for row in exported] == ['original-vector', 'isolated-vector']
    assert stopped == ['isolated-vector']
    assert ('docker', 'rm', 'isolated-vector') in commands


def test_vector_data_mismatch_stops_restore_and_rejects_backup(tmp_path, monkeypatch):
    from schema_transition_host import ExistingHostOperations
    import existing_host_release as host
    operations = object.__new__(ExistingHostOperations)
    operations.release, operations.stamp = tmp_path, 'fixture'
    operations.layout = {'containers': {'eic-v4-vector': {'id': 'original-vector', 'memory': 1000000,
                           'image': 'sha256:' + 'b' * 64}}}
    operations.environment = {'EIC_VECTOR_DATABASE_URL': 'postgresql://fixture:fixture@vector/fixture'}
    monkeypatch.setattr(host, 'run', lambda *argv, **_: 'isolated-vector' if argv[:3] == ('docker', 'ps', '-aq') else '')
    monkeypatch.setattr(host, 'inspect', lambda identifier: [{'Config': {'Env': []}}] if identifier == 'original-vector' else [
        {'Id': 'isolated-vector', 'Image': 'sha256:' + 'b' * 64, 'State': {'Running': True},
         'Config': {'Labels': {'eic.release.id': 'fixture', 'eic.release.role': 'restore'}},
         'Mounts': [{'Type': 'bind', 'Source': str(tmp_path / 'vector-restore-data')}]}])
    stopped = []
    operations._stop = stopped.append
    operations.vector_data_digest = lambda identifier, *_: identifier
    with pytest.raises(ValueError, match='摘要不匹配'):
        operations.restore_vector(tmp_path / 'snapshot.dump')
    assert stopped == ['isolated-vector']


def test_paths_with_links_are_rejected_before_reading_target(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    link = tmp_path / 'link'
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='链接'):
        transition.safe_path(link / 'database.db')


def test_hard_link_in_data_is_rejected(tmp_path):
    import os
    original = tmp_path / 'one'
    original.write_bytes(b'synthetic')
    os.link(original, tmp_path / 'two')
    with pytest.raises(ValueError, match='硬链接'):
        transition.file_map(tmp_path)


def test_normalized_backup_keeps_committed_wal_and_unversioned_control_rows(tmp_path):
    import shutil
    source = tmp_path / 'original'
    backup = tmp_path / 'backup'
    source.mkdir()
    backup.mkdir()
    path = source / 'control.sqlite'
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        assert connection.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        connection.execute('PRAGMA wal_autocheckpoint=0')
        connection.execute('CREATE TABLE control(id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT)')
        connection.execute('INSERT INTO control(label) VALUES(?)', ('synthetic_control',))
        before = transition.database_record(path, require_version=False)
        for item in source.iterdir():
            shutil.copyfile(item, backup / item.name)
        transition.normalize_backup_database(backup / path.name)
        assert transition.database_record(backup / path.name, require_version=False) == before
        assert not (backup / (path.name + '-wal')).exists()
        assert not (backup / (path.name + '-shm')).exists()
        pinned = transition.file_map(backup)
        transition.database_record(backup / path.name, require_version=False)
        assert transition.file_map(backup) == pinned
        assert transition.database_record(path, require_version=False) == before


def test_modified_autoincrement_sequence_is_rejected(case):
    root, data, _, _ = case
    with connect(data / 'eic.db') as connection:
        connection.execute('CREATE TABLE sequence_control(id INTEGER PRIMARY KEY AUTOINCREMENT)')
        connection.execute('INSERT INTO sequence_control DEFAULT VALUES')
    operations = Operations(root)
    upgrade = operations.upgrade
    def change(stage, attempt):
        upgrade(stage, attempt)
        with connect(stage / 'eic.db') as connection:
            connection.execute('UPDATE sqlite_sequence SET seq=999')
    operations.upgrade = change
    with pytest.raises(ValueError, match='自增序列'):
        execute(case, operations)


def test_snapshot_fingerprints_are_pinned_after_all_sqlite_normalization(tmp_path, monkeypatch):
    import secrets
    from types import SimpleNamespace
    import existing_host_release as host
    root, folder = tmp_path / 'host', tmp_path / 'snapshot'
    for part in ('persistent/data', 'persistent/uploads', 'persistent/experience-control', 'private'):
        (root / part).mkdir(parents=True)
    (root / 'private/backend.env').write_text(
        'EIC_DATABASE_URL=sqlite+aiosqlite:////app/data/eic.db\n'
        'EIC_VECTOR_DATABASE_URL=postgresql://fixture:fixture@vector/vector_fixture\n'
        'EIC_JWT_SECRET=' + secrets.token_hex(32) + '\n'
        'EIC_SECRET_ENCRYPTION_KEY=' + secrets.token_hex(32) + '\n', encoding='utf-8')
    (root / 'persistent/uploads/file.txt').write_text('synthetic')
    with connect(root / 'persistent/experience-control/control.sqlite') as control:
        control.execute('CREATE TABLE control(id INTEGER PRIMARY KEY)')
        control.execute('INSERT INTO control VALUES(1)')
    def inspect(identifier):
        return [{'State': {'Running': False}}] if identifier == 'eic-v4-backend' else [
            {'Config': {'Env': ['POSTGRES_USER=fixture', 'POSTGRES_DB=vector_fixture']}}]
    monkeypatch.setattr(host, 'inspect', inspect)
    monkeypatch.setattr(host, 'run', lambda *_: '0')
    def pg_dump(*args, **kwargs):
        kwargs['stdout'].write(b'synthetic vector archive')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(host.subprocess, 'run', pg_dump)
    layout = {'root': str(root), 'containers': {'eic-v4-frontend': {'mounts': []},
                                               'eic-v4-gateway': {'mounts': []}}}
    database = root / 'persistent/data/eic.db'
    with closing(sqlite3.connect(database, isolation_level=None)) as connection:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA wal_autocheckpoint=0')
        connection.execute('CREATE TABLE alembic_version(version_num TEXT PRIMARY KEY)')
        connection.execute('INSERT INTO alembic_version VALUES(?)', (transition.FROM_HEAD,))
        connection.execute('CREATE TABLE business(id INTEGER PRIMARY KEY, text TEXT)')
        connection.execute('INSERT INTO business VALUES(1, ?)', ('committed in wal',))
        record = host.snapshot_quiesced(layout, folder)
        metadata = json.loads((folder / 'MANIFEST.json').read_text())
        assert metadata['normalizedSqliteDatabases'] == [
            'persistent/data/eic.db', 'persistent/experience-control/control.sqlite']
        assert transition.file_map(folder) == {**metadata['files'], 'MANIFEST.json': record['manifestSha256']}
        assert transition.database_record(folder / 'persistent/data/eic.db') == transition.database_record(database)
        assert transition.file_map(folder) == {**metadata['files'], 'MANIFEST.json': record['manifestSha256']}
