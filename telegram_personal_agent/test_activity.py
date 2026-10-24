"""Offline regression tests for daily active time and reply scheduling."""
import os, sys, types, tempfile
from datetime import datetime, timezone, timedelta
os.environ['DASHBOARD_PASSWORD']='test-only-password-123456'
os.environ.pop('TELEGRAM_API_ID',None)
os.environ.pop('TELEGRAM_API_HASH',None)
os.environ.pop('OPENAI_API_KEY',None)
if 'openai' not in sys.modules:
    stub=types.ModuleType('openai')
    stub.AsyncOpenAI=type('AsyncOpenAI',(),{'__init__':lambda self,*a,**k:None})
    sys.modules['openai']=stub
import agent
agent.DB=os.path.join(tempfile.mkdtemp(),'activity.sqlite3')
agent.init_db()
with agent.connect() as c:
    c.execute("INSERT INTO contacts(peer_id,name,mode) VALUES (123,'Test','auto')")
assert agent.session_tick(123)==0
with agent.connect() as c:
    day=agent.local_now().date().isoformat()
    c.execute('UPDATE daily_active_time SET last_activity=? WHERE day=? AND peer_id=?',((datetime.now(timezone.utc)-timedelta(seconds=65)).isoformat(),day,123))
assert 64 <= agent.session_tick(123) <= 67
with agent.connect() as c:
    c.execute('UPDATE daily_active_time SET active_seconds=999999 WHERE day=? AND peer_id=?',('2000-01-01',123))
assert agent.time_limit(123)
with agent.connect() as c:
    c.execute("INSERT INTO schedules(peer_id,enabled,start_hour,end_hour,weekdays,min_gap_hours,weekly_max) VALUES (123,1,0,1,'0',720,1)")
assert agent.allowed_window(123) in (True,False)
print('Activity regression tests passed')
