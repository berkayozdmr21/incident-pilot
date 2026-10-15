import asyncio
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch,AsyncMock

Path('data/test-runtime').mkdir(parents=True,exist_ok=True)
os.environ['PILOT_DATA_DIR']=str(Path('data/test-runtime').absolute())
os.environ['CONTROL_TOKEN']='test-only-token'
os.environ.pop('DATABASE_URL',None)
os.environ.pop('OPENAI_API_KEY',None)
for name in ['orders','payments','inventory']:os.environ[name.upper()+'_URL']='http://'+name

import httpx
from sqlalchemy import delete,select
from services.app import create_service
from pilot.app import create_app
from pilot.engine import Engine
from pilot.store import session,Incident,Job,Audit,Snapshot
from pilot.detector import Detector
from pilot.analysis import build_analysis,narrate


class Router(httpx.AsyncBaseTransport):
    def __init__(self):self.apps={}
    async def handle_async_request(self,request):
        if request.url.host not in self.apps:raise httpx.ConnectError('unreachable')
        return await httpx.ASGITransport(app=self.apps[request.url.host]).handle_async_request(request)


class SystemTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        with session() as db:
            for table in [Job,Incident,Audit,Snapshot]:db.execute(delete(table))
        self.router=Router()
        self.router.apps={name:create_service(name,self.router) for name in ['orders','payments','inventory']}
        self.engine=Engine(self.router);self.engine.started_at=time.time()-10
        self.app=create_app(self.engine,background=False)
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://pilot',headers={'X-Pilot-Client':'dashboard'})
        self.services=httpx.AsyncClient(transport=self.router)

    async def asyncTearDown(self):
        await self.client.aclose();await self.services.aclose()

    async def inject(self,name,fault):
        return await self.client.post('/api/scenarios',json={'service':name,'fault':fault,'expected_revision':self.router.apps[name].state.runtime['revision']})

    async def requests(self,name,count=3):
        path={'orders':'checkout','payments':'charge','inventory':'reserve'}[name]
        return await asyncio.gather(*(self.services.post('http://'+name+'/'+path,json={'demo':True}) for _ in range(count)))

    async def incident_for(self,name):
        await self.engine.collect_once()
        while await self.engine.work_once():pass
        dashboard=(await self.client.get('/api/dashboard')).json()
        return next(i for i in dashboard['incidents'] if i['service']==name)

    async def test_real_http_dependency_flow_and_traces(self):
        responses=await self.requests('orders')
        self.assertTrue(all(r.status_code==200 for r in responses))
        await self.engine.collect_once()
        data=(await self.client.get('/api/dashboard')).json()
        self.assertTrue(all(s['sample_count']==3 for s in data['services']))
        self.assertFalse(data['incidents'])
        for app in self.router.apps.values():
            self.assertEqual(len(app.state.samples[0]['trace_id']),32)
        # All three services should retain the same distributed trace for one checkout.
        order_ids={s['trace_id'] for s in self.router.apps['orders'].state.samples}
        self.assertTrue(order_ids & {s['trace_id'] for s in self.router.apps['payments'].state.samples})
        self.assertTrue(order_ids & {s['trace_id'] for s in self.router.apps['inventory'].state.samples})

    async def test_fault_propagates_and_analysis_has_evidence(self):
        self.assertEqual((await self.inject('payments','errors')).status_code,201)
        self.assertTrue(all(r.status_code==502 for r in await self.requests('orders')))
        incident=await self.incident_for('payments')
        self.assertTrue(incident['analysis']['evidence'])
        self.assertEqual(incident['analysis']['plan'][0]['action'],'clear_fault')
        data=(await self.client.get('/api/dashboard')).json()
        self.assertTrue(any(i['service']=='orders' for i in data['incidents']))

    async def test_approved_action_requires_real_recovery(self):
        await self.inject('payments','errors');await self.requests('payments')
        incident=await self.incident_for('payments')
        body={'action':'clear_fault','approved':True,'expected_revision':incident['revision']}
        response=await self.client.post('/api/incidents/'+incident['id']+'/approve',json=body)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.router.apps['payments'].state.runtime['fault'],'none')
        self.assertEqual((await self.client.post('/api/incidents/'+incident['id']+'/approve',json=body)).status_code,409)
        with session() as db:self.assertEqual(db.get(Incident,incident['id']).status,'mitigating')
        # Advance old samples beyond the rolling window without waiting 20 wall-clock seconds.
        for sample in self.router.apps['payments'].state.samples:sample['time']-=21
        await self.requests('payments')
        await self.engine.collect_once();await self.engine.collect_once()
        with session() as db:self.assertNotEqual(db.get(Incident,incident['id']).status,'resolved')
        await self.engine.collect_once()
        with session() as db:
            self.assertEqual(db.get(Incident,incident['id']).status,'resolved')
            kinds=[a.kind for a in db.execute(select(Audit)).scalars()]
            self.assertIn('action_approved',kinds);self.assertIn('action_executed',kinds);self.assertIn('incident_resolved',kinds)

    async def test_rejects_no_approval_and_unplanned_action(self):
        await self.inject('payments','errors');await self.requests('payments')
        incident=await self.incident_for('payments')
        endpoint='/api/incidents/'+incident['id']+'/approve'
        self.assertEqual((await self.client.post(endpoint,json={'action':'clear_fault','approved':False,'expected_revision':incident['revision']})).status_code,422)
        self.assertEqual((await self.client.post(endpoint,json={'action':'rollback','approved':True,'expected_revision':incident['revision']})).status_code,422)
        self.assertEqual(self.router.apps['payments'].state.runtime['fault'],'errors')

    async def test_stale_service_revision_blocks_action(self):
        await self.inject('payments','errors');await self.requests('payments')
        incident=await self.incident_for('payments')
        await self.inject('payments','latency')
        response=await self.client.post('/api/incidents/'+incident['id']+'/approve',json={'action':'clear_fault','approved':True,'expected_revision':incident['revision']})
        self.assertEqual(response.status_code,409)
        self.assertEqual(self.router.apps['payments'].state.runtime['fault'],'latency')

    async def test_bad_release_rollback(self):
        await self.inject('inventory','bad_release');await self.requests('inventory')
        incident=await self.incident_for('inventory')
        self.assertEqual(incident['analysis']['plan'][0]['action'],'rollback')
        response=await self.client.post('/api/incidents/'+incident['id']+'/approve',json={'action':'rollback','approved':True,'expected_revision':incident['revision']})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.router.apps['inventory'].state.runtime['version'],'v1.0.0')

    async def test_latency_scenario_uses_actual_delay(self):
        await self.inject('payments','latency')
        await self.requests('payments')
        rows=await self.engine.collect_once()
        payment=next(s for s in rows if s['name']=='payments')
        self.assertGreater(payment['p95_ms'],800)
        self.assertEqual(payment['detection']['reason'],'latency')

    async def test_service_admin_token_and_revision(self):
        response=await self.services.post('http://payments/admin/control',json={'action':'inject','fault':'errors','expected_revision':1})
        self.assertEqual(response.status_code,403)
        self.assertEqual((await self.client.post('/api/scenarios',json={'service':'payments','fault':'errors','expected_revision':999})).status_code,409)

    async def test_origin_and_custom_header_protection(self):
        body={'enabled':False}
        self.assertEqual((await self.client.post('/api/load',json=body,headers={'Origin':'http://evil.example'})).status_code,403)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://pilot') as other:
            self.assertEqual((await other.post('/api/load',json=body)).status_code,403)
        self.assertEqual((await self.client.post('/api/load',json=body)).status_code,200)
        self.assertFalse(self.engine.load_enabled)

    async def test_queue_dedup_and_expired_lease(self):
        await self.inject('payments','errors');await self.requests('payments');await self.engine.collect_once()
        with session() as db:
            job=db.execute(select(Job)).scalars().first();incident_id=job.incident_id
            job.status='running';job.updated_at=time.time()-61
        self.assertTrue(await self.engine.work_once())
        with session() as db:self.assertEqual(db.get(Job,job.id).status,'done')
        first=await self.client.post('/api/incidents/'+incident_id+'/analyze',json={})
        second=await self.client.post('/api/incidents/'+incident_id+'/analyze',json={})
        self.assertEqual(first.json()['job_id'],second.json()['job_id'])

    async def test_metrics_report_and_snapshot_persistence(self):
        await self.inject('payments','errors');await self.requests('payments')
        incident=await self.incident_for('payments')
        metrics=await self.client.get('/metrics')
        self.assertIn('pilot_service_p95_ms',metrics.text)
        self.assertIn('pilot_open_incidents 1.0',metrics.text)
        report=await self.client.get('/api/incidents/'+incident['id']+'/report')
        self.assertIn('[E1]',report.text)
        self.assertIn('attachment',report.headers['content-disposition'])
        other=Engine(self.router)
        self.assertEqual(len(other.dashboard()['incidents']),1)

    async def test_unknown_service_and_action_rejected(self):
        self.assertEqual((await self.client.post('/api/scenarios',json={'service':'external','fault':'errors','expected_revision':1})).status_code,422)
        self.assertEqual((await self.client.post('/api/incidents/missing/analyze',json={})).status_code,404)


class DetectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.detector=Detector()
    def test_slo_and_warmup(self):
        self.assertFalse(self.detector.assess(80,0,2,10)['anomalous'])
        self.assertFalse(self.detector.assess(900,1,2,1)['anomalous'])
        self.assertTrue(self.detector.assess(900,0,2,10)['anomalous'])
        self.assertTrue(self.detector.assess(80,.5,2,10)['anomalous'])
        self.assertTrue(self.detector.assess(0,0,0,0,False)['anomalous'])
    def test_analysis_does_not_propose_for_unreachable(self):
        result=build_analysis('payments',{}, {'reachable':False,'p95_ms':0,'error_rate':1})
        self.assertEqual(result['plan'],[])


class NarrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_key_keeps_local_plan(self):
        analysis=build_analysis('payments',{'state':{'revision':2}}, {'reachable':True,'p95_ms':800})
        result=await narrate(analysis)
        self.assertEqual(result['mode'],'evidence_rules')
        self.assertEqual(result['plan'],analysis['plan'])

    async def test_llm_references_validated_and_plan_unchanged(self):
        from unittest.mock import Mock
        analysis=build_analysis('payments',{'state':{'revision':2}}, {'reachable':True,'p95_ms':800})
        response=Mock();response.json.return_value={'choices':[{'message':{'content':'Gecikme yüksek [E1].'}}]}
        client=AsyncMock();client.__aenter__.return_value=client;client.post.return_value=response
        with patch.dict(os.environ,{'OPENAI_API_KEY':'fake-test-key'}),patch('pilot.analysis.httpx.AsyncClient',return_value=client):
            result=await narrate(analysis)
            self.assertEqual(result['mode'],'llm_assisted')
            self.assertEqual(result['plan'],analysis['plan'])
            response.json.return_value={'choices':[{'message':{'content':'Uydurma kanıt [E999].'}}]}
            fallback=await narrate(analysis)
            self.assertEqual(fallback['mode'],'evidence_rules')
            self.assertIn('llm_warning',fallback)


if __name__=='__main__':unittest.main()
