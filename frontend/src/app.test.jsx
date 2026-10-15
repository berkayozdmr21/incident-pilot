import React from 'react';
import {render,screen,cleanup,fireEvent,waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {vi,beforeEach,afterEach,test,expect} from 'vitest';
import {App,ApprovalModal,ScenarioModal} from './main.jsx';

let fetchMock;
const dashboard={services:[{name:'payments',reachable:true,p95_ms:80,error_rate:0,sample_count:10,rps:1,status:'healthy',state:{revision:2,version:'v1.0.0'},detection:{reason:'healthy'}}],incidents:[],events:[],history:[],logs:[],jobs:[],load_enabled:true,llm_configured:false};
beforeEach(()=>{
  fetchMock=vi.fn(async url=>({ok:true,status:200,json:async()=>url==='/api/dashboard'?dashboard:{status:'mitigating'}}));
  vi.stubGlobal('fetch',fetchMock);
  class MockWS{constructor(){this.close=vi.fn();} }
  vi.stubGlobal('WebSocket',MockWS);
});
afterEach(()=>{cleanup();vi.unstubAllGlobals();});

const incident={id:'incident-one',revision:4,analysis:{state_revision:2}};
const plan={service:'payments',action:'clear_fault',label:'Demo arızasını kaldır',impact:'Yalnızca demo servisi etkilenir.'};
test('approval requires explicit checkbox and sends reviewed revision',async()=>{
  const user=userEvent.setup(),done=vi.fn();render(<ApprovalModal incident={incident} plan={plan} onClose={()=>{}} onDone={done}/>);
  const button=screen.getByRole('button',{name:'Onayla ve uygula'});
  expect(button.disabled).toBe(true);
  await user.click(screen.getByRole('checkbox'));
  await user.click(button);
  await waitFor(()=>expect(done).toHaveBeenCalled());
  const [url,options]=fetchMock.mock.calls[0];
  expect(url).toBe('/api/incidents/incident-one/approve');
  expect(JSON.parse(options.body)).toEqual({action:'clear_fault',approved:true,expected_revision:4});
  expect(options.headers['X-Pilot-Client']).toBe('dashboard');
});

test('stale approval error stays visible rather than reporting success',async()=>{
  const user=userEvent.setup(),done=vi.fn();fetchMock.mockResolvedValue({ok:false,status:409,json:async()=>({detail:'Olay değişti. Yenile.'})});
  render(<ApprovalModal incident={incident} plan={plan} onClose={()=>{}} onDone={done}/>);
  await user.click(screen.getByRole('checkbox'));await user.click(screen.getByRole('button',{name:'Onayla ve uygula'}));
  await screen.findByText('Olay değişti. Yenile.');expect(done).not.toHaveBeenCalled();
});

test('scenario sends service fault and current revision',async()=>{
  const user=userEvent.setup(),done=vi.fn();render(<ScenarioModal services={dashboard.services} onClose={()=>{}} onDone={done}/>);
  await user.click(screen.getByRole('radio',{name:/Sorunlu sürüm/}));
  await user.click(screen.getByRole('button',{name:'Senaryoyu başlat'}));
  await waitFor(()=>expect(done).toHaveBeenCalled());
  expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({service:'payments',fault:'bad_release',expected_revision:2});
});

test('scenario cannot run when target is unreachable',()=>{
  render(<ScenarioModal services={[]} onClose={()=>{}} onDone={()=>{}}/>);
  expect(screen.getByRole('button',{name:'Senaryoyu başlat'}).disabled).toBe(true);
});

test('dashboard renders real service response and no invented incident',async()=>{
  render(<App/>);await screen.findByText('Şimdilik sakin.');
  expect(screen.getAllByText('payments').length).toBeGreaterThan(0);
  expect(screen.getAllByText('80 ms').length).toBeGreaterThan(0);
  expect(screen.getByRole('button',{name:'Senaryo başlat'})).toBeTruthy();
});

test('Escape closes scenario modal',async()=>{
  const user=userEvent.setup(),close=vi.fn();render(<ScenarioModal services={dashboard.services} onClose={close} onDone={()=>{}}/>);
  await user.keyboard('{Escape}');expect(close).toHaveBeenCalled();
});
