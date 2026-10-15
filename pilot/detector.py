"""Isolation Forest from reproducible synthetic healthy windows + explicit SLO rules."""
import numpy as np
from sklearn.ensemble import IsolationForest


class Detector:
    def __init__(self):
        rng=np.random.default_rng(42)
        healthy=np.column_stack([rng.normal(80,25,600).clip(5,200),rng.uniform(0,.025,600),rng.uniform(.5,4,600)])
        self.model=IsolationForest(n_estimators=80,contamination=.03,random_state=42).fit(healthy)

    def assess(self,p95,error_rate,rps,samples,reachable=True):
        if not reachable:return {'anomalous':True,'reason':'unreachable','severity':'critical','ml_score':None,'rule_triggered':True}
        if samples<3:return {'anomalous':False,'reason':'warming_up','severity':'info','ml_score':None,'rule_triggered':False}
        value=np.array([[p95,error_rate,rps]])
        score=float(-self.model.decision_function(value)[0])
        rule=error_rate>=.15 or p95>=600
        # ML-only outliers must have some operational impact; traffic change alone is not an incident.
        anomalous=rule or (score>0 and (p95>250 or error_rate>.05))
        reason='errors' if error_rate>=.15 else ('latency' if p95>=600 else ('ml_outlier' if anomalous else 'healthy'))
        return {'anomalous':bool(anomalous),'reason':reason,'severity':'critical' if error_rate>=.5 else 'warning','ml_score':round(score,4),'rule_triggered':rule}
