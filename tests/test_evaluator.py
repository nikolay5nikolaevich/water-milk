import copy
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
import core
import evaluator
import server


class EvaluationRunnerTests(unittest.TestCase):
    def setUp(self):
        self.d=core.demo_dialogues()[1]
        self.kb=core.demo_kb()
        self.report=core.reference_report(self.d,self.kb)

    def test_api_key_authentication_is_not_accepted(self):
        with patch.object(evaluator,'codex_command',return_value=['codex']), patch.object(evaluator,'invoke',return_value=(0,'','Logged in using an API key')):
            self.assertFalse(evaluator.subscription_status()['available'])

    def test_chatgpt_authentication_is_accepted(self):
        with patch.object(evaluator,'codex_command',return_value=['codex']), patch.object(evaluator,'invoke',return_value=(0,'','Logged in using ChatGPT')):
            self.assertTrue(evaluator.subscription_status()['available'])

    def test_api_credentials_are_removed_from_child_environment(self):
        with patch.dict(os.environ,{'OPENAI_API_KEY':'secret','CODEX_API_KEY':'secret','OPENAI_BASE_URL':'https://example.invalid'}):
            environment=evaluator.subscription_env()
        self.assertNotIn('OPENAI_API_KEY',environment)
        self.assertNotIn('CODEX_API_KEY',environment)
        self.assertNotIn('OPENAI_BASE_URL',environment)

    def test_no_company_conditions_does_not_use_demo_prices(self):
        self.d['scope']='company'
        kb=core.evaluation_kb(self.kb,self.d)
        self.assertEqual('general_rubric',kb['mode'])
        self.assertNotIn('F01',{r['id'] for r in kb['rules']})
        schema=evaluator.report_schema(self.d,kb)
        for key in ('accuracy','compliance'):
            self.assertEqual([None],schema['properties']['scores']['properties'][key]['properties']['points']['enum'])

    def test_partial_history_enforced_by_output_schema(self):
        self.d['complete']=False
        schema=evaluator.report_schema(self.d,self.kb)
        for key in ('completeness','initiative','compliance'):
            self.assertEqual([None],schema['properties']['scores']['properties'][key]['properties']['points']['enum'])

    def test_selected_demo_basis_enables_all_five_visible_answer_scores(self):
        self.d['scope']='company'
        self.d['complete']=False
        self.d['source']={'type':'amocrm_browser_chat','card_verified':False}
        kb=core.evaluation_kb(self.kb,self.d,'demo')
        self.assertEqual('demo',kb['scope'])
        self.assertEqual('company',self.d['scope'])  # Import metadata is not rewritten.
        schema=evaluator.report_schema(self.d,kb)
        for key,(_,maximum) in core.CRITERIA.items():
            self.assertIn(maximum,schema['properties']['scores']['properties'][key]['properties']['points']['enum'])
        self.assertEqual([False],schema['properties']['history_complete']['enum'])
        self.assertEqual(['visible_messages'],schema['properties']['assessment_scope']['enum'])

    def test_visible_answer_report_can_score_communication_without_card(self):
        self.d['complete']=False
        self.d['context']=''
        self.d['source']={'type':'amocrm_browser_chat','card_verified':False}
        kb=core.evaluation_kb(self.kb,self.d,'demo')
        report=core.reference_report(self.d,kb)
        self.assertEqual(10,report['scores']['compliance']['points'])
        self.assertEqual(10,report['scores']['completeness']['points'])
        self.assertEqual('visible_messages',report['assessment_scope'])
        self.assertFalse(report['history_complete'])
        self.assertEqual(90,core.total(core.validate_report(report,self.d,kb)))

    def test_auto_basis_still_does_not_use_demo_facts_for_company(self):
        self.d['scope']='company'
        kb=core.evaluation_kb(self.kb,self.d,'auto')
        self.assertEqual('general_rubric',kb['mode'])
        self.assertNotIn('F01',{r['id'] for r in kb['rules']})

    def test_runner_validates_and_bounds_automatic_correction(self):
        invalid=copy.deepcopy(self.report)
        invalid['findings'][0]['quote']='Fabricated quote'
        calls=[]
        def invoke(args,**kwargs):
            calls.append((args,kwargs))
            output=Path(args[args.index('--output-last-message')+1])
            output.write_text(core.dump(invalid if len(calls)==1 else self.report),encoding='utf-8')
            return 0,'',''
        stages=[]
        with tempfile.TemporaryDirectory() as temporary, patch.object(evaluator,'ROOT',Path(temporary)), patch.object(evaluator,'codex_command',return_value=['codex']), patch.object(evaluator,'subscription_status',return_value={'available':True}), patch.object(evaluator,'invoke',side_effect=invoke):
            result=evaluator.run_check(self.d,self.kb,stages.append)
        self.assertEqual(self.report,result)
        self.assertEqual(2,len(calls))
        self.assertIn('correcting',stages)
        args=calls[0][0]
        self.assertIn('forced_login_method="chatgpt"',args)
        self.assertEqual('read-only',args[args.index('--sandbox')+1])
        self.assertIn('--ignore-user-config',args)
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox',args)

    def test_invalid_model_output_not_returned_as_success(self):
        def invoke(args,**kwargs):
            Path(args[args.index('--output-last-message')+1]).write_text('{}',encoding='utf-8')
            return 0,'',''
        with tempfile.TemporaryDirectory() as temporary, patch.object(evaluator,'ROOT',Path(temporary)), patch.object(evaluator,'codex_command',return_value=['codex']), patch.object(evaluator,'subscription_status',return_value={'available':True}), patch.object(evaluator,'invoke',side_effect=invoke) as process:
            with self.assertRaises(evaluator.EvaluationError):
                evaluator.run_check(self.d,self.kb)
        self.assertEqual(2,process.call_count)

    def test_quota_error_is_clear_and_not_retried(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(evaluator,'ROOT',Path(temporary)), patch.object(evaluator,'codex_command',return_value=['codex']), patch.object(evaluator,'subscription_status',return_value={'available':True}), patch.object(evaluator,'invoke',return_value=(1,'','usage_limit_reached')) as process:
            with self.assertRaisesRegex(evaluator.EvaluationError,'лимит'):
                evaluator.run_check(self.d,self.kb)
        self.assertEqual(1,process.call_count)


class CheckEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.old_db=server.DB
        server.DB=Path(self.temporary.name)/'qa.sqlite3'
        server.init_db()
        self.d=core.demo_dialogues()[1]
        with server.connect() as db:
            db.execute('INSERT INTO dialogues VALUES (?,?)',(self.d['id'],core.dump(self.d)))
        self.httpd=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        self.thread=threading.Thread(target=self.httpd.serve_forever,daemon=True)
        self.thread.start()
        self.base=f'http://127.0.0.1:{self.httpd.server_port}'
        self.release=threading.Event()

    def tearDown(self):
        self.release.set()
        self.httpd.shutdown();self.httpd.server_close();self.thread.join()
        server.DB=self.old_db
        self.temporary.cleanup()

    def request(self,path,body=None):
        req=Request(self.base+path,data=None if body is None else core.dump(body).encode(),headers={'Content-Type':'application/json','X-Workbench-Token':server.TOKEN})
        with urlopen(req,timeout=5) as response:return json.load(response)

    def finish(self,job):
        for _ in range(100):
            result=self.request('/api/check/jobs/'+job['id'])
            if result['status'] not in server.CHECK_ACTIVE:return result
            time.sleep(.02)
        self.fail('Check did not finish')

    def test_button_creates_real_source_run_and_saves_rule_snapshot(self):
        with patch.object(server,'run_check',side_effect=lambda d,kb,progress:core.reference_report(d,kb)):
            job=self.request('/api/check/start',{'dialogue_id':self.d['id']})
            result=self.finish(job)
        self.assertEqual('complete',result['status'])
        state=self.request('/api/state')
        run=state['runs'][0]
        self.assertEqual('codex_subscription',run['source'])
        self.assertEqual(90,run['total'])
        self.assertEqual([],run['reviews'])
        self.assertEqual('DEMO-1.0',run['kb']['version'])

    def test_double_click_reuses_active_job(self):
        def slow(d,kb,progress):
            self.release.wait(3)
            return core.reference_report(d,kb)
        with patch.object(server,'run_check',side_effect=slow):
            first=self.request('/api/check/start',{'dialogue_id':self.d['id']})
            second=self.request('/api/check/start',{'dialogue_id':self.d['id']})
            self.assertEqual(first['id'],second['id'])
            self.release.set();self.finish(first)
        self.assertEqual(1,len(self.request('/api/state')['runs']))

    def test_failed_check_does_not_create_result(self):
        with patch.object(server,'run_check',side_effect=evaluator.EvaluationError('Нужен вход')):
            job=self.request('/api/check/start',{'dialogue_id':self.d['id']})
            result=self.finish(job)
        self.assertEqual('error',result['status'])
        self.assertIn('вход',result['error'])
        self.assertEqual([],self.request('/api/state')['runs'])

    def test_dialogue_changed_during_check_is_not_silently_scored(self):
        def mutate(d,kb,progress):
            updated=copy.deepcopy(d);updated['context']+=' changed'
            with server.connect() as db:
                db.execute('UPDATE dialogues SET payload=? WHERE id=?',(core.dump(updated),d['id']))
            return core.reference_report(d,kb)
        with patch.object(server,'run_check',side_effect=mutate):
            job=self.request('/api/check/start',{'dialogue_id':self.d['id']})
            result=self.finish(job)
        self.assertEqual('error',result['status'])
        self.assertEqual([],self.request('/api/state')['runs'])


if __name__=='__main__':unittest.main()
