"""Offline smoke checks; requires pip install -r requirements.txt."""
import os
os.environ['DASHBOARD_PASSWORD']='test-only-password-123456'
import agent
from fastapi.testclient import TestClient
agent.init_db()
with TestClient(agent.app) as c:
    auth=('owner','test-only-password-123456')
    assert c.get('/',auth=auth).status_code==200
    state=c.get('/api/state',auth=auth)
    assert state.status_code==200, state.text
    assert 'reports' in state.json() and 'learning' in state.json()
    payload={'owner_timezone':'Europe/Kyiv','report_hour':21,'notifications_enabled':False,'humor':65,'depth':80,'warmth':75,'business':60,'initiative':60}
    assert c.post('/api/advanced',json=payload,auth=auth).status_code==200
    assert c.get('/api/health').status_code==200
print('Smoke tests passed')
