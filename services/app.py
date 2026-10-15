"""Three independent HTTP demo services, selected by SERVICE_NAME."""
import asyncio
import hmac
import os
import random
import time
from collections import deque
from typing import Literal

import httpx
from fastapi import FastAPI,Header,HTTPException
from pydantic import BaseModel
from prometheus_client import CollectorRegistry,Counter,Histogram,make_asgi_app
from pilot.observability import instrument,trace_id

SERVICES=('orders','payments','inventory')


class Control(BaseModel):
    action: Literal['inject','clear_fault','rollback','reset_runtime']
    fault: Literal['none','latency','errors','bad_release']='none'
    expected_revision: int


def create_service(name=None,transport=None):
    name=name or os.getenv('SERVICE_NAME','orders')
    if name not in SERVICES: raise ValueError('Unknown service')
    app=FastAPI(title='Demo '+name)
    tracer=instrument(app,name)
    state={'service':name,'fault':'none','version':'v1.0.0','previous_version':'v1.0.0','revision':1,'changed_at':time.time(),'generation':1}
    samples=deque(maxlen=3000);logs=deque(maxlen=100)
    registry=CollectorRegistry()
    requests=Counter('demo_requests_total','Business requests',['service','status'],registry=registry)
    latency=Histogram('demo_request_duration_seconds','Business latency',['service'],registry=registry)
    app.mount('/metrics',make_asgi_app(registry))
    app.state.runtime=state;app.state.samples=samples
    token=os.getenv('CONTROL_TOKEN','local-demo-only')
    rng=random.Random(42)

    @app.get('/health')
    def health(): return {'status':'ok',**state}

    @app.get('/telemetry')
    def telemetry(): return {'state':dict(state),'samples':list(samples),'logs':list(logs)}

    @app.post('/admin/control')
    def control(body:Control,x_control_token: str=Header('')):
        if not hmac.compare_digest(token,x_control_token): raise HTTPException(403,'Invalid service control token')
        if body.expected_revision!=state['revision']: raise HTTPException(409,'Service changed; refresh analysis')
        if body.action=='inject':
            state['fault']=body.fault
            if body.fault=='bad_release':
                state['previous_version']=state['version'];state['version']='v2.0.0-demo'
        elif body.action=='rollback':
            if state['version']==state['previous_version']: raise HTTPException(409,'No release to roll back')
            state['version']=state['previous_version'];state['fault']='none'
        elif body.action=='reset_runtime':
            state['generation']+=1;state['fault']='none'
        else: state['fault']='none'
        state['revision']+=1;state['changed_at']=time.time()
        logs.append({'time':time.time(),'level':'INFO','message':f"Control {body.action}: fault={state['fault']}, version={state['version']}", 'trace_id':None})
        return dict(state)

    async def business():
        started=time.perf_counter();at=time.time();error=None;status=200
        with tracer.start_as_current_span(name+'.business'):
            tid=trace_id()
            try:
                await asyncio.sleep(rng.uniform(.015,.045))
                if state['fault']=='latency': await asyncio.sleep(.85)
                if state['fault'] in ('errors','bad_release'):
                    raise HTTPException(503,'simulated_connection_pool_exhausted' if state['fault']=='bad_release' else 'simulated_dependency_failure')
                if name=='orders':
                    async with httpx.AsyncClient(timeout=2.5,transport=transport,trust_env=False) as client:
                        for service,path in [('inventory','reserve'),('payments','charge')]:
                            base=os.getenv(service.upper()+'_URL',f'http://127.0.0.1:{8952 if service=="payments" else 8953}')
                            response=await client.post(base+'/'+path,json={'demo':True})
                            if response.status_code>=400: raise HTTPException(502,'upstream_'+service+'_failed')
                return {'ok':True,'service':name,'version':state['version'],'trace_id':tid,'demo':True}
            except HTTPException as exc:
                status=exc.status_code;error=str(exc.detail);raise
            except httpx.HTTPError:
                status=502;error='upstream_timeout_or_unreachable';raise HTTPException(502,error)
            finally:
                elapsed=(time.perf_counter()-started)*1000
                samples.append({'time':at,'latency_ms':round(elapsed,2),'status':status,'error':error,'trace_id':tid})
                requests.labels(name,str(status)).inc();latency.labels(name).observe(elapsed/1000)
                if error: logs.append({'time':time.time(),'level':'ERROR','message':error,'trace_id':tid})

    path={'orders':'/checkout','payments':'/charge','inventory':'/reserve'}[name]
    app.post(path)(business)
    return app


app=create_service()
