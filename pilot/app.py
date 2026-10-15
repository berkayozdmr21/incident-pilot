import asyncio
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI,HTTPException,Request,WebSocket,WebSocketDisconnect
from fastapi.responses import JSONResponse,Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select,update
from prometheus_client import CollectorRegistry,Gauge,generate_latest,CONTENT_TYPE_LATEST

from .engine import Engine,NAMES
from .store import init_db,session,Incident,Job,audit,incident_dict


class Scenario(BaseModel):
    service: Literal['orders','payments','inventory']
    fault: Literal['latency','errors','bad_release']
    expected_revision: int


class Approval(BaseModel):
    action: Literal['clear_fault','rollback','reset_runtime']
    approved: bool
    expected_revision: int


class LoadSetting(BaseModel):
    enabled: bool


def create_app(runtime=None,background=True):
    runtime=runtime or Engine()
    init_db()
    @asynccontextmanager
    async def lifespan(app):
        if background:runtime.start()
        yield
        if background:await runtime.stop()
    app=FastAPI(title='Incident Pilot',version='1.0.0',lifespan=lifespan)
    app.state.engine=runtime

    @app.middleware('http')
    async def same_origin(request:Request,call_next):
        if request.method in ['POST','PUT','PATCH','DELETE'] and request.url.path.startswith('/api/'):
            origin=request.headers.get('origin')
            if origin and urlparse(origin).netloc!=request.headers.get('host'):
                return JSONResponse({'detail':'Cross-origin mutations are blocked.'},status_code=403)
            if request.headers.get('x-pilot-client')!='dashboard':
                return JSONResponse({'detail':'X-Pilot-Client: dashboard header required.'},status_code=403)
        return await call_next(request)

    @app.get('/api/health')
    def health():return {'status':'ok','mode':'local-demo','database':'postgresql' if os.getenv('DATABASE_URL','').startswith('postgres') else 'sqlite'}

    @app.get('/api/dashboard')
    def dashboard():return runtime.dashboard()

    @app.post('/api/load')
    def set_load(body:LoadSetting):
        runtime.load_enabled=body.enabled
        with session() as db:audit(db,'load_changed','Demo trafiği '+('başlatıldı.' if body.enabled else 'durduruldu.'))
        return {'enabled':body.enabled}

    @app.post('/api/scenarios',status_code=201)
    async def inject(body:Scenario):
        try:state=await runtime.send_control(body.service,'inject',body.expected_revision,body.fault)
        except ValueError as exc:raise HTTPException(409,str(exc))
        except httpx.HTTPError:raise HTTPException(502,'Demo servisine erişilemedi.')
        with session() as db:audit(db,'fault_injected',f'{body.service}: {body.fault} senaryosu başlatıldı.',details={'service':body.service,'fault':body.fault,'state_revision':state['revision']})
        return state

    @app.post('/api/incidents/{incident_id}/analyze',status_code=202)
    def analyze(incident_id:str):
        with session() as db:
            item=db.get(Incident,incident_id)
            if not item:raise HTTPException(404,'Olay bulunamadı.')
            if item.status not in ['open']:raise HTTPException(409,'Yalnızca açık olaylar analiz edilebilir.')
            existing=db.execute(select(Job).where(Job.incident_id==incident_id,Job.status.in_(['queued','running']))).scalars().first()
            if existing:return {'job_id':existing.id,'existing':True}
            job=Job(incident_id=incident_id);db.add(job);db.flush()
            audit(db,'analysis_queued','Yeni kanıt analizi kuyruğa alındı.',incident_id)
            return {'job_id':job.id}

    @app.post('/api/incidents/{incident_id}/approve')
    async def approve(incident_id:str,body:Approval):
        if not body.approved:raise HTTPException(422,'Müdahale açık onay gerektirir.')
        with session() as db:
            item=db.get(Incident,incident_id)
            if not item:raise HTTPException(404,'Olay bulunamadı.')
            if item.status!='open' or item.revision!=body.expected_revision:raise HTTPException(409,'Olay değişti veya müdahale zaten başlatıldı. Yenile.')
            analysis=item.analysis or {}
            if body.action not in [p['action'] for p in analysis.get('plan',[])]:raise HTTPException(422,'İşlem, gözden geçirilmiş izinli planda yok.')
            service=item.service;state_revision=analysis.get('state_revision')
            if state_revision is None:raise HTTPException(409,'Güncel servis durumu bilinmiyor; yeni analiz iste.')
            # Optimistic claim prevents duplicate/replayed approvals from executing twice.
            claimed=db.execute(update(Incident).where(Incident.id==incident_id,Incident.status=='open',Incident.revision==body.expected_revision).values(status='mitigating',revision=Incident.revision+1)).rowcount
            if claimed!=1:raise HTTPException(409,'Başka bir müdahale bu olayı değiştirdi.')
            audit(db,'action_approved','Yerel operatör müdahaleyi onayladı.',incident_id,{'action':body.action,'service':service,'expected_state_revision':state_revision})
        try:
            state=await runtime.send_control(service,body.action,state_revision)
        except (ValueError,httpx.HTTPError) as exc:
            with session() as db:
                item=db.get(Incident,incident_id);item.status='open';item.revision+=1
                audit(db,'action_failed','İşlem uygulanamadı; güncel analiz gerekli.',incident_id,{'error':type(exc).__name__})
            raise HTTPException(409 if isinstance(exc,ValueError) else 502,'Müdahale uygulanamadı. Yeni analiz iste.')
        with session() as db:
            audit(db,'action_executed','Demo servis durumu değiştirildi; sağlıklı ölçümler bekleniyor.',incident_id,{'action':body.action,'new_state_revision':state['revision']})
        return {'status':'mitigating','verification':'20 saniyelik pencere ve 3 ardışık sağlıklı ölçüm bekleniyor.','state':state}

    @app.get('/api/incidents/{incident_id}/report')
    def report(incident_id:str):
        with session() as db:
            item=db.get(Incident,incident_id)
            if not item:raise HTTPException(404,'Olay bulunamadı.')
            item=incident_dict(item)
        analysis=item['analysis'] or {}
        lines=['# Incident Pilot · Olay raporu','',f"Servis: {item['service']}",f"Durum: {item['status']}",f"Olay: {item['id']}",'',analysis.get('hypothesis','Analiz henüz tamamlanmadı.'),'','## Kanıtlar']
        lines.extend(f"- [{e['id']}] {e['text']}" for e in analysis.get('evidence',[]))
        lines+=['','Demo sisteminden üretilmiştir. Kök neden kesinliği veya üretim güvenilirliği iddiası değildir.']
        return Response('\n'.join(lines),media_type='text/markdown',headers={'Content-Disposition':f'attachment; filename="incident-{incident_id}.md"'})

    @app.websocket('/api/live')
    async def live(ws:WebSocket):
        origin=ws.headers.get('origin')
        if origin and urlparse(origin).netloc!=ws.headers.get('host'):
            await ws.close(code=1008);return
        await ws.accept()
        try:
            while True:
                await ws.send_json(runtime.dashboard());await asyncio.sleep(2)
        except (WebSocketDisconnect,RuntimeError):pass

    registry=CollectorRegistry()
    p95=Gauge('pilot_service_p95_ms','Rolling 20 second p95 latency',['service'],registry=registry)
    errors=Gauge('pilot_service_error_ratio','Rolling error ratio',['service'],registry=registry)
    active=Gauge('pilot_open_incidents','Active incidents',registry=registry)
    @app.get('/metrics')
    def metrics():
        for name,row in runtime.services.items():
            p95.labels(name).set(row['p95_ms']);errors.labels(name).set(row['error_rate'])
        with session() as db:active.set(len(db.execute(select(Incident).where(Incident.active_service.is_not(None))).scalars().all()))
        return Response(generate_latest(registry),media_type=CONTENT_TYPE_LATEST)

    root=Path(os.path.abspath(__file__)).parents[1]/'frontend/dist'
    if root.exists():app.mount('/',StaticFiles(directory=root,html=True),name='frontend')
    else:
        @app.get('/')
        def frontend_missing():return {'message':'Frontend build gerekli. BASLAT.cmd kullanın veya frontend içinde npm ci && npm run build çalıştırın.'}
    return app


app=create_app()
