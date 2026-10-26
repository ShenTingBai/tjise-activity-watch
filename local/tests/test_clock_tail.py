# -*- coding: utf-8 -*-
"""校时收尾回归：生产入口，显式隔离真实数据。

@author LiXinwen
"""
import contextlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

LOCAL = Path(__file__).resolve().parents[1]
if str(LOCAL) not in sys.path:
    sys.path.insert(0, str(LOCAL))
if str(LOCAL / 'tests') not in sys.path:
    sys.path.insert(0, str(LOCAL / 'tests'))
from test_ui_contract import run_js
from business_clock import BusinessClock
from fixtures.business_clock import ntp_response


class PreferenceLockTailTests(unittest.TestCase):
    def test_timeout_never_writes_without_file_lock(self):
        app = importlib.import_module('app')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'ui.json'
            raw = b'{"lifecycle_v2":{"run_id":"new-owner"}}'
            path.write_bytes(raw)
            @contextlib.contextmanager
            def busy(*args, **kwargs):
                raise app._PrefsLockTimeout('held')
                yield
            with mock.patch.object(app, 'PREFS', path), \
                    mock.patch.object(app, '_prefs_transaction', busy):
                result = app.update_prefs({'headed': True})
            self.assertEqual(path.read_bytes(), raw, '超时不能绕过跨进程锁写文件')
            self.assertFalse(result)

    def test_api_does_not_acknowledge_unsaved_preferences(self):
        app = importlib.import_module('app')
        with mock.patch.object(app, 'update_prefs', return_value=False):
            result = app.Api().save_prefs({'headed': True})
        self.assertFalse(result['ok'], '保存失败不能回执成功')
        self.assertEqual(result['error'], 'prefs_busy')

    def test_real_other_process_holds_lock_past_save_deadline(self):
        app = importlib.import_module('app')
        code = '''import sys,json
from pathlib import Path
import app
p=Path(sys.argv[1])
with app._prefs_transaction(p):
 print('locked',flush=True)
 sys.stdin.readline()
 d=app.load_prefs_from(p)
 d['lifecycle_v2']={'run_id':'new-owner','quality':'ready'}
 app._atomic_write_prefs(p,d)
'''
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'ui.json'
            path.write_text('{}', encoding='utf-8')
            env = dict(os.environ, TJISE_DATA_DIR=tmp,
                       PYTHONPATH=str(LOCAL))
            proc = subprocess.Popen([sys.executable, '-X', 'utf8', '-c', code, str(path)],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, encoding='utf-8', env=env)
            try:
                self.assertEqual(proc.stdout.readline().strip(), 'locked')
                with mock.patch.object(app, 'PREFS', path), \
                        mock.patch.object(app, '_PREFS_SAVE_TIMEOUT_S', 0.05):
                    result = app.update_prefs({'headed': True})
                self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {},
                                 '另一进程仍持锁时不允许偏好写入')
                self.assertFalse(result)
                proc.communicate('\n', timeout=5)
                self.assertEqual(proc.returncode, 0)
                with mock.patch.object(app, 'PREFS', path):
                    self.assertTrue(app.update_prefs({'headed': True}))
                saved = json.loads(path.read_text(encoding='utf-8'))
                self.assertEqual(saved['lifecycle_v2']['run_id'], 'new-owner')
                self.assertTrue(saved['headed'])
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate(timeout=5)


class SyncProgressTailTests(unittest.TestCase):
    def test_failed_rounds_advance_attempt_identity_without_clock_generation(self):
        def offline(*args):
            raise OSError('offline')
        with tempfile.TemporaryDirectory() as tmp:
            clock = BusinessClock(sources=('a', 'b'), transport=offline, snapshot_dir=tmp,
                                  per_source_cooldown_s=0)
            try:
                self.assertEqual(clock.snapshot().get('sync_round'), 0)
                clock.sync()
                self.assertEqual(clock.snapshot().get('sync_round'), 1)
                clock.sync()
                self.assertEqual(clock.snapshot().get('sync_round'), 2)
                self.assertEqual(clock.generation, 0)
            finally:
                clock.close()

    def test_finished_status_is_also_published_to_snapshot(self):
        def transport(source, packet, timeout):
            return ntp_response(packet[40:48], 1793016000, 1793016000), 1000, 1000, True
        with tempfile.TemporaryDirectory() as tmp:
            clock = BusinessClock(sources=('a', 'b'), tick_fn=lambda: 1000,
                                  transport=transport, snapshot_dir=tmp)
            try:
                self.assertEqual(clock.sync(), 'ready')
                data = json.loads(Path(clock._snapshot_path()).read_text(encoding='utf-8'))
                self.assertEqual(data.get('sync_status'), 'succeeded')
            finally:
                clock.close()

    def test_real_sync_reports_inflight_then_failure(self):
        entered, release = threading.Event(), threading.Event()
        def transport(*args):
            entered.set()
            release.wait(2)
            raise OSError('offline')
        with tempfile.TemporaryDirectory() as tmp:
            clock = BusinessClock(sources=('a', 'b'), transport=transport,
                                  snapshot_dir=tmp, background=False)
            thread = threading.Thread(target=clock.sync)
            try:
                thread.start()
                self.assertTrue(entered.wait(1))
                self.assertEqual(clock.snapshot().get('sync_status'), 'syncing')
            finally:
                release.set()
                thread.join(3)
                clock.close()
            self.assertFalse(thread.is_alive())
            self.assertEqual(clock.snapshot().get('sync_status'), 'failed')


class ClockDisplayTailTests(unittest.TestCase):
    def test_retry_feedback_stops_claiming_old_time_is_usable_after_expiry(self):
        result = run_js(r'''
const snap={schema:1,owner_id:'harness-clock',generation:20,state:'ready',
 epoch_s:1793016000,tick_s:0,sampled_tick_s:0,age_s:0,uncertainty_s:0.1,
 sync_status:'succeeded',sync_round:5};
ingestBusinessClock(snap);
window.pywebview={api:{sync_clock:()=>Promise.resolve({ok:true})}};
resyncClock();
ingestBusinessClock(Object.assign({},snap,{sync_round:6,sync_status:'failed'}));
paintClockPopFeedback(); out.push($('#clock-pop-feedback').textContent);
__advanceMono(900001);
paintClockPopFeedback(); out.push($('#clock-pop-feedback').textContent);
''')
        self.assertIn('仍使用上次校准结果', result[0])
        self.assertNotIn('仍使用', result[1])
        self.assertIn('重试', result[1])

    def test_clock_result_before_ack_is_not_replaced_by_timeout(self):
        result = run_js(r'''
const snap={schema:1,owner_id:'harness-clock',generation:20,state:'ready',
 epoch_s:1793016000,tick_s:0,sampled_tick_s:0,age_s:0,uncertainty_s:0.1,
 sync_status:'succeeded',sync_round:5};
ingestBusinessClock(snap);
const timers=[]; setTimeout=(fn)=>{timers.push(fn);return timers.length;};
let ack; window.pywebview={api:{sync_clock:()=>new Promise(r=>{ack=r;})}};
resyncClock();
ingestBusinessClock(Object.assign({},snap,{sync_round:6,sync_status:'failed'}));
ack({ok:true});
for(let i=0;i<5;i++) await Promise.resolve();
out.push(S.businessClock.syncState,timers.length,S.businessClock.syncError);
''')
        self.assertEqual(result[:2], ['idle', 0])
        self.assertIn('未成功', result[2])

    def test_manual_sync_failure_settles_same_generation_and_ignores_old_round(self):
        result = run_js(r'''
const snap={schema:1,owner_id:'harness-clock',generation:20,state:'ready',
 epoch_s:1793016000,tick_s:0,sampled_tick_s:0,age_s:0,uncertainty_s:0.1,
 sync_status:'succeeded',sync_round:5};
ingestBusinessClock(snap);
window.pywebview={api:{sync_clock:()=>Promise.resolve({ok:true}),
 clock_snapshot:()=>Promise.resolve(snap)}};
resyncClock(); await Promise.resolve(); await Promise.resolve();
ingestBusinessClock(Object.assign({},snap,{sync_status:'failed'}));
out.push(S.businessClock.syncState);
ingestBusinessClock(Object.assign({},snap,{sync_round:6,sync_status:'failed'}));
out.push(S.businessClock.syncState,S.businessClock.syncError,
 businessNowMs()!==null,S.businessClock.gen);
resyncClock();
ingestBusinessClock(Object.assign({},snap,{generation:21,sync_round:7,sync_status:'failed'}));
out.push(S.businessClock.syncState,S.businessClock.syncError,S.businessClock.gen);
''')
        self.assertEqual(result[0], 'syncing', '请求前的旧失败不能结束新校准')
        self.assertEqual(result[1], 'idle', '本轮失败即使generation没变也必须结束等待')
        self.assertIn('未成功', result[2])
        self.assertEqual(result[3:5], [True, 20])
        self.assertEqual(result[5], 'idle')
        self.assertIn('未成功', result[6], '携带更近锚点不能抹掉本次失败结论')
        self.assertEqual(result[7], 21)

    def test_preference_requests_serialize_and_retry_latest_value(self):
        result = run_js(r'''
const calls=[], replies=[], timers=[];
setTimeout=(fn,ms)=>{timers.push(fn);return timers.length;}; clearTimeout=()=>{};
window.pywebview={api:{save_prefs:(p)=>{
 calls.push(Object.assign({},p)); return new Promise(r=>replies.push(r));}}};
const first=savePrefsNow({headed:false});
savePrefsNow({headed:true}); out.push(calls.length);
replies[0]({ok:false,text:'busy'}); await first;
if(timers.length) timers.shift()();
await Promise.resolve(); out.push(calls.map(x=>x.headed));
replies[1]({ok:true});
for(let i=0;i<6;i++) await Promise.resolve();
out.push(S.prefs.headed,Object.keys(S.savePending).length);
''')
        self.assertEqual(result, [1, [False, True], True, 0])

    def test_failed_preferences_retry_once_and_report_once(self):
        result = run_js(r'''
let calls=0; const timers=[];
setTimeout=(fn,ms)=>{timers.push(fn);return timers.length;}; clearTimeout=()=>{};
window.pywebview={api:{save_prefs:()=>{calls++;return Promise.resolve({ok:false});}}};
await savePrefsNow({headed:true});
if(timers.length) await timers.shift()();
out.push(calls,timers.length,S.savePending.headed,S.prefs.headed,
 $('#log')._children.filter(x=>x.textContent.includes('设置暂未保存')).length);
''')
        self.assertEqual(result, [2, 0, True, True, 1])

    def test_boot_keeps_actual_first_sync_in_progress(self):
        result = run_js(r'''
const snap = {schema:1,owner_id:'harness-clock',generation:2,state:'unavailable',
 epoch_s:null,tick_s:null,sampled_tick_s:0,age_s:null,uncertainty_s:0,
 sync_status:'syncing',reason:'正在获取北京时间'};
window.pywebview={api:{
 bootstrap:()=>Promise.resolve({version:'1.1.1',demo:false,busy:false,
  prefs:{welcome_seen:true,tour_version:2},business_clock:snap,
  login:{session_present:false}}),
 clock_snapshot:()=>Promise.resolve(snap),front_ready:()=>Promise.resolve({ok:true}),
 check:(id)=>Promise.resolve({ok:true,request_id:id}),
 list_activities:(id)=>Promise.resolve({ok:true,request_id:id})}};
S.businessClock={anchorEpochMs:0,perfAt:0,sampleAgeMs:0,uncertaintyMs:0,
 gen:0,owner:'',state:'unavailable',attempted:false};
await boot(); await Promise.resolve();
out.push(clockUiState());
ingestBusinessClock(Object.assign({},snap,{sync_status:'failed'}));
out.push(clockUiState());
''')
        self.assertEqual(result, ['syncing', 'unavailable'])

    def test_unknown_clock_has_complete_target_and_correct_led(self):
        result = run_js(r'''
S.items=[{id:'known',name:'活动',sign_start:'2026-10-26 21:00:00'}];
S.byId=new Map(S.items.map(x=>[x.id,x])); S.selected.clear();
S.businessClock.anchorEpochMs=0; S.businessClock.state='unavailable';
S.businessClock.attempted=true; S.demo=false;
const target=nextOpen(); paintLed();
out.push(Object.keys(target).sort(), $('#led-label b').textContent);
''')
        self.assertEqual(result[0], ['best', 'bestId', 'futureId', 'openId', 'openNow'])
        self.assertEqual(result[1], '待校准')

    def test_logs_use_business_time_and_preserve_worker_source_time(self):
        result = run_js(r'''
const RealDate=Date;
globalThis.Date=class extends RealDate {
 constructor(...args){super(...(args.length?args:[0]));} static now(){return 0;}
};
setBusinessNow(toTs('2026-10-26 20:00:00')*1000);
addLog('[i] UI消息'); out.push($('#log')._children[0].textContent);
addLog('2026-10-26 19:59:00 [i] worker消息');
out.push($('#log')._children[1].textContent);
S.businessClock.state='unavailable';
addLog('[i] 未校准消息'); out.push($('#log')._children[2].textContent);
''')
        self.assertTrue(result[0].startswith('20:00:00  '), result[0])
        self.assertEqual(result[1], '2026-10-26 19:59:00 [i] worker消息')
        self.assertTrue(result[2].startswith('待校准  '), result[2])

    def test_today_filter_uses_beijing_date_in_every_os_timezone(self):
        for zone in ['Asia/Shanghai', 'UTC', 'America/Los_Angeles']:
            with self.subTest(zone=zone):
                result = run_js(r'''
setBusinessNow(toTs('2026-10-26 01:00:00')*1000);
S.filter={q:'',status:'all',time:'today',sort:'open',checkin:'all',checkout:'all'};
S.items=[{id:'today',name:'今日',sign_start:'2026-10-26 22:00:00'},
 {id:'tomorrow',name:'明日',sign_start:'2026-10-27 01:00:00'}];
out.push(filtered().map(x=>x.id));
''', env={'TZ': zone})
                self.assertEqual(result, [['today']])
