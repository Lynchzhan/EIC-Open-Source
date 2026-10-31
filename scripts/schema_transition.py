"""受限 SQLite 增量发布事务；候选写入者启动前可回退目录。"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3

FROM_HEAD = '0039_model_search_mode'
TO_HEAD = '0041_sparse_index'
ADDED_TABLES = frozenset({'revoked_sessions', 'langfuse_deliveries',
                          'knowledge_sparse_chunks', 'knowledge_sparse_terms'})
MEMORY_HEAD = '0042_memory_search'
MEMORY_TABLES = frozenset({'memory_search_documents', 'memory_search_terms', 'memory_projection_receipts'})
# 仅允许明确审查过的纯加表路径；旧冻结计划仍使用原来的精确合同。
ALLOWED_TRANSITIONS = {
    (FROM_HEAD, TO_HEAD): ADDED_TABLES,
    (FROM_HEAD, MEMORY_HEAD): ADDED_TABLES | MEMORY_TABLES,
    (TO_HEAD, MEMORY_HEAD): MEMORY_TABLES,
}


def require(value, message):
    if not value:
        raise ValueError(message)


def file_sha(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def safe_path(path):
    path = Path(path).absolute()
    require(not any(item.is_symlink() or getattr(item, 'is_junction', lambda: False)()
                    for item in (path, *path.parents)), '迁移路径含链接')
    return path


def fsync_directory(path):
    if os.name != 'nt':
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def file_map(folder, *, database=False):
    rows = {}
    for directory, directories, filenames in os.walk(folder, followlinks=False):
        for name in [*directories, *filenames]:
            path = safe_path(Path(directory) / name)
            if path.is_file():
                require(path.stat().st_nlink == 1, '迁移文件含硬链接')
                relative = path.relative_to(folder).as_posix()
                if database and relative in {'eic.db', 'eic.db-wal', 'eic.db-shm'}:
                    continue
                rows[relative] = file_sha(path)
    return rows


def database_record(path, *, require_version=True):
    """只返回表结构与内容指纹，不输出业务正文。"""
    path = safe_path(path)
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as connection:
        require(connection.execute('PRAGMA integrity_check').fetchall() == [('ok',)], 'SQLite完整性失败')
        require(not connection.execute('PRAGMA foreign_key_check').fetchall(), 'SQLite外键失败')
        versioned = connection.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='alembic_version'").fetchone()[0]
        heads = connection.execute('SELECT version_num FROM alembic_version').fetchall() if versioned else []
        require(not require_version or len(heads) == 1, '数据库必须是单head')
        tables = {}
        for name, sql in connection.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            if name == 'alembic_version':
                continue
            quoted = '"' + name.replace('"', '""') + '"'
            hashes = []
            for row in connection.execute('SELECT * FROM ' + quoted):
                encoded = json.dumps([{'blob': value.hex()} if isinstance(value, bytes) else value
                                      for value in row], ensure_ascii=False, separators=(',', ':'),
                                     allow_nan=False).encode()
                hashes.append(hashlib.sha256(encoded).digest())
            tables[name] = {'count': len(hashes), 'rowsSha256': hashlib.sha256(b''.join(sorted(hashes))).hexdigest(),
                            'schemaSha256': hashlib.sha256(sql.encode()).hexdigest()}
        # 迁移也不得暗改旧表的索引和触发器。
        objects = {name: hashlib.sha256((sql or '').encode()).hexdigest()
                   for name, table, sql in connection.execute(
                       "SELECT name,tbl_name,sql FROM sqlite_master WHERE type IN ('index','trigger','view')")
                   if table in tables}
        has_sequence = connection.execute("SELECT count(*) FROM sqlite_master WHERE name='sqlite_sequence'").fetchone()[0]
        sequences = connection.execute('SELECT name,seq FROM sqlite_sequence ORDER BY name').fetchall() if has_sequence else []
    return {'head': heads[0][0] if heads else None, 'tables': tables, 'objects': objects, 'sequences': sequences}


def normalize_backup_database(path):
    """只规范化备份目录内的SQLite；保留所有已提交WAL记录。"""
    path = safe_path(path)
    before = database_record(path, require_version=False)
    temporary = safe_path(path.with_name(path.name + '.canonical'))
    require(not temporary.exists(), '备份规范化临时文件已存在')
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as source:
        with closing(sqlite3.connect(temporary)) as target:
            source.backup(target)
    # backup API可能保留WAL模式，关闭所有句柄后，副本侧car不再有未合并写入。
    with closing(sqlite3.connect(temporary)) as target:
        require(target.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 0, '备份checkpoint失败')
        require(target.execute('PRAGMA journal_mode=DELETE').fetchone()[0] == 'delete', '备份日志模式规范化失败')
    for suffix in ('-wal', '-shm', '-journal'):
        sidecar = safe_path(path.with_name(path.name + suffix))
        if sidecar.exists():
            sidecar.unlink()
    temporary.replace(path)
    path.chmod(0o600)
    require(database_record(path, require_version=False) == before, '备份规范化丢失数据')


def validate_transition(plan, manifest):
    require(isinstance(plan, dict), '迁移计划必须为对象')
    source, target = plan.get('fromHead'), plan.get('toHead')
    require(isinstance(source, str) and isinstance(target, str)
            and (source, target) in ALLOWED_TRANSITIONS, '迁移路径不在已审查范围')
    require(type(plan.get('schemaVersion')) is int
            and plan == {'schemaVersion': 1, 'fromHead': source, 'toHead': target,
                     'strategy': 'sqlite_directory_swap', 'commitBoundary': 'before_candidate_start',
                     'postCommitFailure': 'stop_writers_keep_new_data'}, '迁移计划不在已审查范围')
    require(manifest.get('schema_head') == target, '候选schema与计划不同')
    require(re.fullmatch(r'sha256:[a-f0-9]{64}', manifest['images']['backend']['digest']), '候选镜像ID无效')


def assert_upgraded(before, after):
    added = ALLOWED_TRANSITIONS.get((before['head'], after['head']))
    require(added is not None, '迁移head不匹配')
    require(set(after['tables']) == set(before['tables']) | added, '迁移表集合超出批准范围')
    require(not (added & set(before['tables'])), '源库提前存在候选表')
    require(all(after['tables'][key] == value for key, value in before['tables'].items()), '原业务表被修改')
    require(all(after['tables'][name]['count'] == 0 for name in added), 'schema迁移不允许业务回填')
    require(all(after['objects'].get(key) == value for key, value in before['objects'].items()), '原索引或触发器被修改')
    require(after['sequences'] == before['sequences'], '原自增序列被修改')


class Journal:
    def __init__(self, path):
        self.path = safe_path(path)
        # 不自动续跑；掉电现场必须先按真实目录和容器身份恢复。
        self.stream = self.path.open('x', encoding='utf-8', newline='\n')
        if os.name != 'nt':
            self.path.chmod(0o600)

    def event(self, phase, **data):
        self.stream.write(json.dumps({'phase': phase, **data}, sort_keys=True) + '\n')
        self.stream.flush()
        os.fsync(self.stream.fileno())
        fsync_directory(self.path.parent)

    def close(self):
        self.stream.close()


def directory_identity(path):
    metadata = safe_path(path).stat()
    require(Path(path).is_dir(), '迁移对象不是目录')
    return [metadata.st_dev, metadata.st_ino]


def restore_directories(data, stage, original, original_identity, candidate_identity):
    # rename可能已成功但响应/后续赋值中断，必须按真实目录身份判断。
    if original.exists():
        require(directory_identity(original) == original_identity, '原目录身份漂移，拒绝回退')
        if data.exists():
            require(candidate_identity is not None and directory_identity(data) == candidate_identity
                    and not stage.exists(), '当前目录身份不明，拒绝覆盖')
            data.rename(stage)
            fsync_directory(data.parent)
        original.rename(data)
        fsync_directory(data.parent)
    require(data.exists() and directory_identity(data) == original_identity, '原目录未恢复')
    fsync_directory(data.parent)


def sync_tree(folder):
    for directory, _, names in os.walk(folder):
        for name in names:
            with safe_path(Path(directory) / name).open('r+b') as stream:
                os.fsync(stream.fileno())
        fsync_directory(directory)


def execute(layout, manifest, release, operations):
    """operations负责真实容器操作；此函数不提供独立生产CLI或跳过门禁的入口。"""
    validate_transition(manifest.get('schema_transition'), manifest)
    root, release = safe_path(layout['root']), safe_path(release)
    require(release.is_relative_to(root / 'releases') and release.parent == root / 'releases', '发布目录越界')
    require(release.is_dir(), '发布目录缺失')
    data = safe_path(root / 'persistent/data')
    stage = safe_path(data.with_name('data-candidate-' + release.name))
    original = safe_path(data.with_name('data-original-' + release.name))
    require(data.is_dir() and not stage.exists() and not original.exists(), '迁移目录已存在或源缺失')
    require(data.stat().st_dev == release.stat().st_dev, '候选与数据目录须同文件系统')
    original_identity = directory_identity(data)
    candidate_identity = None
    lock = safe_path(root / 'persistent/.schema-transition.lock')
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.write(descriptor, release.name.encode('ascii'))
    os.fsync(descriptor)
    os.close(descriptor)
    fsync_directory(lock.parent)
    journal = Journal(release / 'migration-journal.jsonl')
    result = {'productionSwitchPerformed': False, 'commitBoundaryCrossed': False,
              'rollbackPerformed': False, 'userAcceptance': 'pending'}
    committed = False
    recovered = False
    try:
        journal.event('quiesce_intent')
        operations.quiesce()
        operations.assert_quiesced()
        journal.event('quiesced', originalDirectoryIdentity=original_identity)
        result['backup'] = operations.snapshot(release / 'pre-switch-backup')
        result['backupVerification'] = operations.verify_backup(result['backup'])
        journal.event('backup_verified', manifestSha256=result['backup']['manifestSha256'])
        before = database_record(data / 'eic.db')
        require(before['head'] == manifest['schema_transition']['fromHead'], '生产head与批准计划不同')
        ancillary = file_map(data, database=True)
        shutil.copytree(data, stage)
        # 完整目录复制后，仅在候选目录规范化SQLite，原库/WAL保持原样。
        for suffix in ('', '-wal', '-shm'):
            candidate = stage / ('eic.db' + suffix)
            if candidate.exists():
                candidate.unlink()
        with closing(sqlite3.connect((data / 'eic.db').as_uri() + '?mode=ro', uri=True)) as source:
            with closing(sqlite3.connect(stage / 'eic.db')) as target:
                source.backup(target)
        require(database_record(stage / 'eic.db') == before, '候选复制与原数据库不同')
        for index in range(2):
            journal.event('upgrade_intent', attempt=index + 1)
            operations.upgrade(stage, index)
            operations.assert_migrator_stopped()
            after = database_record(stage / 'eic.db')
            require(after['head'] == manifest['schema_transition']['toHead'], '迁移终点与批准计划不同')
            assert_upgraded(before, after)
            require(file_map(stage, database=True) == ancillary, '迁移修改了其他数据文件')
            journal.event('upgrade_verified', attempt=index + 1)
        require(database_record(data / 'eic.db') == before and file_map(data, database=True) == ancillary,
                '停写期间源数据发生变化')
        operations.assert_quiesced()
        sync_tree(stage)
        candidate_identity = directory_identity(stage)
        journal.event('move_original_intent', candidateDirectoryIdentity=candidate_identity)
        data.rename(original)
        fsync_directory(data.parent)
        journal.event('original_moved')
        stage.rename(data)
        fsync_directory(data.parent)
        journal.event('candidate_activated')
        require(database_record(data / 'eic.db') == after, '目录切换后数据库不同')
        operations.prepare_candidates()
        # 必须先持久化，再调用可能启动后台写入者的docker start。
        journal.event('commit_before_candidate_start')
        committed = True
        result['commitBoundaryCrossed'] = True
        operations.start_candidates()
        operations.check_candidates()
        journal.event('candidates_ready')
        operations.open_gateway()
        operations.check_gateway()
        operations.finalize()
        result['productionSwitchPerformed'] = True
        journal.event('released')
        recovered = True
    except BaseException as error:
        result['failureType'] = type(error).__name__
        errors = []
        # 不输出原异常消息，避免配置或业务正文进入报告。
        try:
            journal.event('failure', errorType=type(error).__name__, committed=committed)
        except BaseException:
            errors.append('persist_failure')
        try:
            operations.contain_failure()
            operations.assert_migrator_stopped()
            operations.assert_quiesced()
        except BaseException:
            errors.append('quiesce_failed')
        if not committed and not errors:
            try:
                restore_directories(data, stage, original, original_identity, candidate_identity)
                operations.resume_original()
                result['rollbackPerformed'] = True
                recovered = True
            except BaseException:
                errors.append('rollback_failed')
                try:
                    operations.contain_failure()
                except BaseException:
                    errors.append('contain_rollback_failed')
        result['recoveryErrors'] = errors
        result['manualRecoveryRequired'] = committed or bool(errors)
        journal.event('recovery_result', **result)
        raise
    finally:
        try:
            journal.close()
            with (release / 'migration-result.json').open('x', encoding='utf-8') as stream:
                json.dump(result, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            fsync_directory(release)
            if recovered:
                lock.unlink()
                fsync_directory(lock.parent)
        finally:
            # 失败且未恢复的锁和目录保留，下一次运行拒绝接管。
            pass
    return result
