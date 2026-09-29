"""Offline policy and persistent retry state; no model or UI automation."""
import hashlib
import re

DEFAULT_THREAD = ''
DELAYS = [60, 120, 300, 600, 900, 900]


def new_state():
    return {'attempts': 0, 'due': None, 'due_turn': None, 'pending': None,
            'handled': [], 'progress': '', 'approval': False, 'reason': '尚未开始监测'}


def transient(error):
    error = (error or '').lower()
    if any(s in error for s in ['invalid_encrypted_content', 'insufficient_quota', 'billing',
                                '余额', 'unauthorized', 'authentication', '401', '403', 'budget']):
        return False
    return any(s in error for s in ['rate limit', 'rate_limit', 'ratelimitexceeded', 'too many requests',
        '429', 'overloaded', 'server is busy', 'service unavailable', 'temporarily unavailable',
        '502', '503', '504', 'connection reset', 'connection timed out', 'network timeout',
        'request timeout', '连接超时', '服务繁忙'])


def safe_approval(text):
    """Full-message whitelist, not a keyword classifier. Unknown requests never approved."""
    text = text.strip()
    if len(text) > 160 or any(c in text for c in ['\n', '`', '>', ':', '：', '/', '\\']):
        return None
    pattern = (r'(?:是否|可以)(?:允许)?(?:我)?在(?:当前|本)项目(?:内|中)?'
               r'(运行(?:一次)?(?:定向|单元|离线)测试|执行(?:一次)?只读检查|'
               r'修改(?:当前|本地)源码并运行单元测试)[吗么]?[？?]')
    match = re.fullmatch(pattern, text)
    return match.group(1) if match else None


def fingerprint(text):
    return hashlib.sha256(text.strip().encode()).hexdigest()


def evaluate(state, snap, now, retry_seconds=None):
    """Returns (action, reason); mutates only caller-owned monitor state."""
    if not snap.get('exists', True):
        return 'pause', '所选聊天已不存在；不会匹配同名聊天'
    goal = snap.get('goal')
    if goal is None and state.get('goal_seen'):
        return 'pause', '之前存在的目标已删除，已停止自动续接'
    if goal is not None:
        state['goal_seen'] = True
    if goal in ['complete', 'paused', 'budget_limited', 'usage_limited']:
        return 'pause', {'complete': '目标已完成',
                         'paused': '目标已主动暂停', 'budget_limited': '目标预算耗尽',
                         'usage_limited': '目标使用额度受限'}[goal]
    if snap.get('manual_stop') or snap.get('turn_status') == 'interrupted':
        return 'pause', '检测到用户主动停止；请明确恢复后重新开始监测'
    progress = snap.get('progress')
    advanced = bool(progress and state.get('progress') and progress != state['progress'])
    if advanced:
        state['attempts'] = 0
        state['due'] = None
    if progress:
        state['progress'] = progress
    if state.get('pending'):
        pending = state['pending']
        if advanced:
            pending['work_advanced'] = True
        if pending['marker'] in snap.get('message_text', ''):
            if pending['kind'] == 'retry' and not pending.get('work_advanced'):
                state['attempts'] += 1
            state['handled'] = (state['handled'] + [pending['key']])[-200:]
            state['pending'] = None
            state['due'] = None
            return 'wait', '已确认自动消息到达，等待执行结果'
        # A later real user message supersedes the uncertain request, but is
        # never interpreted as a blanket approval. Account for a possible send.
        if snap.get('last_user_at', 0) > pending['created']:
            if pending['kind'] == 'retry' and not pending.get('work_advanced'):
                state['attempts'] += 1
            state['handled'] = (state['handled'] + [pending['key']])[-200:]
            state['pending'] = None
            state['due'] = None
            return 'wait', '检测到你已在原对话回复，继续监测；下一轮重新核验状态'
        if now - pending['created'] >= 120:
            # User opted into bounded retries after an ambiguous send. Only a
            # fresh, explicit transient failure permits another continuation;
            # uncertain approvals must never be retried this way.
            if (pending['kind'] == 'retry' and not snap.get('running')
                    and not snap.get('question') and snap.get('turn_status') == 'failed'
                    and transient(snap.get('error'))):
                if not pending.get('work_advanced'):
                    state['attempts'] += 1
                state['handled'] = (state['handled'] + ['uncertain:' + pending['marker']])[-200:]
                state['pending'] = None
                if state['attempts'] >= 6:
                    return 'pause', '连续6次自动续接仍失败（含未确认发送），已暂停'
                state['due_turn'] = snap.get('turn_id', '')
                delay = retry_seconds if retry_seconds is not None else DELAYS[state['attempts']]
                state['due'] = max(now, pending['created'] + delay)
                return 'wait', '发送未确认且仍为临时故障，已计入次数；按退避时间继续重试'
            return 'attention', '发送结果尚未确认，仍在监测且不会重复发送；在原对话回复后自动重新核验'
        return 'wait', '正在核对上一条消息是否送达'
    if snap.get('running'):
        return 'wait', '聊天正在执行，保持安静'
    turn = snap.get('turn_id', '')
    if snap.get('turn_status') == 'failed' and transient(snap.get('error')):
        key = 'retry:' + turn
        if key in state['handled']:
            return 'wait', '该失败轮次已发送过续接，等待新的执行结果'
        if state['attempts'] >= 6:
            return 'pause', '连续6次自动续接仍失败，已暂停'
        if state['due_turn'] != turn or state['due'] is None:
            state['due_turn'] = turn
            state['due'] = now + (retry_seconds if retry_seconds is not None else DELAYS[state['attempts']])
        if now < state['due']:
            return 'wait', '临时错误，等待退避时间结束'
        return 'retry', '达到重试时间'
    state['due'] = None
    question = snap.get('question', '')
    if question:
        key = 'approval:' + snap.get('question_id', '')
        if key in state['handled']:
            return 'wait', '该请求已答复'
        scope = safe_approval(question)
        if state['approval'] and scope:
            return 'approve', scope
        return 'attention', '等待你在原对话审核，仍在监测；审核后自动重新核验，无需重新绑定'
    if snap.get('turn_status') == 'failed':
        return 'attention', '当前错误不属于可自动恢复的临时错误'
    return 'wait', '未检测到需要自动续接的临时错误' if goal else '普通聊天监测中，未检测到需要续接的临时错误'
