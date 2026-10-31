"""迁移状态机的既有云端Docker适配；只能从完整发布门后调用。"""
from pathlib import Path
from contextlib import closing
import hashlib
import json
import re
import sqlite3
import subprocess
import time
from urllib.parse import urlsplit

import existing_host_release as host
from schema_transition import database_record, file_map, file_sha, require, safe_path, validate_transition, execute


class ExistingHostOperations:
    def __init__(self, layout, manifest, release, env_file):
        self.layout, self.manifest = layout, manifest
        self.release, self.env_file = Path(release), Path(env_file)
        self.stamp = self.release.name
        require(re.fullmatch(r'[a-z0-9-]{1,80}', self.stamp), '发布ID无效')
        self.names = ['eic-v4-' + role + '-' + self.stamp for role in ('backend', 'frontend')]
        self.migrators = ['eic-v4-migrate-' + self.stamp + '-' + str(index) for index in range(2)]
        environment = dict(line.split('=', 1) for line in host.filtered_environment(
            self.env_file.read_text(encoding='utf-8')).splitlines())
        require(environment['EIC_DATABASE_URL'].strip('\"\'') == 'sqlite+aiosqlite:////app/data/eic.db',
                '迁移只支持已审查的SQLite目录地址')
        self.environment = environment

    def _stop(self, identifier):
        host.run('docker', 'stop', '--time', '60', identifier, timeout=75)
        require(not host.inspect(identifier)[0]['State']['Running'], '容器停写失败')

    def _candidates(self):
        text = host.run('docker', 'ps', '-aq', '--no-trunc', '--filter', 'label=eic.release.id=' + self.stamp)
        rows = host.inspect(*text.splitlines()) if text else []
        for row in rows:
            role = row['Config'].get('Labels', {}).get('eic.release.role')
            require(role in {'backend', 'frontend', 'migrator', 'restore'}, '候选容器标签未知')
            expected_image = (self.layout['containers']['eic-v4-vector']['image'] if role == 'restore'
                              else self.manifest['images']['frontend' if role == 'frontend' else 'backend']['digest'])
            require(row['Image'] == expected_image, '候选容器镜像身份漂移')
        return rows

    def quiesce(self):
        # gateway首先停，避免新HTTP请求继续进入。
        for role in ('gateway', 'backend', 'frontend'):
            identifier = self.layout['containers']['eic-v4-' + role]['id']
            self._stop(identifier)
            state = host.inspect(identifier)[0]['State']
            require(not state.get('OOMKilled') and state.get('ExitCode') != 137, '既有服务未正常退出')

    def assert_quiesced(self):
        for role in ('gateway', 'backend', 'frontend'):
            actual = host.inspect(self.layout['containers']['eic-v4-' + role]['id'])[0]
            require(not actual['State']['Running'], '既有写入者或网关仍运行')
        require(all(not row['State']['Running'] for row in self._candidates()), '候选仍在写入')
        # 另一个Docker容器即使不在应用网络，也不得写相同持久目录。
        ids = host.run('docker', 'ps', '-q', '--no-trunc').splitlines()
        protected = Path(self.layout['root']) / 'persistent'
        for row in host.inspect(*ids) if ids else []:
            for mount in row['Mounts']:
                source = Path(mount['Source'])
                if mount.get('RW') and (source == protected or protected.is_relative_to(source)
                                        or source.is_relative_to(protected)):
                    raise ValueError('持久目录存在未停写的容器')
        vector = host.inspect('eic-v4-vector')[0]
        env = dict(row.split('=', 1) for row in vector['Config']['Env'] if '=' in row)
        user = env.get('POSTGRES_USER', 'postgres')
        database = urlsplit(self.environment['EIC_VECTOR_DATABASE_URL'].strip('\"\'')).path.lstrip('/')
        require(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', database), '向量库名称无效')
        require(host.run('docker', 'exec', vector['Id'], 'psql', '-U', user, '-d', 'postgres', '-Atc',
                         "SELECT count(*) FROM pg_stat_activity WHERE datname='" + database + "' AND backend_type='client backend'") == '0',
                '向量库仍有客户端连接')

    def snapshot(self, destination):
        return host.snapshot_quiesced(self.layout, destination)

    def verify_backup(self, record):
        folder = safe_path(record['path'])
        require(folder == self.release / 'pre-switch-backup', '备份路径越界')
        require(file_sha(folder / 'MANIFEST.json') == record['manifestSha256'], '备份清单不匹配')
        manifest = json.loads((folder / 'MANIFEST.json').read_text(encoding='utf-8'))
        require(manifest.get('quiesced') is True, '备份未停写')
        actual = file_map(folder)
        actual.pop('MANIFEST.json')
        require(actual == manifest['files'], '备份成员哈希不匹配')
        root = Path(self.layout['root'])
        normalized = manifest.get('normalizedSqliteDatabases', [])
        require('persistent/data/eic.db' in normalized, '主数据库备份未规范化')
        original_files = {name + '/' + key: value for name in ('persistent', 'private')
                          for key, value in file_map(root / name).items()}
        backup_files = {key: value for key, value in actual.items() if key.startswith(('persistent/', 'private/'))}
        for relative in normalized:
            source, target = safe_path(root / relative), safe_path(folder / relative)
            require(relative.startswith('persistent/') and source.is_relative_to(root / 'persistent')
                    and target.is_relative_to(folder / 'persistent'), '备份数据库路径越界')
            require(database_record(source, require_version=False) == database_record(target, require_version=False),
                    '备份数据库内容不匹配')
            for suffix in ('', '-wal', '-shm', '-journal'):
                original_files.pop(relative + suffix, None)
                backup_files.pop(relative + suffix, None)
        require(original_files == backup_files, '备份与停写源不同')
        # 本次停写快照也必须实际恢复，归档目录列表成功不能替代恢复。
        restored = safe_path(self.release / 'sqlite-restored.db')
        require(not restored.exists(), '恢复副本已存在')
        backup_db = folder / 'persistent/data/eic.db'
        with closing(sqlite3.connect(backup_db.as_uri() + '?mode=ro', uri=True)) as source:
            with closing(sqlite3.connect(restored)) as destination:
                source.backup(destination)
        require(database_record(restored) == database_record(root / 'persistent/data/eic.db'), 'SQLite备份恢复不匹配')
        vector_proof = self.restore_vector(folder / 'vector.dump')
        require(file_map(folder) == {**manifest['files'], 'MANIFEST.json': record['manifestSha256']},
                '恢复核验改变了备份成员')
        return {'sqliteRestored': True, 'vectorRestored': True, 'vectorDataSha256': vector_proof,
                'quiesced': True, 'uploadsAndPrivateFilesVerified': True}

    def restore_vector(self, dump):
        name = 'eic-v4-restore-' + self.stamp
        original = self.layout['containers']['eic-v4-vector']
        storage = safe_path(self.release / 'vector-restore-data')
        storage.mkdir(mode=0o700, exist_ok=False)
        identifier = None
        try:
            host.run('docker', 'create', '--name', name, '--network', 'none', '--restart', 'no',
                     '--memory', str(original['memory']), '--label', 'eic.release.id=' + self.stamp,
                     '--label', 'eic.release.role=restore', '--env', 'POSTGRES_USER=eic_restore',
                     '--env', 'POSTGRES_DB=eic_restore', '--env', 'POSTGRES_HOST_AUTH_METHOD=trust',
                     '--mount', 'type=bind,src=' + str(storage) + ',dst=/var/lib/postgresql/data',
                     original['image'], '-c', 'shared_buffers=32MB', '-c', 'max_connections=10', timeout=60)
            identifier = host.inspect(name)[0]['Id']
            host.run('docker', 'start', identifier, timeout=60)
            deadline = time.monotonic() + 60
            while True:
                try:
                    host.run('docker', 'exec', identifier, 'pg_isready', '-U', 'eic_restore', '-d', 'eic_restore', timeout=5)
                    break
                except (ValueError, OSError, subprocess.TimeoutExpired):
                    require(time.monotonic() < deadline, '隔离恢复数据库启动超时')
                    time.sleep(min(1, max(0, deadline - time.monotonic())))
            host.run('docker', 'cp', str(dump), identifier + ':/tmp/restore.dump', timeout=60)
            host.run('docker', 'exec', identifier, 'pg_restore', '-U', 'eic_restore', '-d', 'eic_restore',
                     '--exit-on-error', '--single-transaction', '--no-owner', '--no-privileges', '/tmp/restore.dump', timeout=180)
            env = dict(row.split('=', 1) for row in host.inspect(original['id'])[0]['Config']['Env'] if '=' in row)
            user = env.get('POSTGRES_USER', 'postgres')
            database = urlsplit(self.environment['EIC_VECTOR_DATABASE_URL'].strip('\"\'')).path.lstrip('/')
            expected = self.vector_data_digest(original['id'], user, database, 'vector-source-data.txt')
            actual = self.vector_data_digest(identifier, 'eic_restore', 'eic_restore', 'vector-restored-data.txt')
            require(actual == expected, '向量恢复后的数据摘要不匹配')
            return actual
        finally:
            # create响应也可能丢失；按唯一名字查询并严格核验后清理，仅处理本次恢复容器。
            found = host.run('docker', 'ps', '-aq', '--no-trunc', '--filter', 'name=^/' + name + '$')
            if found:
                actual = host.inspect(found)[0]
                labels = actual['Config'].get('Labels', {})
                require(actual['Image'] == original['image'] and labels.get('eic.release.id') == self.stamp
                        and labels.get('eic.release.role') == 'restore', '恢复容器身份漂移，拒绝清理')
                require(all(mount['Type'] == 'bind' and Path(mount['Source']) == storage
                            for mount in actual['Mounts']), '恢复容器挂载漂移，拒绝清理')
                if actual['State']['Running']:
                    self._stop(actual['Id'])
                host.run('docker', 'rm', actual['Id'], timeout=60)

    def vector_data_digest(self, identifier, user, database, filename):
        destination = safe_path(self.release / filename)
        command = ['docker', 'exec', identifier, 'pg_dump', '-U', user, '-d', database,
                   '--data-only', '--no-owner', '--no-privileges', '--inserts']
        with destination.open('xb') as stream:
            completed = subprocess.run(command, stdout=stream, stderr=subprocess.PIPE, timeout=180)
        require(completed.returncode == 0, '向量数据只读导出失败')
        digest = hashlib.sha256()
        with destination.open('rb') as stream:
            for row in stream:
                if not row.startswith((b'\\restrict ', b'\\unrestrict ')):
                    digest.update(row)
        return digest.hexdigest()

    def upgrade(self, stage, attempt):
        argv = ['docker', 'create', '--name', self.migrators[attempt], '--network', 'none',
                '--restart', 'no', '--memory', str(self.layout['containers']['eic-v4-backend']['memory']),
                '--label', 'eic.release.id=' + self.stamp, '--label', 'eic.release.role=migrator',
                '--env-file', str(self.env_file), '--mount', 'type=bind,src=' + str(stage) + ',dst=/app/data',
                self.manifest['images']['backend']['digest'], '/app/.venv/bin/python', '-B', '-m', 'eic.core.schema', 'upgrade']
        host.run(*argv, timeout=60)
        host.run('docker', 'start', '-a', self.migrators[attempt], timeout=180)
        actual = host.inspect(self.migrators[attempt])[0]
        require(not actual['State']['Running'] and actual['State']['ExitCode'] == 0
                and not actual['State'].get('OOMKilled'), '迁移容器失败')

    def assert_migrator_stopped(self):
        for row in self._candidates():
            if row['Config']['Labels']['eic.release.role'] == 'migrator':
                require(not row['State']['Running'], '迁移器尚未终态')

    def prepare_candidates(self):
        for role in ('backend', 'frontend'):
            host.run('docker', 'network', 'disconnect', self.layout['network'],
                     self.layout['containers']['eic-v4-' + role]['id'])
            host.run(*host.container_command(role, self.layout, self.manifest, self.stamp, self.env_file))

    def start_candidates(self):
        for name in self.names:
            host.run('docker', 'start', name, timeout=60)

    def check_candidates(self):
        host.run('docker', 'exec', self.names[0], '/app/.venv/bin/python', '-B', '-m', 'eic.core.schema', 'check', timeout=60)
        host.run('docker', 'exec', self.names[1], 'nginx', '-t', timeout=30)
        host.wait_ready(*self.names, check_gateway=False)

    def open_gateway(self):
        host.run('docker', 'start', self.layout['containers']['eic-v4-gateway']['id'], timeout=30)
        host.run('docker', 'exec', 'eic-v4-gateway', 'nginx', '-t', timeout=30)
        host.run('docker', 'exec', 'eic-v4-gateway', 'nginx', '-s', 'reload', timeout=30)

    def check_gateway(self):
        host.gateway_health(self.names[1])

    def finalize(self):
        for role, name in zip(('backend', 'frontend'), self.names):
            host.run('docker', 'rename', self.layout['containers']['eic-v4-' + role]['id'],
                     'eic-v4-' + role + '-rollback-' + self.stamp)
            host.run('docker', 'rename', name, 'eic-v4-' + role)

    def contain_failure(self):
        # 单项失败仍继续停止其他写入者，最后统一拒绝恢复。
        errors = []
        def attempt(action):
            try:
                return action()
            except BaseException:
                errors.append(True)
                return None
        attempt(lambda: self._stop(self.layout['containers']['eic-v4-gateway']['id']))
        rows = attempt(self._candidates)
        for row in rows or []:
            if row['State']['Running']:
                attempt(lambda: self._stop(row['Id']))
        for role in ('backend', 'frontend'):
            attempt(lambda: self._stop(self.layout['containers']['eic-v4-' + role]['id']))
        require(not errors, '失败收口未能确认全部写入者停止')

    def resume_original(self):
        # 此方法只在状态机确认未启动候选写入者且原目录已还原时调用。
        for row in self._candidates():
            if self.layout['network'] in row['NetworkSettings']['Networks']:
                host.run('docker', 'network', 'disconnect', self.layout['network'], row['Id'])
        for role in ('backend', 'frontend'):
            old_id = self.layout['containers']['eic-v4-' + role]['id']
            state = host.inspect(old_id)[0]
            if self.layout['network'] not in state['NetworkSettings']['Networks']:
                host.run('docker', 'network', 'connect', '--alias', role, self.layout['network'], old_id)
            host.run('docker', 'start', old_id, timeout=60)
        self.open_gateway()
        host.wait_ready('eic-v4-backend', 'eic-v4-frontend')


def apply_transition(layout, manifest, release, env_file):
    validate_transition(manifest.get('schema_transition'), manifest)
    operations = ExistingHostOperations(layout, manifest, release, env_file)
    return execute(layout, manifest, release, operations)
