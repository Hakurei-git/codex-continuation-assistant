import argparse
from contextlib import closing
import ctypes
import datetime
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
import uuid

from bridge import Bridge, Disconnected
from core import DEFAULT_THREAD, new_state, evaluate

DATA = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'CodexContinuationAssistant'
HOME = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex'))


def save(path, data):
    temporary = path.with_suffix('.' + uuid.uuid4().hex + '.tmp')
    with temporary.open('w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, path)


def load():
    DATA.mkdir(parents=True, exist_ok=True)
    path = DATA / 'config.json'
    if path.exists():
        config = json.loads(path.read_text(encoding='utf-8'))
        config.setdefault('targets', [config['selected']] if config.get('selected') else [])
        config.setdefault('interval', 30)
        if 'retry_seconds' not in config:
            config['retry_seconds'] = 30
            for state in config['threads'].values():
                state['due'] = None
        config.setdefault('mode', 'multi' if len(config['targets']) > 1 else 'single')
        config['threads'].pop('', None)
        config['version'] = 3
        return config
    pause()
    return {'version': 3, 'selected': '', 'targets': [], 'mode': 'single', 'interval': 30, 'retry_seconds': 30, 'threads': {}, 'pipe': ''}


def log(message):
    with (DATA / 'actions.log').open('a', encoding='utf-8') as f:
        f.write(datetime.datetime.now().isoformat(timespec='seconds') + ' ' + message + '\n')


def pause():
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / 'paused').touch()


def state_for(config, tid=None):
    return config['threads'].setdefault(tid if tid is not None else config['selected'], new_state())


def goal(tid):
    p = HOME / 'goals_1.sqlite'
    c = sqlite3.connect(p.as_uri() + '?mode=ro', uri=True, timeout=2)
    try:
        row = c.execute('select status from thread_goals where thread_id=?', (tid,)).fetchone()
        return row[0] if row else None
    finally:
        c.close()


def snapshot(bridge, tid):
    bridge.caller = local_caller(exclude=tid)
    c = sqlite3.connect((HOME / 'state_5.sqlite').as_uri() + '?mode=ro', uri=True, timeout=2)
    try:
        row = c.execute('select archived,rollout_path from threads where id=?', (tid,)).fetchone()
    finally:
        c.close()
    if row is None or row[0]:
        return {'exists': False}
    read = bridge.call('read_thread', {'threadId': tid, 'hostId': 'local', 'turnLimit': 10,
                                     'includeOutputs': False, 'maxOutputCharsPerItem': 2000})
    if not read.get('thread'):
        return {'exists': False}
    live = bridge.call('wait_threads', {'targets': [{'threadId': tid, 'hostId': 'local'}], 'timeoutMs': 0})
    if live.get('errors') or not live.get('polls'):
        raise Disconnected('无法核验聊天实时状态')
    poll = live['polls'][0]
    latest = poll.get('latestTurn') or {}
    read_turns = read.get('turns', [])
    latest_read = next((t for t in read_turns if t['id'] == latest.get('id')), None)
    if latest.get('id') and latest_read is None:
        raise Disconnected('聊天状态正在变化，下一轮重新检查')
    progress = ''
    user_messages = []
    last_user_at = 0
    stopped_control = False
    for turn in reversed(read_turns):
        for item in turn.get('items', []):
            if item.get('type') == 'agentMessage' and re.search(r'实体\s*Esc|电脑控制被.*中止|接管因.*停止|停止电脑控制', item.get('text', '')):
                stopped_control = True
            if item.get('type') == 'commandExecution' and item.get('status') == 'completed' and item.get('exitCode') == 0:
                progress = item['id']
            if item.get('type') == 'userMessage':
                user_text = '\n'.join(c.get('text', '') for c in item.get('content', []) if c.get('type') == 'text')
                user_messages.append(user_text)
                if '[自动续接助手:' not in user_text:
                    last_user_at = max(last_user_at, turn.get('startedAt') or 0)
                if '[自动续接助手:' not in user_text and len(user_text) < 200 and re.search(r'恢复接管|正常接管|你接管吧|恢复控制', user_text):
                    stopped_control = False
    question = question_id = ''
    latest_items = (latest_read or {}).get('items', [])
    for item in latest_items:
        if item.get('type') == 'agentMessage' and item.get('phase') in ['final', 'final_answer']:
            text = item.get('text', '').strip()
            if re.search(r'是否|允许|同意|确认|批准', text) and re.search(r'[?？]', text):
                question, question_id = text, item['id']
    last_user = next((m for m in reversed(user_messages) if '[自动续接助手:' not in m), '')
    manual_stop = stopped_control or (len(last_user) < 200 and bool(re.match(r'^(?:请)?(?:暂停|停下|停止|先别|不要继续|别继续)', last_user.strip())))
    status = poll.get('thread', {}).get('status', {})
    kind = status.get('type') if isinstance(status, dict) else status
    return {'exists': True, 'id': tid, 'title': read['thread'].get('title', tid),
            'updated': read['thread'].get('updatedAt', 0),
            'project_path': read['thread'].get('cwd', ''), 'goal': goal(tid),
            'running': kind == 'active' or latest.get('status') == 'inProgress',
            'turn_id': latest.get('id', ''), 'turn_status': latest.get('status'),
            'error': json.dumps(latest.get('error') or {}, ensure_ascii=False),
            'progress': progress, 'manual_stop': manual_stop, 'message_text': '\n'.join(user_messages),
            'last_user_at': last_user_at,
            'question': question, 'question_id': question_id, 'history_path': row[1]}


def history_receipt(path, pending):
    """Read only exact incoming message records; quotes/tool logs never acknowledge."""
    marker = pending['marker']
    token = marker.rsplit(':', 1)[-1].rstrip(']').encode()
    with open(path, 'rb') as history:
        size = os.fstat(history.fileno()).st_size
        offset = pending.get('history_offset', 0)
        history.seek(offset if offset <= size else 0)
        while True:
            position = history.tell()
            line = history.readline()
            if not line or not line.endswith(b'\n'):
                pending['history_offset'] = position
                return False
            if token not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            payload = record.get('payload', {})
            if record.get('type') != 'response_item':
                continue
            if (payload.get('type') == 'function_call_output'
                    and payload.get('name') == 'send_message_to_thread'
                    and payload.get('namespace') == 'codex_app'):
                output = payload.get('output', '')
                if output.startswith('<codex_delegation>') and marker in output:
                    return True
            if payload.get('type') == 'message' and payload.get('role') == 'user':
                if any(marker in c.get('text', '') for c in payload.get('content', [])):
                    return True


def list_chats(bridge):
    data = bridge.call('list_threads', {'limit': 50})
    projects = bridge.call('list_projects', {}).get('projects', [])
    names = {p['projectId']: p['label'] for p in projects}
    rows = []
    for t in data.get('pinnedThreads', []) + data.get('threads', []):
        if t.get('kind') != 'codex' or t.get('hostId', 'local') != 'local':
            continue
        status = t.get('status', '')
        rows.append({'id': t['id'], 'title': t['title'], 'project': names.get(t.get('projectId'), t.get('cwd', '')),
                     'status': status.get('type', '') if isinstance(status, dict) else status,
                     'updated': t.get('updatedAt', 0)})
    by_id = {r['id']: r for r in rows}
    # The live catalog is capped at 50 recents; supplement older local IDs read-only.
    c = sqlite3.connect((HOME / 'state_5.sqlite').as_uri() + '?mode=ro', uri=True, timeout=2)
    try:
        for tid, title, project, cwd, updated in c.execute('select id,title,project_id,cwd,updated_at from threads where archived=0'):
            by_id.setdefault(tid, {'id': tid, 'title': title, 'project': names.get(project, cwd),
                                  'status': 'notLoaded', 'updated': updated})
    finally:
        c.close()
    return sorted(by_id.values(), key=lambda r: r['updated'] / 1000 if r['updated'] > 100000000000 else r['updated'], reverse=True)


def tick(config, bridge, dry=False, tid=None):
    multi = tid is not None
    tid = tid or config['selected']
    state = state_for(config, tid)
    snap = snapshot(bridge, tid)
    if state.get('pending') and snap.get('history_path'):
        try:
            if history_receipt(snap['history_path'], state['pending']):
                snap['message_text'] = snap.get('message_text', '') + state['pending']['marker']
        except OSError:
            pass  # Keep pending; a missing/unreadable history is not non-delivery.
    if dry or (DATA / 'paused').exists() or state.get('stopped'):
        return {'reason': state.get('reason') if state.get('stopped') else '监测已暂停（只读状态已更新）', 'snapshot': public(snap), 'state': state}
    action, reason = evaluate(state, snap, time.time(), config.get('retry_seconds', 30))
    state['last_check'] = time.time()
    if action in ['retry', 'approve']:
        # Revalidate both control state and live conversation immediately before dispatch.
        second = snapshot(bridge, tid)
        current = load()
        if (DATA / 'paused').exists() or (tid not in current['targets'] if multi else current['selected'] != tid):
            return {'reason': '发送前已取消', 'state': state}
        action2, reason2 = evaluate(state, second, time.time(), config.get('retry_seconds', 30))
        if action2 != action or second.get('turn_id') != snap.get('turn_id') or second.get('question_id') != snap.get('question_id'):
            action, reason = 'wait', '发送前状态已变化，已取消本次发送'
        else:
            marker = '[自动续接助手:' + uuid.uuid4().hex + ']'
            key = ('retry:' + snap['turn_id']) if action == 'retry' else ('approval:' + snap['question_id'])
            if action == 'retry':
                task = '继续完成当前目标。' if snap.get('goal') else '继续当前对话中尚未完成的工作。'
                message = task + '先核查当前进程、已有回执和实际进度；保留最新用户授权与安全边界，不重放一次性操作。若用户已暂停、工作已完成或需要新授权，不要据此继续操作。'
            else:
                message = ('同意本次在当前项目范围内' + reason2 + '。仅限可回滚的工作区操作；不得修改正式存档、删除数据、付费、扩大权限、关闭安全保护、突破一次性次数或恢复用户主动停止的接管。若实际操作超出此范围，请停止并向用户确认。')
            state['pending'] = {'marker': marker, 'created': time.time(), 'kind': 'retry' if action == 'retry' else 'approval', 'key': key}
            state['reason'] = '准备发送，记录已持久化'
            save(DATA / 'config.json', config)
            log('dispatch ' + action + ' thread=' + tid + ' key=' + key + ' marker=' + marker)
            try:
                receipt = bridge.call('send_message_to_thread', {'threadId': tid, 'hostId': 'local', 'prompt': message + '\n' + marker})
                # Keep structural delivery diagnostics, never prompt or error bodies.
                if isinstance(receipt, dict):
                    state['pending']['receipt_keys'] = list(receipt.keys())
                reason = '消息已提交，等待下一轮确认到达'
            except Exception as exc:
                log('delivery_uncertain thread=' + tid + ' error_type=' + type(exc).__name__)
                reason = '发送结果不明确，先核对记录；不会立即补发'
    if action == 'pause':
        state['stopped'] = True
        if not multi:
            pause()
    if state.get('reason') != reason:
        log('state thread=' + tid + ' ' + reason)
    state['reason'] = reason
    save(DATA / 'config.json', config)
    return {'reason': reason, 'snapshot': public(snap), 'state': state,
            'notify': action in ['pause', 'attention'], 'paused': (DATA / 'paused').exists()}


def public(snap):
    return {k: v for k, v in snap.items() if k not in ['message_text', 'question', 'error', 'history_path']}


def local_caller(exclude=''):
    try:
        with closing(sqlite3.connect((HOME / 'state_5.sqlite').as_uri() + '?mode=ro', uri=True)) as catalog:
            caller = catalog.execute('select id from threads where archived=0 and id != ? order by updated_at desc limit 1', (exclude,)).fetchone()
        if not caller:
            raise Disconnected('本地接口需要另一个聊天作为调用上下文，请在 Codex 额外创建一个普通聊天')
        return caller[0]
    except sqlite3.Error as exc:
        raise Disconnected('本机 Codex 数据结构不兼容或尚未初始化，请打开 Codex 并创建聊天') from exc


def command(args):
    config = load()
    if args.action == 'pause':
        pause()
        return {'reason': '已暂停，取消尚未提交的消息；已提交的消息不能撤回'}
    bridge = Bridge(config.get('pipe', ''))
    # Bootstrap from this user's local catalog, never a distributor's chat ID.
    bridge.caller = local_caller()
    bridge.connect()
    config['pipe'] = bridge.pipe
    if args.action == 'configure':
        value = json.loads(args.value)
        ids = list(dict.fromkeys(value['ids']))
        interval = value['interval']
        retry_seconds = value.get('retry_seconds', config['retry_seconds'])
        if not isinstance(retry_seconds, int) or isinstance(retry_seconds, bool) or not 5 <= retry_seconds <= 86400:
            raise ValueError('重试等待必须为5至86400秒的整数')
        mode = value.get('mode', 'multi' if len(ids) > 1 else 'single')
        if mode not in ['single', 'multi'] or (mode == 'single' and len(ids) > 1):
            raise ValueError('单对话模式只能选择一个聊天')
        if not isinstance(interval, int) or isinstance(interval, bool) or not 10 <= interval <= 86400:
            raise ValueError('检查间隔必须为10至86400秒的整数')
        for tid in ids:
            uuid.UUID(tid)
            data = bridge.call('read_thread', {'threadId': tid, 'hostId': 'local', 'turnLimit': 1, 'includeOutputs': False})
            if data.get('thread', {}).get('kind') != 'codex' or data['thread'].get('hostId', 'local') != 'local':
                raise ValueError('请选择本机 Codex 聊天')
        pause()
        for state in config['threads'].values():
            state['due'] = None
        config.update(targets=ids, selected=ids[0] if ids else '', interval=interval, retry_seconds=retry_seconds, mode=mode)
        save(DATA / 'config.json', config)
        return {'reason': '配置已保存；点击开始监测后生效', 'targets': ids, 'interval': interval}
    if args.action == 'list':
        rows = list_chats(bridge)
        save(DATA / 'config.json', config)
        return {'rows': rows, 'selected': config['selected'], 'targets': config['targets'], 'mode': config['mode'], 'interval': config['interval'], 'retry_seconds': config['retry_seconds'], 'states': config['threads'], 'state': state_for(config) if config['selected'] else None, 'data_dir': str(DATA)}
    if args.action == 'bind':
        uuid.UUID(args.value)
        data = bridge.call('read_thread', {'threadId': args.value, 'hostId': 'local', 'turnLimit': 1, 'includeOutputs': False})
        if data.get('thread', {}).get('kind') != 'codex':
            raise ValueError('请选择本机 Codex 聊天')
        pause()
        state_for(config)['due'] = None
        config['selected'] = args.value
        config['targets'] = [args.value]
        state = state_for(config)
        state['due'] = None
        save(DATA / 'config.json', config)
        return {'selected': args.value, 'title': data['thread']['title'], 'state': state, 'reason': '聊天已核验并绑定；请点击开始监测'}
    if args.action == 'approval':
        value = json.loads(args.value)
        tid = value['id'] if isinstance(value, dict) else config['selected']
        if tid not in config['targets']:
            raise ValueError('请先将此聊天加入监测配置')
        state_for(config, tid)['approval'] = value['enabled'] if isinstance(value, dict) else bool(value)
        save(DATA / 'config.json', config)
        return {'reason': '仅修改了所选聊天的限定授权设置', 'state': state_for(config, tid)}
    if args.action == 'start':
        if not config['targets']:
            raise ValueError('请先勾选聊天并保存配置')
        for tid in config['targets']:
            state = state_for(config, tid)
            state['stopped'] = False
            if state['attempts'] >= 6:
                state.update(attempts=0, due=None)
        (DATA / 'paused').unlink(missing_ok=True)
        save(DATA / 'config.json', config)
        log('user started targets=' + str(len(config['targets'])))
        return {'reason': f"已开始监测{len(config['targets'])}个聊天，检查间隔{config['interval']}秒", 'paused': False}
    results = []
    for tid in config['targets']:
        try:
            result = tick(config, bridge, dry=args.action == 'observe', tid=tid)
            results.append(dict(result, id=tid))
        except Exception as exc:
            reason = '连接或状态读取失败，等待下次检查'
            if isinstance(exc, Disconnected) and str(exc).startswith('本地接口需要另一个聊天'):
                reason = str(exc)
            results.append({'id': tid, 'reason': reason, 'error_type': type(exc).__name__})
    return {'results': results, 'paused': (DATA / 'paused').exists(), 'interval': config['interval']}


def main():
    global DATA
    parser = argparse.ArgumentParser()
    parser.add_argument('--action', choices=['list', 'bind', 'configure', 'start', 'pause', 'approval', 'tick', 'observe'])
    parser.add_argument('--value', default='')
    parser.add_argument('--value-file')
    parser.add_argument('--output-file')
    parser.add_argument('--data-dir', help='Shared absolute state directory for GUI workers')
    parser.add_argument('--smoke', default='')
    parser.add_argument('--start-monitor', action='store_true', help='Explicitly start monitoring after the window loads')
    args = parser.parse_args()
    if args.value_file:
        args.value = Path(args.value_file).read_text(encoding='utf-8-sig')
    if args.data_dir:
        DATA = Path(args.data_dir).resolve()
    if not args.action:
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        k.CreateMutexW.restype = ctypes.c_void_p
        handle = k.CreateMutexW(None, False, 'Local\\CodexContinuationAssistant')
        if not handle or ctypes.get_last_error() == 183:
            ctypes.windll.user32.MessageBoxW(None, '自动续接助手已经在运行。', 'Codex 自动续接', 64)
            return 0
        pause()
        directory = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
        cmd = [str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'),
               '-NoProfile', '-ExecutionPolicy', 'Bypass', '-STA', '-WindowStyle', 'Hidden',
               '-File', str(directory / 'Window.ps1'), '-Backend', sys.executable, '-DataDir', str(DATA)]
        if not getattr(sys, 'frozen', False):
            cmd += ['-BackendScript', str(Path(__file__).resolve())]
        if args.smoke:
            cmd += ['-SmokeOutput', args.smoke]
        elif args.start_monitor:
            cmd += ['-StartMonitoring']
        try:
            return subprocess.call(cmd, creationflags=subprocess.CREATE_NO_WINDOW)
        finally:
            pause()
            k.CloseHandle.argtypes = [ctypes.c_void_p]
            k.CloseHandle(handle)
    try:
        # Serialize workers without taking locks on Codex databases or histories.
        import msvcrt
        DATA.mkdir(parents=True, exist_ok=True)
        with (DATA / 'worker.lock').open('a+b') as lock:
            if lock.tell() == 0:
                lock.write(b'0')
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            result = {'ok': True, 'data': command(args)}
        code = 0
    except Exception as exc:
        result = {'ok': False, 'error': str(exc), 'disconnected': isinstance(exc, (Disconnected, OSError))}
        code = 1
    text = json.dumps(result, ensure_ascii=False)
    if args.output_file:
        Path(args.output_file).write_text(text, encoding='utf-8')
    elif sys.stdout:
        sys.stdout.reconfigure(encoding='utf-8')
        print(text)
    return code


if __name__ == '__main__':
    sys.exit(main())
