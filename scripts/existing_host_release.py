"""既有目录挂载布局发布；由release_deploy进入，保留完整门及原部署回滚。"""
from __future__ import annotations

import hashlib
from contextlib import closing
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import sqlite3
import subprocess
import time
from urllib.request import urlopen


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_layout(layout, remote_dir):
    require(layout.get('schemaVersion') == 1 and layout.get('root') == remote_dir,
            '布局版本或项目根目录不匹配')
    require(re.fullmatch(r'/opt/[a-zA-Z0-9][a-zA-Z0-9_/-]*', remote_dir)
            and '..' not in PurePosixPath(remote_dir).parts, '项目路径无效')
    require(re.fullmatch(r'[a-z0-9][a-z0-9-]{2,63}', layout.get('network', '')), '网络名称无效')
    containers = layout.get('containers', {})
    require(set(containers) == {'eic-v4-' + role for role in ('backend', 'frontend', 'vector', 'search', 'gateway')},
            '只接受明确盘点的五个既有服务')
    for name, row in containers.items():
        require(re.fullmatch(r'[a-f0-9]{64}', row.get('id', ''))
                and re.fullmatch(r'sha256:[a-f0-9]{64}', row.get('image', '')), '容器身份不完整')
        require(row.get('running') is True and set(row.get('networks', {})) == {layout['network']},
                '既有服务必须运行且只连接审查过的网络')
        require(row.get('restartPolicy') == {'Name': 'unless-stopped', 'MaximumRetryCount': 0},
                '既有重启策略未受支持')
        require(isinstance(row.get('memory'), int) and row['memory'] > 0, '服务内存限制缺失')
    expected = {(remote_dir + '/persistent/' + part, '/app/' + part)
                for part in ('data', 'uploads', 'experience-control')}
    mounts = containers['eic-v4-backend'].get('mounts', [])
    require(len(mounts) == 3 and {(row['Source'], row['Destination']) for row in mounts} == expected
            and all(row['Type'] == 'bind' and row['RW'] is True for row in mounts),
            '必须保留三个原业务目录挂载')
    require(not containers['eic-v4-backend']['ports'] and not containers['eic-v4-frontend']['ports'],
            '应用服务不得新增公网端口')
    require(containers['eic-v4-gateway']['ports'] == {
        '80/tcp': [{'HostIp': '0.0.0.0', 'HostPort': '51671'}]}, '网关端口布局未受支持')
    for name, expected_source in [('frontend', remote_dir + '/source/frontend/nginx.conf'),
                                   ('gateway', remote_dir + '/source/deploy/cloud-clean/gateway.conf')]:
        mounts = containers['eic-v4-' + name]['mounts']
        if name == 'frontend' and not mounts:
            continue
        require(len(mounts) == 1 and mounts[0]['Type'] == 'bind' and mounts[0]['Source'] == expected_source
                and mounts[0]['Destination'] == '/etc/nginx/conf.d/default.conf' and mounts[0]['RW'] is False,
                '代理配置挂载未受支持')
    expected_files = {remote_dir + path for path in ('/private/backend.env', '/private/vector.env',
        '/source/frontend/nginx.conf', '/source/deploy/cloud-clean/gateway.conf')}
    minimum_files = expected_files - {remote_dir + '/source/frontend/nginx.conf'}
    require(minimum_files <= set(layout.get('fileSha256', {})) <= expected_files, '配置指纹集合不完整')
    if containers['eic-v4-frontend']['mounts']:
        require(set(layout['fileSha256']) == expected_files, '既有前端挂载配置缺少指纹')
    require(all(re.fullmatch(r'[a-f0-9]{64}', value) for value in layout['fileSha256'].values()), '配置指纹无效')


def filtered_environment(text):
    """只保留应用配置；不继承旧镜像PATH、PYTHONPATH或系统版本变量。"""
    rows = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, separator, value = line.partition('=')
        require(separator and key == key.strip() and re.fullmatch(r'[A-Z][A-Z0-9_]*', key),
                '环境文件含不支持的语法')
        if key.startswith(('EIC_', 'LANGFUSE_', 'OTEL_')):
            require(key not in rows, '环境文件应用键重复')
            rows[key] = value
    required = {'EIC_DATABASE_URL', 'EIC_VECTOR_DATABASE_URL', 'EIC_JWT_SECRET', 'EIC_SECRET_ENCRYPTION_KEY'}
    require(required <= rows.keys() and all(rows[key] for key in required), '应用凭据或数据地址缺失')
    secrets = [rows[key].strip('"\'') for key in ('EIC_JWT_SECRET', 'EIC_SECRET_ENCRYPTION_KEY')]
    require(all(len(value) >= 32 and len(set(value)) >= 12
                and not any(marker in value.lower() for marker in ('replace-', 'change-me', 'changeme', 'example', 'placeholder'))
                for value in secrets) and secrets[0] != secrets[1], '生产密钥强度不足或未分离')
    require(rows.get('EIC_AUTO_CREATE_SCHEMA', 'false').strip('"\'').lower() == 'false'
            and rows.get('EIC_RUNTIME_SCHEMA_UPDATES', 'false').strip('"\'').lower() == 'false',
            '既有布局禁止隐式迁移schema')
    return '\n'.join(key + '=' + value for key, value in rows.items()) + '\n'


def run(*argv, **kwargs):
    result = subprocess.run(argv, capture_output=True, **kwargs)
    require(result.returncode == 0, '发布命令失败: ' + argv[0] + '，退出码=' + str(result.returncode))
    return result.stdout.decode('utf-8').strip()


def inspect(*names):
    return json.loads(run('docker', 'inspect', *names))


def verify_live(layout):
    validate_layout(layout, layout['root'])
    names = list(layout['containers'])
    live = inspect(*names)
    network = json.loads(run('docker', 'network', 'inspect', layout['network']))[0]
    require(set(network.get('Containers', {})) == {row['id'] for row in layout['containers'].values()},
            '网络存在未盘点服务或重复别名')
    for name, actual in zip(names, live):
        expected = layout['containers'][name]
        require(actual['Id'] == expected['id'] and actual['Image'] == expected['image']
                and actual['State']['Running'] is True, '生产容器身份漂移: ' + name)
        mounts = [{key: row.get(key) for key in ('Type', 'Source', 'Destination', 'RW', 'Name')}
                  for row in actual['Mounts']]
        require(sorted(mounts, key=lambda row: row['Destination']) == sorted(expected['mounts'], key=lambda row: row['Destination'])
                and actual['HostConfig']['PortBindings'] == expected['ports'],
                '生产挂载或端口漂移: ' + name)
        networks = {key: {'aliases': value.get('Aliases')} for key, value in actual['NetworkSettings']['Networks'].items()}
        require(networks == expected['networks'] and actual['HostConfig']['Memory'] == expected['memory']
                and actual['HostConfig']['RestartPolicy'] == expected['restartPolicy'], '运行约束漂移: ' + name)
    configured = dict(line.split('=', 1) for line in filtered_environment(
        (Path(layout['root']) / 'private/backend.env').read_text()).splitlines())
    effective = {key: value for key, value in (line.split('=', 1) for line in live[names.index('eic-v4-backend')]['Config']['Env'])
                 if key.startswith(('EIC_', 'LANGFUSE_', 'OTEL_'))}
    require(configured == effective, '私密配置文件与当前生效的应用配置不同，必须先审查差异键')
    for filename, expected in layout['fileSha256'].items():
        path = Path(filename)
        require(not any(item.is_symlink() for item in (path, *path.parents)) and sha(path) == expected,
                '配置文件漂移或链接')
    for part in ('data', 'uploads', 'experience-control'):
        path = Path(layout['root']) / 'persistent' / part
        require(path.is_dir() and not any(item.is_symlink() for item in (path, *path.parents)), '业务目录含链接')
    return live


def container_command(role, layout, manifest, stamp, env_file):
    old = layout['containers']['eic-v4-' + role]
    argv = ['docker', 'create', '--name', 'eic-v4-' + role + '-' + stamp,
            '--label', 'eic.release.id=' + stamp, '--label', 'eic.release.role=' + role,
            '--network', layout['network'], '--network-alias', role,
            '--restart', 'unless-stopped', '--memory', str(old['memory'])]
    if role == 'backend':
        argv += ['--env-file', str(env_file)]
        for mount in old['mounts']:
            argv += ['--mount', 'type=bind,src=' + mount['Source'] + ',dst=' + mount['Destination']]
        argv += [manifest['images'][role]['digest'], '/app/.venv/bin/python', '-m', 'uvicorn',
                 'eic.main:app', '--host', '0.0.0.0', '--port', '8000']
    else:
        # 前端代理配置随不可变镜像发布，不再由历史宿主机挂载覆盖。
        argv += [manifest['images'][role]['digest']]
    return argv


def snapshot_quiesced(layout, folder):
    """停写后保存当前恢复点，私密材料只留云端0700目录。"""
    require(inspect('eic-v4-backend')[0]['State']['Running'] is False, '快照前后端仍在写入')
    folder.mkdir(mode=0o700, exist_ok=False)
    root = Path(layout['root'])
    files = {}
    for part in ('persistent', 'private'):
        source = root / part
        for item in source.rglob('*'):
            require(not item.is_symlink(), '快照不允许符号链接')
        shutil.copytree(source, folder / part)
    (folder / 'proxy-config').mkdir()
    for role in ('frontend', 'gateway'):
        for mount in layout['containers']['eic-v4-' + role]['mounts']:
            source = Path(mount['Source'])
            require(not source.is_symlink() and source.is_file(), '代理配置不是普通文件')
            shutil.copy2(source, folder / 'proxy-config' / (role + '.conf'))
    for path in folder.rglob('*'):
        path.chmod(0o700 if path.is_dir() else 0o600)
    normalized_databases = []
    from schema_transition import normalize_backup_database, sync_tree
    for path in (folder / 'persistent').rglob('*'):
        if path.suffix in {'.db', '.sqlite', '.sqlite3'}:
            normalize_backup_database(path)
            normalized_databases.append(path.relative_to(folder).as_posix())
    vector = inspect('eic-v4-vector')[0]
    env = dict(row.split('=', 1) for row in vector['Config']['Env'] if '=' in row)
    user, database = env.get('POSTGRES_USER', 'postgres'), env.get('POSTGRES_DB', env.get('POSTGRES_USER', 'postgres'))
    from urllib.parse import urlsplit
    configured = dict(line.split('=', 1) for line in filtered_environment((root / 'private/backend.env').read_text()).splitlines())
    require(urlsplit(configured['EIC_VECTOR_DATABASE_URL'].strip('"\'')).path.lstrip('/') == database,
            '向量连接目标与快照库不一致')
    require(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', database), '向量库名称无效')
    require(run('docker', 'exec', 'eic-v4-vector', 'psql', '-U', user, '-d', 'postgres', '-Atc',
                "SELECT count(*) FROM pg_stat_activity WHERE datname='" + database + "' AND state != 'idle'") == '0',
            '向量库仍有在途事务')
    with (folder / 'vector.dump').open('xb') as output:
        completed = subprocess.run(['docker', 'exec', 'eic-v4-vector', 'pg_dump', '-U', user,
                                    '-d', database, '-Fc'], stdout=output, stderr=subprocess.PIPE, timeout=180)
        require(completed.returncode == 0, 'PGVector快照失败')
    (folder / 'vector.dump').chmod(0o600)
    # SQLite规范化和检查全部结束后再固定成员哈希，避免只读WAL共享内存改变快照。
    for path in folder.rglob('*'):
        if path.is_file():
            files[path.relative_to(folder).as_posix()] = sha(path)
    with (folder / 'MANIFEST.json').open('x') as stream:
        json.dump({'files': files, 'quiesced': True, 'secretsLogged': False,
                   'normalizedSqliteDatabases': sorted(normalized_databases)}, stream, indent=2)
    (folder / 'MANIFEST.json').chmod(0o600)
    sync_tree(folder)
    return {'path': str(folder), 'manifestSha256': sha(folder / 'MANIFEST.json')}


def rollback_existing(layout, new_names, manifest):
    """逐步尽力恢复；单步失败不截断后续恢复，候选未停写时不启动旧后端。"""
    errors = []
    backend_safe = True
    stamp = new_names[0][len('eic-v4-backend-'):]

    def attempt(label, action):
        try:
            return action()
        except Exception as exc:
            errors.append({'step': label, 'errorType': type(exc).__name__})
            return None

    for role, name in reversed(list(zip(('backend', 'frontend'), new_names))):
        # create/start的响应可能丢失，因此按本次唯一名称检查真实状态。
        found = attempt('find_candidate_' + role, lambda: run('docker', 'ps', '-aq', '--no-trunc',
            '--filter', 'label=eic.release.id=' + stamp, '--filter', 'label=eic.release.role=' + role))
        if found is None:
            if role == 'backend': backend_safe = False
            continue
        if not found:
            continue
        actual = attempt('inspect_candidate_' + role, lambda: inspect(found)[0])
        if actual is None or actual['Image'] != manifest['images'][role]['digest']:
            errors.append({'step': 'candidate_identity_' + role, 'errorType': 'IdentityMismatch'})
            if role == 'backend': backend_safe = False
            continue
        attempt('stop_candidate_' + role, lambda: run('docker', 'stop', '--time', '60', found, timeout=75))
        state = attempt('check_stopped_' + role, lambda: inspect(found)[0])
        if state is None or state['State']['Running']:
            if role == 'backend': backend_safe = False
            errors.append({'step': 'candidate_still_running_' + role, 'errorType': 'UnsafeToResumeOld'})
        if state is not None and layout['network'] in state['NetworkSettings']['Networks']:
            attempt('disconnect_candidate_' + role,
                    lambda: run('docker', 'network', 'disconnect', layout['network'], found))
        if state is not None and state.get('Name') == '/eic-v4-' + role:
            attempt('rename_failed_candidate_' + role, lambda: run('docker', 'rename', found, name))
    for role in ('backend', 'frontend'):
        old = 'eic-v4-' + role
        old_id = layout['containers'][old]['id']
        actual = attempt('inspect_original_' + role, lambda: inspect(old_id)[0])
        if actual is None or actual['Id'] != old_id:
            errors.append({'step': 'original_identity_' + role, 'errorType': 'IdentityMismatch'})
            continue
        if role == 'backend' and not backend_safe:
            errors.append({'step': 'resume_original_backend', 'errorType': 'CandidateNotQuiesced'})
            continue
        if actual.get('Name', '/' + old) != '/' + old:
            attempt('rename_original_' + role, lambda: run('docker', 'rename', old_id, old))
        if layout['network'] not in actual['NetworkSettings']['Networks']:
            attempt('reconnect_original_' + role,
                    lambda: run('docker', 'network', 'connect', '--alias', role, layout['network'], old_id))
        attempt('start_original_' + role, lambda: run('docker', 'start', old_id))
    attempt('gateway_config', lambda: run('docker', 'exec', 'eic-v4-gateway', 'nginx', '-t'))
    attempt('gateway_reload', lambda: run('docker', 'exec', 'eic-v4-gateway', 'nginx', '-s', 'reload'))
    healthy = attempt('rollback_health', lambda: wait_ready('eic-v4-backend', 'eic-v4-frontend'))
    return {'rollbackPerformed': not errors and healthy is True, 'rollbackErrors': errors}


def remaining_time(deadline, maximum=10):
    remaining = deadline - time.monotonic()
    require(remaining > 0, '服务恢复就绪超时')
    return min(maximum, remaining)


def gateway_health(frontend='eic-v4-frontend', *, deadline=None):
    deadline = deadline if deadline is not None else time.monotonic() + 90
    with urlopen('http://127.0.0.1:51671/health', timeout=remaining_time(deadline)) as response:
        require(response.status == 200 and json.load(response)['status'] == 'ok', '网关健康检查失败')
    base = 'http://127.0.0.1:51671'
    with urlopen(base + '/', timeout=remaining_time(deadline)) as response:
        require(response.status == 200 and 'text/html' in response.headers.get('Content-Type', ''), '前端入口无效')
        html = response.read(1024 * 1024 + 1)
        require(len(html) <= 1024 * 1024 and b'<html' in html.lower(), '前端入口内容无效')
    index_sha = run('docker', 'exec', frontend, 'sha256sum', '/usr/share/nginx/html/index.html',
                    timeout=remaining_time(deadline)).split()[0]
    require(re.fullmatch(r'[a-f0-9]{64}', index_sha) and hashlib.sha256(html).hexdigest() == index_sha,
            '网关入口与候选镜像不一致')
    files = run('docker', 'exec', frontend, 'find', '/usr/share/nginx/html/assets', '-type', 'f',
                timeout=remaining_time(deadline)).splitlines()
    assets = [name for name in files if name.endswith(('.js', '.mjs', '.css'))]
    require(assets and len(assets) <= 1024, '前端资源清单无效')
    for name in assets:
        relative = name.removeprefix('/usr/share/nginx/html')
        require(re.fullmatch(r'/assets/[A-Za-z0-9_./-]+', relative) and '..' not in relative, '前端资源路径无效')
        with urlopen(base + relative, timeout=remaining_time(deadline)) as response:
            mime = response.headers.get('Content-Type', '').split(';')[0]
            expected = {'text/css'} if name.endswith('.css') else {'application/javascript', 'text/javascript'}
            require(response.status == 200 and mime in expected, '前端资源状态或MIME无效: ' + relative)
    remaining_time(deadline)
    return True


def wait_ready(backend, frontend, timeout=90, *, deadline=None, check_gateway=True):
    deadline = deadline if deadline is not None else time.monotonic() + timeout
    while True:
        try:
            run('docker', 'exec', backend, '/app/.venv/bin/python', '-c',
                "import json,urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8000/api/Health/Ready',timeout=5); assert json.load(r)['status']=='ready'", timeout=remaining_time(deadline, 8))
            if check_gateway:
                return gateway_health(frontend, deadline=deadline)
            remaining_time(deadline)
            return True
        except (ValueError, OSError, TimeoutError, subprocess.TimeoutExpired):
            if time.monotonic() >= deadline:
                raise ValueError('服务恢复就绪超时') from None
            time.sleep(min(1, max(0, deadline - time.monotonic())))


def apply_remote(payload):
    """远端再次验证完整门及布局，同head失败接回原容器；显式迁移交由受限状态机。"""
    import release_deploy
    layout = payload['layout']
    manifest = payload['manifest']
    release_deploy.require_harness_release(Path(payload['gate']), payload['gateSha256'], manifest['source_members'],
        {key: value['digest'] for key, value in manifest['images'].items()})
    require(all(manifest['release_gates'].get(key) is True
                for key in ('full_release_gate', 'correctness', 'restore', 'policy_approved')), '发布门缺失')
    receipt_path = Path(payload['restoreReceipt'])
    require(sha(receipt_path) == manifest['restore_receipt_sha256'], '恢复凭据哈希不匹配')
    recovery = json.loads(receipt_path.read_text())
    require(recovery.get('passed') is True and recovery.get('scope') == 'production_restored'
            and recovery.get('realRetrievalPassed') is True and recovery.get('crossStorageConsistent') is True
            and recovery.get('sourceMembersSha256') == hashlib.sha256(json.dumps(manifest['source_members'],
                ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest(), '恢复证据不完整')
    require(sha(Path(payload['layoutPath'])) == manifest['existing_layout_sha256'], '布局哈希不匹配')
    verify_live(layout)
    release = Path(payload['release'])
    env_file = release / 'backend.env'
    with env_file.open('x', encoding='utf-8') as stream:
        stream.write(filtered_environment((Path(layout['root']) / 'private/backend.env').read_text()))
    env_file.chmod(0o600)
    database = Path(layout['root']) / 'persistent/data/eic.db'
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        current = db.execute('SELECT version_num FROM alembic_version').fetchall()
    if 'schema_transition' in manifest:
        from schema_transition import validate_transition
        from schema_transition_host import apply_transition
        validate_transition(manifest['schema_transition'], manifest)
        require(current == [(manifest['schema_transition']['fromHead'],)], '迁移源head与批准计划不同')
        return apply_transition(layout, manifest, release, env_file)
    require(current == [(manifest['schema_head'],)],
            'schema不同，必须另行审查迁移和数据回滚')
    stamp = release.name
    new_names = ['eic-v4-' + role + '-' + stamp for role in ('backend', 'frontend')]
    stopped, disconnected, created = [], [], []
    result = {'productionSwitchPerformed': False, 'backup': None, 'rollbackPerformed': False}
    try:
        # 先停止旧应用，等待正常退出；旧服务保留，不删卷/镜像。
        for old in ('eic-v4-backend', 'eic-v4-frontend'):
            run('docker', 'stop', '--time', '60', old, timeout=75)
            stopped.append(old)
            state = inspect(old)[0]['State']
            require(not state['Running'] and not state['OOMKilled'] and state['ExitCode'] != 137,
                    '旧服务未正常停止，拒绝继续')
        result['backup'] = snapshot_quiesced(layout, release / 'pre-switch-backup')
        for old in stopped:
            run('docker', 'network', 'disconnect', layout['network'], old)
            disconnected.append(old)
        for role, new_name in zip(('backend', 'frontend'), new_names):
            run(*container_command(role, layout, manifest, stamp, env_file))
            created.append(new_name)
            run('docker', 'start', new_name)
        run('docker', 'exec', new_names[0], '/app/.venv/bin/python', '-m', 'eic.core.schema', 'check', timeout=60)
        run('docker', 'exec', new_names[1], 'nginx', '-t')
        deadline = time.monotonic() + 90
        wait_ready(new_names[0], new_names[1], deadline=deadline, check_gateway=False)
        run('docker', 'exec', 'eic-v4-gateway', 'nginx', '-t', timeout=remaining_time(deadline))
        run('docker', 'exec', 'eic-v4-gateway', 'nginx', '-s', 'reload', timeout=remaining_time(deadline))
        wait_ready(new_names[0], new_names[1], deadline=deadline)
        # 保留稳定服务名，下一次发布仍能盘点当前实例；旧对象按本次stamp保留回滚。
        for role, new_name in zip(('backend', 'frontend'), new_names):
            run('docker', 'rename', 'eic-v4-' + role, 'eic-v4-' + role + '-rollback-' + stamp)
            run('docker', 'rename', new_name, 'eic-v4-' + role)
        result['productionSwitchPerformed'] = True
        result['newContainers'] = ['eic-v4-backend', 'eic-v4-frontend']
        result['rollbackContainers'] = ['eic-v4-' + role + '-rollback-' + stamp for role in ('backend', 'frontend')]
    except BaseException as exc:
        # 若候选已产生写入，不恢复旧数据快照，防止覆盖新记录；同schema回滚代码。
        result['failureType'] = type(exc).__name__
        result.update(rollback_existing(layout, new_names, manifest))
        raise
    finally:
        # 不输出环境、数据库或私密备份内容。
        with (release / 'apply-result.json').open('x') as stream:
            json.dump(result, stream, indent=2)
    return result


def remote_apply(client, helpers, args, manifest):
    """传输到新目录并校验哈希；远端复用同一正式入口的完整门验证。"""
    import secrets
    stamp = manifest['source_archive_sha256'][:12] + '-' + secrets.token_hex(6)
    release = args.remote_dir + '/releases/' + stamp
    prefix = '/tmp/eic-release-' + stamp
    q = shlex.quote
    helpers.run(client, 'umask 077; mkdir -m 700 ' + q(prefix))
    transfers = [(args.source_tar, 'source.tgz'), (args.images_tar, 'images.tar'),
                 (args.layout_json, 'layout.json'), (args.restore_receipt, 'restore.json'),
                 (args.harness_gate, 'gate.json')]
    for source, filename in transfers:
        helpers.sftp_put(client, source, prefix + '/' + filename)
        helpers.run(client, "printf '%s  %s\\n' " + q(sha(source)) + ' ' + q(prefix + '/' + filename) + ' | sha256sum -c -')
    from production_evidence import referenced_paths
    for relative, source in referenced_paths(args.harness_gate).items():
        require(PurePosixPath(relative).parts[0] not in {
            'gate.json', 'layout.json', 'restore.json', 'source.tgz', 'images.tar', 'payload.json'
        }, '证据与发布控制文件重名')
        remote = prefix + '/' + relative
        helpers.run(client, 'mkdir -p ' + q(str(PurePosixPath(remote).parent)))
        helpers.sftp_put(client, source, remote)
        helpers.run(client, "printf '%s  %s\\n' " + q(sha(source)) + ' ' + q(remote) + ' | sha256sum -c -')
    payload = {'manifest': manifest, 'layout': json.loads(args.layout_json.read_text()),
               'layoutPath': prefix + '/layout.json', 'restoreReceipt': prefix + '/restore.json',
               'gate': prefix + '/gate.json', 'gateSha256': args.expected_harness_gate_sha256, 'release': release}
    sftp = client.open_sftp()
    try:
        with sftp.file(prefix + '/payload.json', 'wx') as stream:
            sftp.chmod(prefix + '/payload.json', 0o600)
            stream.write(json.dumps(payload))
    finally:
        sftp.close()
    # 源码归档已在本地逐成员核验且传输哈希一致；远端不接受另一个解包来源。
    command = '\n'.join([
        'set -euo pipefail', 'umask 077', 'test ! -e ' + q(release),
        'mkdir -p ' + q(args.remote_dir + '/releases'), 'mkdir -m 700 ' + q(release),
        'tar -xzf ' + q(prefix + '/source.tgz') + ' -C ' + q(release),
        'docker load -i ' + q(prefix + '/images.tar'),
        'PYTHONPATH=' + q(release + '/scripts') + ' python3 -c ' + q(
            "import json; from existing_host_release import apply_remote; apply_remote(json.load(open(" + repr(prefix + '/payload.json') + ")))"),
    ])
    helpers.run(client, command, timeout=600)
