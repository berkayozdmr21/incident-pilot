"""Synthetic detector evaluation. Not production telemetry or an ML benchmark."""
import json
import os
import sys
from pathlib import Path
import numpy as np
from sklearn.metrics import classification_report,confusion_matrix
sys.path.insert(0,str(Path(os.path.abspath(__file__)).parents[1]))
from pilot.detector import Detector

if __name__=='__main__':
    rng=np.random.default_rng(2026);detector=Detector()
    healthy=[(float(rng.uniform(40,160)),float(rng.uniform(0,.025)),float(rng.uniform(.6,3))) for _ in range(100)]
    faulty=[(float(rng.uniform(650,1500)),0,2) for _ in range(40)]+[(80,float(rng.uniform(.2,.9)),2) for _ in range(40)]+[(float(rng.uniform(270,500)),.07,2) for _ in range(20)]
    data=healthy+faulty;actual=[0]*100+[1]*100
    results=[detector.assess(p,e,r,10) for p,e,r in data]
    combined=[int(r['anomalous']) for r in results]
    rules=[int(r['rule_triggered']) for r in results]
    report={'note':'200 synthetic evaluation windows, generated separately from synthetic healthy training. Fault definitions overlap with configured rules, so results are optimistic by construction.',
            'combined':classification_report(actual,combined,output_dict=True,zero_division=0),
            'rules_only':classification_report(actual,rules,output_dict=True,zero_division=0),
            'confusion_matrix':confusion_matrix(actual,combined).tolist()}
    destination=Path('ml/evaluation_report.json');destination.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({'combined_f1':report['combined']['1']['f1-score'],'rules_f1':report['rules_only']['1']['f1-score'],'note':report['note']},indent=2))
