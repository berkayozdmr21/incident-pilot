import asyncio
import os
import time
from collections import deque
import numpy as np
import httpx
from sqlalchemy import select,update,delete
from sqlalchemy.exc import IntegrityError
from .store import session,Incident,Snapshot,Job,Audit,audit,incident_dict
from .detector import Detector
from .analysis import build_analysis,narrate

NAMES=['orders','payments','inventory']
PORTS={'orders':8951,'payments':8952,'inventory':8953}


class Engine:
    def __init__(self,transport=None):
        self.urls={name:os.getenv(name.upper()+'_URL',f'http://127.0.0.1:{PORTS[name]}') for name in NAMES}
        self.token=os.getenv('CONTROL_TOKEN','local-demo-only')
        self.detector=Detector();self.services={};self.telemetry={};self.load_enabled=True
        self.tasks=[];self.transport=transport;self.tick_lock=asyncio.Lock();self.started_at=time.time()

    def client(self):return httpx.AsyncClient(timeout=3.5,transport=self.transport,trust_env=False)

    async def fetch_service(self,name):
        try:
            async with self.client() as client:
                result=await client.get(self.urls[name]+'/telemetry');result.raise_for_status();payload=result.json()
            samples=[s for s in payload['samples'] if s['time']>=time.time()-20]
            latencies=[s['latency_ms'] for s in samples]
            errors=sum(s['status']>=400 for s in samples)
            row={'name':name,'reachable':True,'p95_ms':round(float(np.percentile(latencies,95)),1) if latencies else 0,
                 'error_rate':errors/len(samples) if samples else 0,'rps':len(samples)/20,'sample_count':len(samples),
                 'state':payload['state']}
            self.telemetry[name]=payload
        except (httpx.HTTPError,ValueError,KeyError):
            row={'name':name,'reachable':False,'p95_ms':0,'error_rate':1,'rps':0,'sample_count':0,'state':{}}
        row['detection']=self.detector.assess(row['p95_ms'],row['error_rate'],row['rps'],row['sample_count'],row['reachable'])
        row['status']='offline' if not row['reachable'] else ('degraded' if row['detection']['anomalous'] else ('warming_up' if row['sample_count']<3 else 'healthy'))
        return row

    async def collect_once(self):
        async with self.tick_lock:
            rows=await asyncio.gather(*(self.fetch_service(name) for name in NAMES))
            self.services={r['name']:r for r in rows}
            now=time.time()
            with session() as db:
                db.add(Snapshot(time=now,services=rows))
                db.execute(delete(Snapshot).where(Snapshot.time<now-7200))
                for row in rows:
                    # Give freshly started services a short startup grace period.
                    bad=row['detection']['anomalous'] and now-self.started_at>5
                    active=db.execute(select(Incident).where(Incident.active_service==row['name'])).scalar_one_or_none()
                    if bad and active is None:
                        active=Incident(service=row['name'],active_service=row['name'],severity=row['detection']['severity'],title=row['name']+' · '+row['detection']['reason'],evidence=[row])
                        db.add(active);db.flush()
                        audit(db,'incident_opened','Anomali tespit edildi.',active.id,{'reason':row['detection']['reason']})
                        db.add(Job(incident_id=active.id))
                    elif active:
                        if bad or row['sample_count']<3:
                            active.healthy_streak=0
                        else:
                            active.healthy_streak+=1
                            if active.healthy_streak>=3:
                                active.status='resolved';active.resolved_at=now;active.active_service=None;active.revision+=1
                                audit(db,'incident_resolved','Üç ardışık sağlıklı ölçümle doğrulandı.',active.id)
                        if active.status=='mitigating' and not bad:active.status='recovering'
            return rows

    async def send_control(self,service,action,revision,fault='none'):
        async with self.client() as client:
            response=await client.post(self.urls[service]+'/admin/control',headers={'X-Control-Token':self.token},json={'action':action,'fault':fault,'expected_revision':revision})
            if response.status_code==409:raise ValueError('Servis değişmiş. Yeni analiz iste ve tekrar onayla.')
            response.raise_for_status();return response.json()

    async def work_once(self):
        now=time.time()
        with session() as db:
            # A crashed worker's lease can be reclaimed. One worker per control plane is supported.
            db.execute(update(Job).where(Job.status=='running',Job.updated_at<now-60).values(status='queued'))
            job=db.execute(select(Job).where(Job.status=='queued').order_by(Job.created_at).limit(1)).scalar_one_or_none()
            if not job:return False
            if db.execute(update(Job).where(Job.id==job.id,Job.status=='queued').values(status='running',updated_at=now,attempts=Job.attempts+1)).rowcount!=1:return False
            job_id,incident_id=job.id,job.incident_id
        try:
            with session() as db:
                incident=db.get(Incident,incident_id)
                if not incident or incident.status=='resolved':raise ValueError('Olay artık aktif değil.')
                service=incident.service;revision=incident.revision
            metrics=await self.fetch_service(service)
            payload=self.telemetry.get(service,{}) if metrics['reachable'] else {}
            analysis=await narrate(build_analysis(service,payload,metrics))
            with session() as db:
                incident=db.get(Incident,incident_id)
                if incident.status=='resolved' or incident.revision!=revision:raise ValueError('Analiz sırasında olay değişti.')
                incident.analysis=analysis;incident.revision+=1
                job=db.get(Job,job_id);job.status='done';job.updated_at=time.time()
                audit(db,'analysis_completed','Kanıt analizi hazır; müdahale için onay gerekli.',incident_id,{'mode':analysis['mode']})
        except Exception as exc:
            with session() as db:
                job=db.get(Job,job_id);job.status='failed';job.updated_at=time.time();job.error=type(exc).__name__
                audit(db,'analysis_failed','Analiz tamamlanamadı; tekrar istenebilir.',incident_id)
        return True

    async def collect_loop(self):
        while True:
            try:await self.collect_once()
            except asyncio.CancelledError:raise
            except Exception:
                with session() as db:audit(db,'collector_error','Metrik toplama döngüsü hata verdi; yeniden denenecek.')
            await asyncio.sleep(2)

    async def load_loop(self):
        async def checkout():
            try:
                async with self.client() as client:await client.post(self.urls['orders']+'/checkout',json={'demo':True})
            except httpx.HTTPError:pass
        while True:
            if self.load_enabled:await asyncio.gather(*(checkout() for _ in range(3)))
            await asyncio.sleep(2)

    async def worker_loop(self):
        while True:
            try:await self.work_once()
            except asyncio.CancelledError:raise
            except Exception:pass
            await asyncio.sleep(.5)

    def start(self):
        self.tasks=[asyncio.create_task(loop()) for loop in [self.collect_loop,self.load_loop,self.worker_loop]]

    async def stop(self):
        for task in self.tasks:task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)

    def dashboard(self):
        with session() as db:
            incidents=[incident_dict(i) for i in db.execute(select(Incident).order_by(Incident.opened_at.desc()).limit(100)).scalars()]
            snapshots=list(db.execute(select(Snapshot).order_by(Snapshot.time.desc()).limit(60)).scalars())
            events=[{'id':a.id,'time':a.time,'kind':a.kind,'message':a.message,'incident_id':a.incident_id,'details':a.details} for a in db.execute(select(Audit).order_by(Audit.time.desc()).limit(70)).scalars()]
            jobs=[{'id':j.id,'status':j.status,'incident_id':j.incident_id,'attempts':j.attempts,'error':j.error} for j in db.execute(select(Job).order_by(Job.created_at.desc()).limit(30)).scalars()]
        logs=[]
        for name,payload in self.telemetry.items():
            logs.extend({**log,'service':name} for log in payload.get('logs',[])[-15:])
        return {'time':time.time(),'services':list(self.services.values()),'incidents':incidents,'events':events,'jobs':jobs,
                'history':[{'time':s.time,'services':s.services} for s in reversed(snapshots)],
                'logs':sorted(logs,key=lambda x:x['time'],reverse=True)[:40],
                'load_enabled':self.load_enabled,'llm_configured':bool(os.getenv('OPENAI_API_KEY')),
                'window_seconds':20,'healthy_confirmations':3}
