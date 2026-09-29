import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import app
from core import new_state, evaluate, transient, safe_approval, DELAYS


def snapshot(**kwargs):
    base = {'exists': True, 'goal': 'active', 'running': False, 'turn_status': 'failed',
            'turn_id': 't1', 'error': 'rate limit exceeded', 'message_text': '',
            'progress': 'old-work', 'question': '', 'question_id': ''}
    return dict(base, **kwargs)


class PolicyTests(unittest.TestCase):
    def test_custom_retry_delay_applies_to_every_attempt(self):
        for count in range(6):
            s=new_state();s['attempts']=count
            self.assertEqual(evaluate(s,snapshot(),100,retry_seconds=7)[0],'wait')
            self.assertEqual(s['due'],107)
            self.assertEqual(evaluate(s,snapshot(),106,retry_seconds=7)[0],'wait')
            self.assertEqual(evaluate(s,snapshot(),107,retry_seconds=7)[0],'retry')

    def test_custom_delay_used_after_uncertain_send(self):
        s=new_state();s['pending']={'marker':'x','created':100,'key':'retry:t1','kind':'retry'}
        evaluate(s,snapshot(),221,retry_seconds=300)
        self.assertEqual(s['due'],400)

    def test_backoff(self):
        for n, delay in enumerate(DELAYS):
            s = new_state(); s['attempts'] = n
            self.assertEqual(evaluate(s, snapshot(), 100)[0], 'wait')
            self.assertEqual(s['due'], 100 + delay)
            self.assertEqual(evaluate(s, snapshot(), 100 + delay)[0], 'retry')

    def test_six_attempt_limit(self):
        s = new_state(); s['attempts'] = 6
        self.assertEqual(evaluate(s, snapshot(), 100)[0], 'pause')

    def test_running_never_sends(self):
        s = new_state(); s.update(due=0, due_turn='t1')
        self.assertEqual(evaluate(s, snapshot(running=True), 100)[0], 'wait')

    def test_goal_gates(self):
        for goal in ['paused', 'complete', 'budget_limited', 'usage_limited']:
            self.assertEqual(evaluate(new_state(), snapshot(goal=goal), 100)[0], 'pause')

    def test_deleted_chat(self):
        self.assertEqual(evaluate(new_state(), {'exists': False}, 100)[0], 'pause')

    def test_no_goal_transient_failure_retries(self):
        s = new_state()
        self.assertEqual(evaluate(s, snapshot(goal=None), 100)[0], 'wait')
        self.assertEqual(evaluate(s, snapshot(goal=None), 160)[0], 'retry')

    def test_no_goal_running_or_completed_stays_quiet(self):
        self.assertEqual(evaluate(new_state(), snapshot(goal=None, running=True), 100)[0], 'wait')
        self.assertEqual(evaluate(new_state(), snapshot(goal=None, turn_status='completed', error=''), 100)[0], 'wait')

    def test_previously_seen_goal_deleted_stops(self):
        s = new_state()
        evaluate(s, snapshot(goal='active', running=True), 100)
        self.assertEqual(evaluate(s, snapshot(goal=None), 200)[0], 'pause')

    def test_no_goal_manual_stop_and_auth_failure_remain_gated(self):
        self.assertEqual(evaluate(new_state(), snapshot(goal=None, manual_stop=True), 100)[0], 'pause')
        self.assertEqual(evaluate(new_state(), snapshot(goal=None, error='401 authentication'), 100)[0], 'attention')

    def test_user_stop(self):
        self.assertEqual(evaluate(new_state(), snapshot(manual_stop=True), 100)[0], 'pause')
        self.assertEqual(evaluate(new_state(), snapshot(turn_status='interrupted'), 100)[0], 'pause')

    def test_blocked_alone_is_not_reason(self):
        self.assertEqual(evaluate(new_state(), snapshot(goal='blocked', error='unknown failure'), 100)[0], 'attention')

    def test_excluded_errors(self):
        for error in ['401 authentication', 'insufficient_quota', 'invalid_encrypted_content 400', 'budget limit exceeded', '403']:
            self.assertFalse(transient(error))

    def test_allowed_errors(self):
        for error in ['rateLimitExceeded', '429', '503 service unavailable', 'network timeout', 'overloaded']:
            self.assertTrue(transient(error))

    def test_duplicate_failure(self):
        s = new_state(); s['handled'] = ['retry:t1']
        self.assertEqual(evaluate(s, snapshot(), 1000)[0], 'wait')

    def test_pending_ack_only_once(self):
        s = new_state(); s['pending'] = {'marker': 'unique-marker', 'created': 10, 'key': 'retry:t1', 'kind': 'retry'}
        q = snapshot(message_text='unique-marker')
        evaluate(s, q, 20); evaluate(s, q, 21)
        self.assertEqual(s['attempts'], 1)
        self.assertIsNone(s['pending'])

    def test_ambiguous_send_keeps_monitoring(self):
        s = new_state(); s['pending'] = {'marker': 'unique-marker', 'created': 10, 'key': 'retry:t1', 'kind': 'retry'}
        self.assertEqual(evaluate(s, snapshot(), 20)[0], 'wait')
        self.assertEqual(evaluate(s, snapshot(running=True), 131)[0], 'attention')
        self.assertIsNotNone(s['pending'])
        self.assertEqual(evaluate(s, snapshot(running=True), 10000)[0], 'attention')

    def test_manual_reply_resolves_uncertain_delivery_without_approval(self):
        s = new_state(); s['pending'] = {'marker': 'unique-marker', 'created': 10, 'key': 'retry:t1', 'kind': 'retry'}
        evaluate(s, snapshot(last_user_at=9, running=True), 131)
        self.assertIsNotNone(s['pending'])
        self.assertEqual(evaluate(s, snapshot(last_user_at=20), 132)[0], 'wait')
        self.assertIsNone(s['pending'])
        self.assertEqual(s['attempts'], 1)
        self.assertFalse(s['approval'])
        self.assertIn('retry:t1', s['handled'])

    def test_manual_stop_overrides_pending_reply(self):
        s = new_state(); s['pending'] = {'marker': 'x', 'created': 10, 'key': 'retry:t1', 'kind': 'retry'}
        self.assertEqual(evaluate(s, snapshot(last_user_at=20, manual_stop=True), 132)[0], 'pause')

    def test_uncertain_retry_recovers_same_failed_turn_with_backoff(self):
        s = new_state()
        for n in range(6):
            s['pending'] = {'marker': str(n), 'created': 1000*n, 'key': 'retry:t1', 'kind': 'retry'}
            now = 1000*n + 121
            action, _ = evaluate(s, snapshot(), now)
            self.assertIsNone(s['pending'])
            self.assertEqual(s['attempts'], n+1)
            if n == 5:
                self.assertEqual(action, 'pause')
            else:
                self.assertEqual(action, 'wait')
                self.assertEqual(evaluate(s, snapshot(), s['due']-1)[0], 'wait')
                self.assertEqual(evaluate(s, snapshot(), s['due'])[0], 'retry')

    def test_uncertain_approval_and_nontransient_errors_never_resend(self):
        for kind, q in [('approval', snapshot()), ('retry', snapshot(error='401 authentication')),
                        ('retry', snapshot(question='允许删除存档吗？'))]:
            s = new_state(); s['pending'] = {'marker': 'x', 'created': 0, 'key': 'k', 'kind': kind}
            self.assertEqual(evaluate(s, q, 1000)[0], 'attention')
            self.assertIsNotNone(s['pending'])
            self.assertEqual(s['attempts'], 0)

    def test_real_progress_resets_count(self):
        s = new_state(); s.update(attempts=4, progress='old-work')
        evaluate(s, snapshot(progress='new-command', running=True), 10)
        self.assertEqual(s['attempts'], 0)

    def test_same_progress_no_reset(self):
        s = new_state(); s.update(attempts=4, progress='old-work')
        evaluate(s, snapshot(), 10)
        self.assertEqual(s['attempts'], 4)

    def test_safe_request(self):
        self.assertEqual(safe_approval('是否允许在当前项目中运行一次定向测试？'), '运行一次定向测试')

    def test_unsafe_or_quoted_requests(self):
        for q in ['是否允许删除正式存档？', '是否允许在当前项目中运行一次定向测试？\n然后删除全部数据',
                  '> 是否允许在当前项目中运行一次定向测试？', '日志：是否允许在当前项目中运行一次定向测试？',
                  '是否允许恢复接管？', '是否允许再次运行已耗尽次数的实机测试？']:
            self.assertIsNone(safe_approval(q))

    def test_approval_default_off_and_dedupe(self):
        q = snapshot(turn_status='completed', error='', question='是否允许在当前项目中运行一次定向测试？', question_id='q1')
        s = new_state()
        self.assertEqual(evaluate(s, q, 100)[0], 'attention')
        s['approval'] = True
        self.assertEqual(evaluate(s, q, 100)[0], 'approve')
        s['handled'] = ['approval:q1']
        self.assertEqual(evaluate(s, q, 100)[0], 'wait')


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patch = patch.object(app, 'DATA', Path(self.temp.name))
        self.patch.start()
        self.caller_patch = patch.object(app, 'local_caller', return_value='local-fixture')
        self.caller_patch.start()
        self.config = app.load()
        self.config['selected'] = 'target'
        app.save(app.DATA / 'config.json', self.config)
        (app.DATA / 'paused').unlink()
        self.calls = []
        class FakeBridge:
            def call(_, name, args):
                self.calls.append((name, args))
                return {'threadId': 'target'}
        self.bridge = FakeBridge()

    def tearDown(self):
        self.caller_patch.stop(); self.patch.stop(); self.temp.cleanup()

    def ready(self):
        s = app.state_for(self.config)
        s.update(due=1, due_turn='t1')

    def test_first_load_paused(self):
        (app.DATA / 'config.json').unlink()
        app.load()
        self.assertTrue((app.DATA / 'paused').exists())

    def test_manual_continue_restarts_stopped_monitor(self):
        s=app.state_for(self.config);s.update(stopped=True,last_check=100,reason='检测到用户主动停止；请明确恢复后重新开始监测')
        with patch.object(app,'snapshot',return_value=snapshot(resume_at=101,running=True)):
            result=app.tick(self.config,self.bridge,tid='target')
        self.assertFalse(result['state']['stopped'])
        self.assertEqual(self.calls,[])

    def test_continue_does_not_lift_control_stop(self):
        s=app.state_for(self.config);s.update(stopped=True,stopped_at=100,stop_kind='manual')
        with patch.object(app,'snapshot',return_value=snapshot(resume_at=101,control_stop_at=90,control_stopped=True,manual_stop=False)):
            result=app.tick(self.config,self.bridge,tid='target')
        self.assertFalse(result['state']['stopped'])
        self.assertIn('等待退避时间',result['reason'])
        self.assertEqual(self.calls,[])

    def test_control_stop_does_not_block_running_chat(self):
        with patch.object(app,'snapshot',return_value=snapshot(control_stopped=True,running=True)):
            result=app.tick(self.config,self.bridge,tid='target')
        self.assertIn('正在执行',result['reason'])
        self.assertFalse(result['notify'])
        self.assertEqual(self.calls,[])

    def test_retry_carries_control_restriction(self):
        self.ready()
        with patch.object(app,'snapshot',return_value=snapshot(control_stopped=True)):
            app.tick(self.config,self.bridge)
        self.assertEqual(len(self.calls),1)
        self.assertIn('不得调用电脑控制工具',self.calls[0][1]['prompt'])

    def test_control_stop_never_automatically_approves(self):
        s=new_state();s['approval']=True
        q=snapshot(control_stopped=True,turn_status='completed',question='是否允许在当前项目中运行一次定向测试？',question_id='q')
        self.assertEqual(evaluate(s,q,100)[0],'attention')

    def test_old_continue_global_pause_and_other_stops_remain_stopped(self):
        for kind,resume,global_pause in [('manual',99,False),('other',101,False),('manual',101,True)]:
            s=app.state_for(self.config);s.update(stopped=True,stopped_at=100,stop_kind=kind,reason='停止')
            if global_pause:app.pause()
            with patch.object(app,'snapshot',return_value=snapshot(resume_at=resume)):
                result=app.tick(self.config,self.bridge,tid='target')
            self.assertTrue(result['state']['stopped'])
            self.assertEqual(self.calls,[])

    def test_send_is_ledgered_before_dispatch(self):
        self.ready()
        with patch.object(app, 'snapshot', return_value=snapshot()):
            app.tick(self.config, self.bridge)
        self.assertEqual(len(self.calls), 1)
        saved = app.load()['threads']['target']
        self.assertIsNotNone(saved['pending'])
        self.assertIn(saved['pending']['marker'], self.calls[0][1]['prompt'])
        self.assertEqual(saved['attempts'], 0)

    def test_stop_before_send(self):
        self.ready()
        calls = []
        def stop_on_recheck(*args):
            calls.append(1)
            if len(calls) == 2:
                app.pause()
            return snapshot()
        with patch.object(app, 'snapshot', side_effect=stop_on_recheck):
            app.tick(self.config, self.bridge)
        self.assertEqual(self.calls, [])

    def test_state_changes_before_send(self):
        self.ready()
        with patch.object(app, 'snapshot', side_effect=[snapshot(), snapshot(running=True)]):
            app.tick(self.config, self.bridge)
        self.assertEqual(self.calls, [])

    def test_observe_never_sends(self):
        self.ready()
        with patch.object(app, 'snapshot', return_value=snapshot()):
            app.tick(self.config, self.bridge, dry=True)
        self.assertEqual(self.calls, [])

    def test_timeout_does_not_resend(self):
        self.ready()
        def failure(*args): raise TimeoutError()
        self.bridge.call = failure
        with patch.object(app, 'snapshot', return_value=snapshot()):
            app.tick(self.config, self.bridge)
            loaded = app.load()
            result = app.tick(loaded, self.bridge)
        self.assertEqual(result['reason'], '正在核对上一条消息是否送达')

    def test_manual_review_then_running_never_pauses_or_sends(self):
        q = snapshot(turn_status='completed', error='', question='是否允许修改正式存档？', question_id='q1')
        with patch.object(app, 'snapshot', side_effect=[q, snapshot(running=True, turn_id='t2')]):
            first = app.tick(self.config, self.bridge)
            second = app.tick(app.load(), self.bridge)
        self.assertFalse(first['paused'])
        self.assertFalse(second['paused'])
        self.assertEqual(self.calls, [])
        self.assertIn('正在执行', second['reason'])

    def test_uncertainty_survives_restart_without_pause(self):
        s = app.state_for(self.config)
        s['pending'] = {'marker': 'x', 'created': 0, 'key': 'retry:t1', 'kind': 'retry'}
        with patch.object(app, 'snapshot', return_value=snapshot(running=True)):
            first = app.tick(self.config, self.bridge)
            second = app.tick(app.load(), self.bridge)
        self.assertFalse(first['paused'])
        self.assertFalse(second['paused'])
        self.assertEqual(self.calls, [])
        self.assertIsNotNone(app.load()['threads']['target']['pending'])

    def test_authorization_isolated(self):
        app.state_for(self.config)['approval'] = True
        self.config['selected'] = 'other'
        self.assertFalse(app.state_for(self.config)['approval'])

    def test_binding_cancels_old_due_and_does_not_inherit_permission(self):
        old = app.state_for(self.config)
        old.update(approval=True, due=500)
        app.save(app.DATA / 'config.json', self.config)
        new_id = '22222222-2222-4222-8222-222222222222'
        with patch.object(app, 'Bridge') as cls:
            cls.return_value.pipe = 'test'
            cls.return_value.call.return_value = {'thread': {'kind': 'codex', 'title': '同名聊天'}}
            result = app.command(SimpleNamespace(action='bind', value=new_id))
        saved = app.load()
        self.assertEqual(result['selected'], new_id)
        self.assertFalse(saved['threads'][new_id]['approval'])
        self.assertIsNone(saved['threads']['target']['due'])
        self.assertTrue((app.DATA / 'paused').exists())

    def test_invalid_binding_preserves_existing_selection(self):
        with patch.object(app, 'Bridge') as cls:
            cls.return_value.pipe = 'test'
            with self.assertRaises(ValueError):
                app.command(SimpleNamespace(action='bind', value='not-a-uuid'))
        self.assertEqual(app.load()['selected'], 'target')

    def test_disconnected_monitor_does_not_send(self):
        self.ready()
        with patch.object(app, 'snapshot', side_effect=app.Disconnected('offline')):
            with self.assertRaises(app.Disconnected):
                app.tick(self.config, self.bridge)
        self.assertEqual(self.calls, [])

    def test_fresh_install_has_no_personal_ids(self):
        (app.DATA / 'config.json').unlink()
        config = app.load()
        self.assertEqual(config['selected'], '')
        self.assertEqual(config['targets'], [])
        self.assertEqual(config['interval'], 30)

    def test_multi_one_completed_does_not_stop_other(self):
        self.config['targets'] = ['target', 'other']
        app.save(app.DATA / 'config.json', self.config)
        app.state_for(self.config, 'other').update(due=1, due_turn='t1')
        with patch.object(app, 'snapshot', side_effect=[snapshot(goal='complete'), snapshot(), snapshot()]):
            app.tick(self.config, self.bridge, tid='target')
            app.tick(self.config, self.bridge, tid='other')
        self.assertFalse((app.DATA / 'paused').exists())
        self.assertTrue(app.state_for(self.config, 'target')['stopped'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1]['threadId'], 'other')
        self.assertIsNone(app.state_for(self.config, 'target')['pending'])
        self.assertIsNotNone(app.state_for(self.config, 'other')['pending'])

    def test_multi_removed_target_cancels_send(self):
        self.ready(); self.config['targets'] = ['other']
        app.save(app.DATA / 'config.json', self.config)
        with patch.object(app, 'snapshot', return_value=snapshot()):
            app.tick(self.config, self.bridge, tid='target')
        self.assertEqual(self.calls, [])

    def test_configure_validates_interval_and_preserves_authorizations(self):
        ids = ['22222222-2222-4222-8222-222222222222', '33333333-3333-4333-8333-333333333333']
        self.config['threads'][ids[0]] = dict(new_state(), approval=True)
        app.save(app.DATA / 'config.json', self.config)
        with patch.object(app, 'Bridge') as cls:
            cls.return_value.pipe = 'test'
            cls.return_value.call.return_value = {'thread': {'kind': 'codex', 'hostId': 'local'}}
            for bad in [0, 9, 86401, 1.5, True, '30']:
                with self.assertRaises(ValueError):
                    app.command(SimpleNamespace(action='configure', value=json.dumps({'ids':ids,'interval':bad})))
            for bad in [0,4,86401,True,1.5,'5']:
                with self.assertRaises(ValueError):
                    app.command(SimpleNamespace(action='configure', value=json.dumps({'ids':ids,'interval':75,'retry_seconds':bad})))
            app.command(SimpleNamespace(action='configure', value=json.dumps({'ids':ids,'interval':75,'retry_seconds':7})))
        saved = app.load()
        self.assertEqual(saved['targets'], ids)
        self.assertEqual(saved['interval'], 75)
        self.assertEqual(saved['retry_seconds'],7)
        self.assertTrue(app.state_for(saved, ids[0])['approval'])
        self.assertFalse(app.state_for(saved, ids[1])['approval'])
        self.assertTrue((app.DATA / 'paused').exists())

    def test_batch_connection_failure_does_not_skip_other(self):
        self.config['targets'] = ['target', 'other']
        app.save(app.DATA / 'config.json', self.config)
        with patch.object(app, 'Bridge') as cls, patch.object(app, 'snapshot', side_effect=[app.Disconnected('offline'), snapshot(running=True)]):
            cls.return_value.pipe = 'test'
            result = app.command(SimpleNamespace(action='tick', value=''))
        self.assertEqual(len(result['results']), 2)
        self.assertEqual(result['results'][0]['error_type'], 'Disconnected')
        self.assertIn('正在执行', result['results'][1]['reason'])

    def test_legacy_config_migration(self):
        config = {'version':1, 'selected':'old-local-id', 'threads':{'old-local-id':dict(new_state(), attempts=3)},'pipe':''}
        app.save(app.DATA / 'config.json', config)
        migrated = app.load()
        self.assertEqual(migrated['targets'], ['old-local-id'])
        self.assertEqual(migrated['threads']['old-local-id']['attempts'], 3)

    def test_dynamic_caller_uses_target(self):
        from bridge import Bridge
        b = Bridge(); b.caller='local-bootstrap'
        with patch.object(b, 'request', return_value={'success':True,'contentItems':[{'type':'inputText','text':'{}'}]}) as request:
            b.call('read_thread', {'threadId':'selected-local'})
            self.assertEqual(request.call_args.args[2]['threadId'], 'local-bootstrap')
            b.call('list_threads', {})
            self.assertEqual(request.call_args.args[2]['threadId'], 'local-bootstrap')


class ContextTests(unittest.TestCase):
    def test_control_stop_ignores_assistant_and_quoted_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'history.jsonl'
            sentinel='Computer Use was stopped by the user with the physical Escape key. Stop your work, do not call further tools'
            rows=[{'type':'response_item','timestamp':'2026-09-29T01:00:00Z','payload':{'type':'message','role':'assistant','content':[{'text':'解释实体 Esc 停止接管。'+sentinel}]}},
                  {'type':'response_item','timestamp':'2026-09-29T01:01:00Z','payload':{'type':'custom_tool_call_output','output':json.dumps({'quoted':sentinel})}}]
            path.write_text(''.join(json.dumps(r)+'\n' for r in rows),encoding='utf-8')
            self.assertEqual(app.history_control_stop(path),(False,0))
            rows.append({'type':'response_item','timestamp':'2026-09-29T01:02:00Z','payload':{'type':'custom_tool_call_output','output':[{'type':'input_text','text':'Script completed'},{'type':'input_text','text':sentinel}]}})
            path.write_text(''.join(json.dumps(r)+'\n' for r in rows),encoding='utf-8')
            self.assertTrue(app.history_control_stop(path)[0])
            rows.append({'type':'response_item','timestamp':'2026-09-29T01:03:00Z','payload':{'type':'message','role':'user','content':[{'text':'恢复接管'}]}})
            path.write_text(''.join(json.dumps(r)+'\n' for r in rows),encoding='utf-8')
            self.assertFalse(app.history_control_stop(path)[0])

    def test_delegated_receipt_and_quoted_output_are_distinct(self):
        marker = '[自动续接助手:abc123]'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'rollout.jsonl'
            pending = {'marker':marker}
            quoted = {'type':'response_item','payload':{'type':'function_call_output','name':'exec_command','namespace':'functions','output':marker}}
            path.write_text(json.dumps(quoted)+'\n', encoding='utf-8')
            self.assertFalse(app.history_receipt(path, pending))
            old_offset = pending['history_offset']
            actual = {'type':'response_item','payload':{'type':'function_call_output','name':'send_message_to_thread','namespace':'codex_app','output':'<codex_delegation><input>'+marker+'</input></codex_delegation>'}}
            with path.open('a',encoding='utf-8') as f: f.write(json.dumps(actual)+'\n')
            self.assertTrue(app.history_receipt(path,pending))
            self.assertGreater(old_offset,0)

    def test_receipt_partial_line_is_rechecked(self):
        marker='[自动续接助手:abc123]'
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'rollout.jsonl';pending={'marker':marker}
            line=json.dumps({'type':'response_item','payload':{'type':'message','role':'user','content':[{'text':marker}]}})
            path.write_text(line,encoding='utf-8')
            self.assertFalse(app.history_receipt(path,pending))
            with path.open('a',encoding='utf-8') as f:f.write('\n')
            self.assertTrue(app.history_receipt(path,pending))

    def test_pending_progress_resets_attempts_before_receipt(self):
        s=new_state();s.update(attempts=5,progress='old',pending={'marker':'x','created':100,'key':'retry:t1','kind':'retry'})
        evaluate(s,snapshot(progress='new',running=True),101)
        self.assertEqual(s['attempts'],0)
        evaluate(s,snapshot(progress='new',message_text='x'),102)
        self.assertEqual(s['attempts'],0)
        self.assertIsNone(s['pending'])

    def test_context_excludes_monitored_thread(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with sqlite3.connect(home / 'state_5.sqlite') as c:
                c.execute('create table threads(id text, archived integer, updated_at integer)')
                c.executemany('insert into threads values(?,?,?)', [('target',0,20),('context',0,10),('archived',1,30)])
            c.close()
            with patch.object(app, 'HOME', home):
                self.assertEqual(app.local_caller(exclude='target'), 'context')
                self.assertEqual(app.local_caller(exclude='context'), 'target')

    def test_only_one_chat_reports_missing_context(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with sqlite3.connect(home / 'state_5.sqlite') as c:
                c.execute('create table threads(id text, archived integer, updated_at integer)')
                c.execute("insert into threads values('target',0,20)")
            c.close()
            with patch.object(app, 'HOME', home):
                with self.assertRaises(app.Disconnected):
                    app.local_caller(exclude='target')


if __name__ == '__main__':
    unittest.main(verbosity=2)
