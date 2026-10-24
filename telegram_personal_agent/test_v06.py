import os,sys,types,tempfile
stub=types.ModuleType('openai')
class AsyncOpenAI:
    def __init__(self,*args,**kwargs):pass
stub.AsyncOpenAI=AsyncOpenAI
sys.modules['openai']=stub
os.environ.pop('OPENAI_API_KEY',None)
os.environ.pop('TELEGRAM_API_ID',None)
os.environ.pop('TELEGRAM_API_HASH',None)
os.environ['DASHBOARD_PASSWORD']='test-only-password-123456'
import agent
from fastapi.testclient import TestClient
agent.DB=os.path.join(tempfile.mkdtemp(),'v06.sqlite3')
agent.init_db()
with TestClient(agent.app) as c:
    auth=('owner','test-only-password-123456')
    r=c.get('/api/diagnostics',auth=auth)
    assert r.status_code==200, r.text
    assert r.json()['database_ok']
    assert c.get('/api/groups/candidates',auth=auth).status_code==200
    assert c.get('/api/groups/candidates').status_code==401
    assert c.post('/api/groups/scan',json={'group_id':123,'max_messages':150},auth=auth).status_code==503
    assert c.post('/api/groups/candidates/123/decision',json={'status':'invalid'},auth=auth).status_code==400
print('v0.6 offline tests passed')
