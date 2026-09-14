#!/usr/bin/env python3
"""Local SQLite records and scoped handoff packets for navigator."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import time
import uuid

PROTOCOL = 'navigator/v1'
SCHEMA = '''
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE tasks(id TEXT PRIMARY KEY, blueprint TEXT NOT NULL, revision INTEGER NOT NULL,
                   state TEXT NOT NULL, paused_at REAL);
CREATE TABLE loops(task TEXT NOT NULL REFERENCES tasks(id), id TEXT NOT NULL, spec TEXT NOT NULL,
                   version INTEGER NOT NULL, status TEXT NOT NULL, active_run TEXT, result TEXT,
                   retired INTEGER NOT NULL DEFAULT 0,
                   PRIMARY KEY(task,id));
CREATE TABLE runs(id TEXT PRIMARY KEY, task TEXT NOT NULL, loop TEXT NOT NULL, request_id TEXT NOT NULL,
                  snapshot TEXT NOT NULL, status TEXT NOT NULL, worker_id TEXT, host_id TEXT, workdir TEXT,
                  worker_token TEXT NOT NULL, review_token TEXT NOT NULL, result TEXT, verdict TEXT,
                  reconciled_at REAL, FOREIGN KEY(task,loop) REFERENCES loops(task,id), UNIQUE(task,request_id));
CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT, task TEXT NOT NULL REFERENCES tasks(id),
                    at REAL NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL);
CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'history is append-only'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'history is append-only'); END;
'''
MAIN = {'create', 'blueprint', 'plan', 'status', 'history', 'ready', 'claim', 'bind',
        'change', 'reconcile', 'pause', 'resume', 'finish'}
WORKER = {'context', 'submit', 'block', 'review-packet'}
VERIFIER = {'context', 'verify'}
CONDITIONS = {'source', 'source_version', 'environment', 'commands'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def required_text(data, key):
    value = data.get(key)
    require(isinstance(value, str) and bool(value.strip()), 'missing or invalid ' + key)
    return value


def criteria(data):
    value = data.get('acceptance')
    require(isinstance(value, list) and value and all(isinstance(x, str) and x.strip() for x in value),
            'acceptance must contain nonempty criteria')
    required_text(data, 'goal')


def artifact(raw):
    require(isinstance(raw, str) and Path(raw).is_absolute(), 'artifact path must be absolute')
    path = Path(raw).resolve()
    require(path.is_file(), 'artifact does not exist: ' + str(path))
    value = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            value.update(block)
    return {'path': str(path), 'sha256': value.hexdigest()}


def unchanged(result):
    files = result['artifacts'][:]
    if result.get('verification'):
        files.append(result['verification']['report'])
    try:
        return all(artifact(item['path']) == item for item in files)
    except (ValueError, OSError):
        return False


def connect(path):
    connection = sqlite3.connect(str(path), timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    return connection


def initialize(root):
    require(root and Path(root).is_absolute(), 'init requires an absolute --root')
    folder = Path(root).resolve() / '.navigator'
    folder.mkdir(parents=True, exist_ok=True)
    db, access_path = folder / 'state.sqlite', folder / 'main.json'
    if db.exists() or access_path.exists():
        require(db.is_file() and access_path.is_file(), 'incomplete store; reconcile files before initializing')
        access = json.loads(access_path.read_text())
        with connect(db) as connection:
            expected = connection.execute("SELECT value FROM meta WHERE key='main_token'").fetchone()
            require(expected and expected[0] == digest(access['token']) and access['db'] == str(db),
                    'existing store and access file do not match')
        return {'db': str(db), 'access': str(access_path)}
    token = secrets.token_urlsafe(32)
    # Reserve the file before creating the schema, rather than overwriting an existing store.
    descriptor = os.open(str(db), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    with connect(db) as connection:
        connection.executescript(SCHEMA)
        connection.executemany('INSERT INTO meta VALUES (?,?)', [('schema', '1'), ('main_token', digest(token))])
    access = {'protocol': PROTOCOL, 'role': 'main', 'db': str(db), 'token': token}
    with os.fdopen(os.open(str(access_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'w') as handle:
        handle.write(encode(access) + '\n')
    return {'db': str(db), 'access': str(access_path)}


class Store:
    def __init__(self, access):
        require(access.get('protocol') == PROTOCOL, 'unsupported access protocol')
        require(Path(access['db']).is_absolute() and Path(access['db']).is_file(), 'database not found')
        self.access = access
        self.db = connect(access['db'])
        version = self.db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        require(version and version[0] == '1', 'unsupported database schema')
        self.role = access.get('role')
        if self.role == 'main':
            expected = self.db.execute("SELECT value FROM meta WHERE key='main_token'").fetchone()[0]
        else:
            require(self.role in ('worker', 'verifier'), 'unknown role')
            row = self.row('SELECT * FROM runs WHERE id=?', (access.get('run'),))
            require(row['task'] == access.get('task') and row['loop'] == access.get('loop'), 'wrong run scope')
            field = 'worker_token' if self.role == 'worker' else 'review_token'
            expected = digest(row[field])
        require(secrets.compare_digest(expected, digest(access.get('token'))), 'invalid access token')

    def row(self, sql, values=()):
        result = self.db.execute(sql, values).fetchone()
        require(result is not None, 'record not found')
        return result

    def task(self, task):
        return self.row('SELECT * FROM tasks WHERE id=?', (task,))

    def loop(self, task, loop):
        return self.row('SELECT * FROM loops WHERE task=? AND id=?', (task, loop))

    def event(self, task, kind, data):
        self.db.execute('INSERT INTO events(task,at,kind,data) VALUES (?,?,?,?)',
                        (task, time.time(), kind, encode(data)))

    def loops(self, task):
        return self.db.execute('SELECT * FROM loops WHERE task=? AND retired=0 ORDER BY id', (task,)).fetchall()

    def invalidate(self, task, roots, reason):
        rows = self.loops(task)
        affected = set(roots)
        while True:
            extra = {r['id'] for r in rows if set(json.loads(r['spec'])['deps']) & affected}
            if extra <= affected:
                break
            affected |= extra
        for row in rows:
            if row['id'] not in affected:
                continue
            self.db.execute('UPDATE loops SET version=version+1,status=?,result=NULL WHERE task=? AND id=?',
                            ('blocked' if row['active_run'] else 'pending', task, row['id']))
            if row['active_run']:
                self.db.execute("UPDATE runs SET status='stale' WHERE id=?", (row['active_run'],))
        if affected:
            self.event(task, 'invalidate', {'loops': sorted(affected), 'reason': reason})
            self.db.execute("UPDATE tasks SET state='paused',paused_at=? WHERE id=? AND state='complete'",
                            (time.time(), task))
        return sorted(affected)

    def scan(self, task):
        roots = [r['id'] for r in self.loops(task)
                 if r['status'] == 'passed' and not unchanged(json.loads(r['result']))]
        return self.invalidate(task, roots, 'artifact or verification report changed') if roots else []

    def snapshot(self, task, loop):
        row = self.loop(task, loop)
        dependencies = {}
        for dep in json.loads(row['spec'])['deps']:
            source = self.loop(task, dep)
            require(source['status'] == 'passed', 'dependency is not passed: ' + dep)
            dependencies[dep] = digest(json.loads(source['result']))
        return {'blueprint': digest(json.loads(self.task(task)['blueprint'])),
                'version': row['version'], 'dependencies': dependencies}

    def current_run(self):
        run = self.row('SELECT * FROM runs WHERE id=?', (self.access['run'],))
        row = self.loop(run['task'], run['loop'])
        require(self.task(run['task'])['state'] == 'active', 'task is not active')
        require(row['active_run'] == run['id'], 'attempt is no longer active')
        require(run['status'] in ('reserved', 'running', 'review'), 'attempt needs reconciliation')
        require(json.loads(run['snapshot']) == self.snapshot(run['task'], run['loop']), 'stale attempt')
        return run

    def context(self, run, role):
        task, loop = run['task'], run['loop']
        row = self.loop(task, loop)
        result = {'blueprint': json.loads(self.task(task)['blueprint']), 'loop': json.loads(row['spec']),
                  'run': {'id': run['id'], 'status': run['status'], 'worker_id': run['worker_id']}}
        if role == 'worker':
            result['dependencies'] = {name: json.loads(self.loop(task, name)['result'])
                                      for name in result['loop']['deps']}
        else:
            require(run['result'], 'nothing submitted for review')
            submitted = json.loads(run['result'])
            result['artifacts'] = submitted['artifacts']
            result['conditions'] = {k: v for k, v in submitted['conditions'].items() if k in CONDITIONS}
        return result

    def packet(self, run, role):
        return {'protocol': PROTOCOL, 'db': self.access['db'], 'role': role,
                'token': run['worker_token' if role == 'worker' else 'review_token'],
                'task': run['task'], 'loop': run['loop'], 'run': run['id'],
                'script': str(Path(__file__).resolve()), 'context': self.context(run, role)}

    def handoff(self, run, role):
        packet = self.packet(run, role)
        instruction = ('完成这个循环, 只修改 scope 中的内容, 通过 context 读取当前要求, '
                       '用 submit 提交产物, 再用 review-packet 为新上下文的独立 subagent 准备验证包, '
                       '不要修改总体计划, 最后返回脚本给出的回执'
                       if role == 'worker' else
                       '独立核对原定标准和产物, 不修改实施产物, 保存检查报告并用 verify 提交, '
                       '不再派发验证者, 最后返回脚本给出的回执')
        reference = Path(__file__).resolve().parents[1] / 'references' / 'commands.md'
        access_name = 'access-' + run['id'] + '-' + role + '.json'
        prompt = ('Navigator ' + run['id'] + '\n' + instruction + '\n'
                  '将下面 JSON 保存为当前任务目录中的 ' + access_name + ', '
                  '用 JSON 中 script 指向的脚本和 --access 参数调用命令, 操作说明见 '
                  + str(reference) + ', 拿不到数据库或工具时说明阻塞, 不复制数据库另写\n'
                  + json.dumps(packet, ensure_ascii=False, indent=2))
        return {'packet': packet, 'prompt': prompt}

    def ready(self, task):
        if self.task(task)['state'] != 'active':
            return []
        rows = self.loops(task)
        passed = {r['id'] for r in rows if r['status'] == 'passed'}
        return sorted([json.loads(r['spec']) for r in rows
                       if not r['active_run'] and r['status'] != 'passed'
                       and set(json.loads(r['spec'])['deps']) <= passed], key=lambda x: (x['phase'], x['id']))

    def execute(self, action, data):
        allowed = MAIN if self.role == 'main' else WORKER if self.role == 'worker' else VERIFIER
        require(action in allowed, 'operation not allowed for ' + self.role)
        if action == 'create':
            blueprint = data.get('blueprint')
            require(isinstance(blueprint, dict), 'blueprint must be an object')
            criteria(blueprint)
            task = uuid.uuid4().hex
            self.db.execute('INSERT INTO tasks VALUES (?,?,1,?,NULL)', (task, encode(blueprint), 'active'))
            self.event(task, action, {'blueprint': blueprint})
            return {'task': task, 'revision': 1}
        task = required_text(data, 'task') if self.role == 'main' else self.access['task']
        self.task(task)
        self.scan(task)
        if self.role != 'main':
            return self.execute_actor(action, data)
        if action == 'status':
            state = dict(self.task(task))
            state['blueprint'] = json.loads(state['blueprint'])
            state['task'] = state.pop('id')
            state['loops'] = [{**dict(r), 'spec': json.loads(r['spec']),
                               'result': json.loads(r['result']) if r['result'] else None} for r in self.loops(task)]
            state['runs'] = [{k: r[k] for k in ('id', 'loop', 'status', 'worker_id', 'host_id', 'workdir', 'reconciled_at')}
                             for r in self.db.execute('SELECT * FROM runs WHERE task=? ORDER BY rowid', (task,))]
            return state
        if action == 'history':
            return {'events': [{**dict(r), 'data': json.loads(r['data'])}
                               for r in self.db.execute('SELECT * FROM events WHERE task=? ORDER BY seq', (task,))]}
        if action == 'ready':
            return {'loops': self.ready(task)}
        if action in ('plan', 'blueprint'):
            current = self.task(task)
            require(type(data.get('revision')) is int and data['revision'] == current['revision'], 'stale revision')
            if action == 'blueprint':
                value = data.get('blueprint')
                require(isinstance(value, dict), 'blueprint must be an object')
                criteria(value)
                reason = required_text(data, 'reason')
                old = json.loads(current['blueprint'])
                self.db.execute('UPDATE tasks SET blueprint=?,revision=revision+1 WHERE id=?', (encode(value), task))
                self.invalidate(task, [r['id'] for r in self.loops(task)], reason)
                self.event(task, action, {'before': old, 'after': value, 'reason': reason})
            else:
                self.save_plan(task, data['loops'])
                self.db.execute('UPDATE tasks SET revision=revision+1 WHERE id=?', (task,))
            return {'task': task, 'revision': current['revision'] + 1}
        if action == 'claim':
            loop, request_id = required_text(data, 'loop'), required_text(data, 'request_id')
            old = self.db.execute('SELECT * FROM runs WHERE task=? AND request_id=?', (task, request_id)).fetchone()
            if old:
                require(old['loop'] == loop, 'request_id already used for a different loop')
                return {'run': old['id'], 'new': False, 'status': old['status'], **self.handoff(old, 'worker')}
            require(loop in {x['id'] for x in self.ready(task)}, 'loop is not ready or already reserved')
            run = uuid.uuid4().hex
            self.db.execute('''INSERT INTO runs(id,task,loop,request_id,snapshot,status,worker_token,review_token)
                               VALUES (?,?,?,?,?,?,?,?)''',
                            (run, task, loop, request_id, encode(self.snapshot(task, loop)), 'reserved',
                             secrets.token_urlsafe(24), secrets.token_urlsafe(24)))
            self.db.execute("UPDATE loops SET status='running',active_run=? WHERE task=? AND id=?", (run, task, loop))
            self.event(task, action, {'loop': loop, 'run': run, 'request_id': request_id})
            return {'run': run, 'new': True, **self.handoff(self.row('SELECT * FROM runs WHERE id=?', (run,)), 'worker')}
        if action == 'bind':
            run = self.scoped_run(task, data)
            worker, host, directory = [required_text(data, x) for x in ('worker_id', 'host_id', 'workdir')]
            require(Path(directory).is_absolute(), 'workdir must be absolute')
            if run['worker_id']:
                require((worker, host, directory) == (run['worker_id'], run['host_id'], run['workdir']), 'already bound')
            else:
                require(run['status'] == 'reserved', 'attempt cannot be bound')
                self.db.execute("UPDATE runs SET worker_id=?,host_id=?,workdir=?,status='running' WHERE id=?",
                                (worker, host, directory, run['id']))
                self.event(task, action, {'run': run['id'], 'worker_id': worker, 'host_id': host, 'workdir': directory})
            return {'run': run['id'], 'worker_id': worker}
        if action == 'change':
            loop, reason = required_text(data, 'loop'), required_text(data, 'reason')
            self.loop(task, loop)
            return {'affected': self.invalidate(task, [loop], reason)}
        if action == 'reconcile':
            run = self.scoped_run(task, data)
            note = required_text(data, 'note')
            require(type(data.get('stopped')) is bool, 'stopped must be boolean')
            self.db.execute('UPDATE runs SET reconciled_at=? WHERE id=?', (time.time(), run['id']))
            if data['stopped'] and run['status'] != 'passed':
                self.db.execute("UPDATE runs SET status='cancelled' WHERE id=?", (run['id'],))
                self.db.execute("UPDATE loops SET active_run=NULL,status='pending' WHERE task=? AND id=? AND active_run=?",
                                (task, run['loop'], run['id']))
            self.event(task, action, {'run': run['id'], 'stopped': data['stopped'], 'note': note})
            return {'run': run['id'], 'stopped': data['stopped']}
        if action in ('pause', 'resume'):
            note = required_text(data, 'note')
            state = self.task(task)
            require(state['state'] != 'complete', 'completed task cannot be resumed without a change')
            if action == 'resume':
                require(state['state'] == 'paused', 'task is not paused')
                active = self.db.execute('''SELECT r.* FROM runs r JOIN loops l ON l.active_run=r.id
                                            WHERE r.task=?''', (task,)).fetchall()
                require(all(r['worker_id'] and r['reconciled_at'] and r['reconciled_at'] >= state['paused_at'] for r in active),
                        'reconcile every active attempt before resuming')
            self.db.execute('UPDATE tasks SET state=?,paused_at=? WHERE id=?',
                            ('paused' if action == 'pause' else 'active', time.time() if action == 'pause' else None, task))
            self.event(task, action, {'note': note})
            return {'state': 'paused' if action == 'pause' else 'active'}
        if action == 'finish':
            rows = self.loops(task)
            require(self.task(task)['state'] == 'active' and rows and all(r['status'] == 'passed' for r in rows),
                    'all loops must pass before finishing')
            require(any(json.loads(r['spec'])['kind'] == 'acceptance' for r in rows), 'final acceptance loop is required')
            self.db.execute("UPDATE tasks SET state='complete' WHERE id=?", (task,))
            self.event(task, action, {})
            return {'state': 'complete'}
        raise ValueError('unknown operation')

    def scoped_run(self, task, data):
        run = self.row('SELECT * FROM runs WHERE id=?', (required_text(data, 'run'),))
        require(run['task'] == task, 'wrong task')
        return run

    def save_plan(self, task, specs):
        require(isinstance(specs, list) and specs, 'loops must be a nonempty list')
        require(not any(r['active_run'] for r in self.loops(task)), 'reconcile active attempts before replacing plan')
        indexed = {}
        for raw in specs:
            require(isinstance(raw, dict), 'loop must be an object')
            spec = dict(raw)
            name = required_text(spec, 'id')
            require(name not in indexed, 'duplicate loop id')
            required_text(spec, 'title')
            criteria(spec)
            require(type(spec.get('phase')) is int and spec['phase'] > 0, 'phase must be a positive integer')
            require(spec.get('kind') in ('work', 'check', 'acceptance'), 'invalid loop kind')
            for field in ('deps', 'scope'):
                require(isinstance(spec.get(field), list) and all(isinstance(x, str) and x.strip() for x in spec[field]),
                        field + ' must be a string list')
            require(len(set(spec['deps'])) == len(spec['deps']), 'duplicate dependencies')
            indexed[name] = spec
        visited, visiting, covered = set(), set(), {}
        def visit(name):
            require(name in indexed, 'unknown dependency: ' + name)
            require(name not in visiting, 'dependency cycle')
            if name in visited:
                return
            visiting.add(name)
            covered[name] = set()
            for dep in indexed[name]['deps']:
                visit(dep)
                require(indexed[dep]['phase'] <= indexed[name]['phase'], 'dependency belongs to a later phase')
                covered[name] |= {dep} | covered[dep]
            visiting.remove(name)
            visited.add(name)
        for name in indexed:
            visit(name)
        final = [name for name in indexed if indexed[name]['kind'] == 'acceptance']
        require(not final or any(len(covered[name]) == len(indexed) - 1 for name in final),
                'final acceptance must depend directly or indirectly on every other loop')
        old = {r['id']: r for r in self.db.execute('SELECT * FROM loops WHERE task=?', (task,))}
        previous = {name: json.loads(row['spec']) for name, row in old.items() if not row['retired']}
        # Keep retired definitions addressable by historical runs, but not in the current plan.
        changed = [name for name in old if name in indexed and
                   (old[name]['retired'] or json.loads(old[name]['spec']) != indexed[name])]
        for name in set(old) - set(indexed):
            self.db.execute('UPDATE loops SET retired=1 WHERE task=? AND id=?', (task, name))
        for name, spec in indexed.items():
            if name in old:
                self.db.execute('UPDATE loops SET spec=?,retired=0 WHERE task=? AND id=?', (encode(spec), task, name))
            else:
                self.db.execute('INSERT INTO loops(task,id,spec,version,status) VALUES (?,?,?,1,?)',
                                (task, name, encode(spec), 'pending'))
        self.invalidate(task, changed, 'loop definition changed')
        if previous != indexed:
            self.db.execute("UPDATE tasks SET state='paused',paused_at=? WHERE id=? AND state='complete'",
                            (time.time(), task))
        self.event(task, 'plan', {'loops': list(indexed.values())})

    def execute_actor(self, action, data):
        run = self.current_run()
        task, loop, run_id = run['task'], run['loop'], run['id']
        if action == 'context':
            return self.context(run, self.role)
        if action == 'submit':
            require(run['worker_id'], 'main agent must bind the created task first')
            summary = required_text(data, 'summary')
            paths = data.get('artifacts')
            require(isinstance(paths, list) and paths, 'submit at least one local artifact')
            conditions = data.get('conditions', {})
            require(isinstance(conditions, dict) and set(conditions) <= CONDITIONS,
                    'conditions only accepts source, source_version, environment and commands')
            require(all(isinstance(v, list) and all(isinstance(x, str) for x in v) if k == 'commands'
                        else isinstance(v, str) for k, v in conditions.items()), 'invalid condition value')
            result = {'summary': summary, 'artifacts': [artifact(x) for x in paths], 'conditions': conditions}
            if run['result']:
                require(result == json.loads(run['result']), 'result changed; ask main agent to open a new attempt')
            else:
                require(run['status'] == 'running', 'attempt is not running')
                self.db.execute("UPDATE runs SET result=?,status='review' WHERE id=?", (encode(result), run_id))
                self.db.execute("UPDATE loops SET status='review' WHERE task=? AND id=?", (task, loop))
                self.event(task, action, {'run': run_id, 'result': result})
            return {'run': run_id, 'state': 'review', 'digest': digest(result)}
        if action == 'block':
            reason = required_text(data, 'reason')
            self.db.execute("UPDATE runs SET status='stale' WHERE id=?", (run_id,))
            self.db.execute("UPDATE loops SET status='blocked' WHERE task=? AND id=?", (task, loop))
            self.event(task, action, {'run': run_id, 'reason': reason})
            return {'run': run_id, 'state': 'blocked'}
        require(run['status'] == 'review' and run['result'], 'submit a result before requesting verification')
        result = json.loads(run['result'])
        require(unchanged(result), 'submitted artifact changed; main agent must reconcile')
        if action == 'review-packet':
            return self.handoff(run, 'verifier')
        require(type(data.get('passed')) is bool, 'passed must be boolean')
        reviewer = required_text(data, 'reviewer_id')
        require(reviewer != run['worker_id'], 'reviewer must be independent from the worker')
        verdict = {'passed': data['passed'], 'reviewer_id': reviewer, 'report': artifact(data.get('report'))}
        self.db.execute('UPDATE runs SET verdict=?,status=? WHERE id=?',
                        (encode(verdict), 'passed' if verdict['passed'] else 'failed', run_id))
        if verdict['passed']:
            result['verification'] = verdict
            self.db.execute("UPDATE loops SET result=?,status='passed',active_run=NULL WHERE task=? AND id=?",
                            (encode(result), task, loop))
        else:
            self.db.execute("UPDATE loops SET status='blocked' WHERE task=? AND id=?", (task, loop))
        self.event(task, 'verify', {'run': run_id, 'verdict': verdict})
        return {'task': task, 'loop': loop, 'run': run_id, 'state': 'passed' if verdict['passed'] else 'blocked',
                'report': verdict['report']}


class JsonParser(argparse.ArgumentParser):
    def error(self, message):
        print(json.dumps({'ok': False, 'error': message}))
        self.exit(2)


def main():
    parser = JsonParser(description=__doc__)
    parser.add_argument('operation', choices=sorted(MAIN | WORKER | VERIFIER | {'init'}))
    parser.add_argument('--root')
    parser.add_argument('--access')
    parser.add_argument('--data', help='request JSON filename, or - for stdin')
    args = parser.parse_args()
    store = None
    try:
        if args.operation == 'init':
            result = initialize(args.root)
        else:
            require(args.access, '--access is required')
            access = json.loads(Path(args.access).read_text())
            data = json.loads(sys.stdin.read() if args.data == '-' else Path(args.data).read_text()) if args.data else {}
            require(isinstance(data, dict), 'request must be a JSON object')
            store = Store(access)
            store.db.execute('BEGIN IMMEDIATE')
            result = store.execute(args.operation, data)
            store.db.commit()
        print(json.dumps({'ok': True, **result}, ensure_ascii=False))
        return 0
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as error:
        if store:
            store.db.rollback()
        print(json.dumps({'ok': False, 'error': str(error)}, ensure_ascii=False))
        return 1
    finally:
        if store:
            store.db.close()


if __name__ == '__main__':
    sys.exit(main())
