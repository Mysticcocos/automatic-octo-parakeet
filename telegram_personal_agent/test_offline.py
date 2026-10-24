"""Offline API smoke tests; mocks OpenAI import, never contacts Telegram."""
import os,sys,types,tempfile
os.environ['DASHBOARD_PASSWORD']='test-only-password-123456'
os.environ.pop('TELEGRAM_API_ID',None)
os.environ.pop('TELEGRAM_API_HASH',None)
os.environ.pop('OPENAI_API_KEY',None)
if 'openai' not in sys.modules:
    stub=types.ModuleType('openai')
    class AsyncOpenAI:
        def __init__(self,*args,**kwargs): pass
    stub.AsyncOpenAI=AsyncOpenAI
    sys.modules['openai']=stub
import agent
from fastapi.testclient import TestClient
agent.DB=os.path.join(tempfile.mkdtemp(),'test.sqlite3')
agent.init_db()
with TestClient(agent.app) as c:
    auth=('owner','test-only-password-123456')
    assert c.get('/',auth=auth).status_code==200
    r=c.get('/api/state',auth=auth);assert r.status_code==200,r.text
    for k in ('memory_candidates','reviews','versions','outbound','notification_rules'):assert k in r.json()
    with agent.connect() as db:db.execute("INSERT INTO contacts(peer_id,name,username,mode,notes,last_seen) VALUES (1,'Test','','manual','','')")
    agent.add_message(1,'user','Добрый день')
    agent.add_message(1,'assistant','Здравствуйте!')
    assert agent.deterministic_review(1,'Здравствуйте!')
    assert not agent.deterministic_review(1,'Рад вас видеть')
    insights=c.get('/api/contact/1/insights',auth=auth)
    assert insights.status_code==200,insights.text
    assert insights.json()['messages_30d']['incoming']==1
    assert insights.json()['messages_30d']['outgoing']==1
    assert c.get('/api/contact/1/profile',auth=auth).status_code==200
    payload={'humor':40,'depth':80,'warmth':75,'business':60,'initiative':50,'instructions':'Без шуток на сложные темы'}
    assert c.post('/api/contact/1/persona',json=payload,auth=auth).status_code==200
    assert c.get('/api/contact/1/profile',auth=auth).json()['style']['humor']==40
    assert c.post('/api/outbound',json={'peer_id':1,'text':'Здравствуйте!','reason':'Согласовано'},auth=auth).status_code==200
    metrics=c.get('/api/analytics',auth=auth)
    assert metrics.status_code==200
    assert metrics.json()['contacts_trend']
    assert c.post('/api/notifications',json={'rules':{'insult':False}},auth=auth).status_code==200
    assert c.post('/api/settings',json={'persona':agent.DEFAULT_PERSONA,'auto_reply':False,'daily_limit':35,'model':'gpt-4.1-mini'},auth=auth).status_code==200
    assert len(c.get('/api/state',auth=auth).json()['versions'])>=1
    assert c.delete('/api/contact/1/data',auth=auth).status_code==200
    assert c.get('/api/contact/1',auth=auth).status_code==404
print('Offline API smoke tests passed')
