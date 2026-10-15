"""Launch four independent local processes; check readiness before opening UI."""
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
import secrets
from pathlib import Path
from dotenv import load_dotenv

ROOT=Path(os.path.abspath(__file__)).parent


def main():
    os.chdir(ROOT);load_dotenv(ROOT/'.env',override=False)
    if not (ROOT/'frontend/dist/index.html').exists():
        npm=shutil.which('npm.cmd' if os.name=='nt' else 'npm')
        if not npm:raise RuntimeError('Node.js gerekli veya derlenmis ZIP paketini kullanin.')
        subprocess.run([npm,'ci','--no-audit','--no-fund'],cwd=ROOT/'frontend',check=True)
        subprocess.run([npm,'run','build'],cwd=ROOT/'frontend',check=True)
    # Reserve all port candidates together so the selected service ports are distinct.
    sockets=[]
    for _ in range(4):
        sock=socket.socket();sock.bind(('127.0.0.1',0));sockets.append(sock)
    ports=[sock.getsockname()[1] for sock in sockets]
    for sock in sockets:sock.close()
    env=dict(os.environ);env['CONTROL_TOKEN']=secrets.token_urlsafe(32)
    names=['orders','payments','inventory']
    for name,port in zip(names,ports[1:]):env[name.upper()+'_URL']=f'http://127.0.0.1:{port}'
    processes=[];logs=[]
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    (ROOT/'data').mkdir(exist_ok=True)
    try:
        for name,port in zip(names,ports[1:]):
            file=(ROOT/'data'/(name+'.log')).open('w',encoding='utf-8');logs.append(file)
            processes.append(subprocess.Popen([sys.executable,'-m','uvicorn','services.app:app','--host','127.0.0.1','--port',str(port)],env={**env,'SERVICE_NAME':name},stdout=file,stderr=subprocess.STDOUT))
        file=(ROOT/'data/pilot.log').open('w',encoding='utf-8');logs.append(file)
        processes.append(subprocess.Popen([sys.executable,'-m','uvicorn','pilot.app:app','--host','127.0.0.1','--port',str(ports[0])],env=env,stdout=file,stderr=subprocess.STDOUT))
        targets=[f'http://127.0.0.1:{port}'+('/api/health' if i==0 else '/health') for i,port in enumerate(ports)]
        for _ in range(100):
            if any(p.poll() is not None for p in processes):raise RuntimeError('Bir servis kapandi. data/ klasorundeki loglari kontrol edin.')
            ready=True
            for target in targets:
                try:
                    with opener.open(target,timeout=1) as response:ready=ready and response.status==200
                except Exception:ready=False
            if ready:break
            time.sleep(.5)
        else:raise RuntimeError('Servisler zamaninda yanit vermedi. data/ loglarini kontrol edin.')
        url=f'http://127.0.0.1:{ports[0]}'
        print('\nIncident Pilot hazir: '+url,flush=True)
        print('Demo trafik otomatik baslar. Panelde Senaryo baslat ile bir ariza secin.',flush=True)
        print('Bu pencereyi acik tutun. Tum servisleri durdurmak icin Ctrl+C.',flush=True)
        webbrowser.open(url)
        while all(p.poll() is None for p in processes):time.sleep(1)
        raise RuntimeError('Bir servis beklenmedik sekilde kapandi; data/ loglarini inceleyin.')
    finally:
        for process in processes:
            if process.poll() is None:process.terminate()
        for process in processes:
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.kill();process.wait()
        for file in logs:file.close()


if __name__=='__main__':
    try:main()
    except KeyboardInterrupt:pass
    except Exception as exc:print('Baslatma hatasi: '+str(exc),flush=True);sys.exit(1)
