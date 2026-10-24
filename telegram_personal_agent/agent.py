"""Personal Telegram AI agent: single owner, no operator functionality."""
import os, json, sqlite3, asyncio, secrets, logging, re
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from openai import AsyncOpenAI

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')
DB = ROOT / 'personal_agent.sqlite3'
logging.basicConfig(level=logging.INFO)
log = logging.getLogger('personal-agent')

DEFAULT_PERSONA = '''Ты — персональный ИИ-помощник владельца, ведущий диалог в его Telegram по его поручению.
Стиль: зрелый, интеллигентный, остроумный, начитанный, деликатный; деловой, когда уместно, живой в бытовых разговорах.
Пиши естественно, не используй заученные шаблоны, не задавай подряд много вопросов.
Подстраивай тон под контекст и стиль собеседника. Не изображай реальные биографические факты владельца, которых не знаешь.
Не выдавай себя за человека, если тебя прямо спрашивают об автоматизации; честно объясни, что ты ИИ-помощник.
Не обещай доход, сделки, переводы, юридические обязательства и иные действия от имени владельца.
Уважай отказ, границы и личные данные; не дави на собеседника. Не придумывай факты.
Отвечай на языке собеседника. Обычно кратко (1–4 предложения), подробнее только по запросу.'''


def connect():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    return con


def init_db():
    with connect() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS contacts (peer_id INTEGER PRIMARY KEY, name TEXT NOT NULL DEFAULT '', username TEXT NOT NULL DEFAULT '', mode TEXT NOT NULL DEFAULT 'auto', notes TEXT NOT NULL DEFAULT '', last_seen TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, peer_id INTEGER NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL, created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, peer_id INTEGER, category TEXT NOT NULL, detail TEXT NOT NULL, created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS drafts (id INTEGER PRIMARY KEY AUTOINCREMENT, peer_id INTEGER NOT NULL, text TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created TEXT NOT NULL);
        ''')
        c.executescript("""
        CREATE TABLE IF NOT EXISTS memories (id INTEGER PRIMARY KEY AUTOINCREMENT,peer_id INTEGER NOT NULL,fact TEXT NOT NULL,source TEXT NOT NULL DEFAULT 'owner',status TEXT NOT NULL DEFAULT 'pending',created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS learning (id INTEGER PRIMARY KEY AUTOINCREMENT,proposal TEXT NOT NULL,reason TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS schedules (peer_id INTEGER PRIMARY KEY,enabled INTEGER NOT NULL DEFAULT 0,start_hour INTEGER NOT NULL DEFAULT 10,end_hour INTEGER NOT NULL DEFAULT 20,weekdays TEXT NOT NULL DEFAULT '0,1,2,3,4,5,6',min_gap_hours INTEGER NOT NULL DEFAULT 24,weekly_max INTEGER NOT NULL DEFAULT 2);
        CREATE TABLE IF NOT EXISTS reports (id INTEGER PRIMARY KEY AUTOINCREMENT,day TEXT UNIQUE NOT NULL,body TEXT NOT NULL,created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS owner_actions (id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL,detail TEXT NOT NULL,created TEXT NOT NULL);
        """)
        c.executescript("""
        CREATE TABLE IF NOT EXISTS contact_profiles (peer_id INTEGER PRIMARY KEY, profile TEXT NOT NULL DEFAULT '{}', updated TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS contact_personas (peer_id INTEGER PRIMARY KEY, humor INTEGER, depth INTEGER, warmth INTEGER, business INTEGER, initiative INTEGER, instructions TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS memory_candidates (id INTEGER PRIMARY KEY AUTOINCREMENT,peer_id INTEGER NOT NULL,fact TEXT NOT NULL,reason TEXT NOT NULL,source TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS persona_versions (id INTEGER PRIMARY KEY AUTOINCREMENT,persona TEXT NOT NULL,humor INTEGER NOT NULL,depth INTEGER NOT NULL,warmth INTEGER NOT NULL,business INTEGER NOT NULL,initiative INTEGER NOT NULL,reason TEXT NOT NULL,created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS review_log (id INTEGER PRIMARY KEY AUTOINCREMENT,peer_id INTEGER,original TEXT NOT NULL,final TEXT NOT NULL,verdict TEXT NOT NULL,notes TEXT NOT NULL,created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS outbound_requests (id INTEGER PRIMARY KEY AUTOINCREMENT,peer_id INTEGER NOT NULL,text TEXT NOT NULL,reason TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS notification_rules (category TEXT PRIMARY KEY,enabled INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS conversation_sessions (peer_id INTEGER PRIMARY KEY, started TEXT NOT NULL, last_activity TEXT NOT NULL, active_seconds INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS profile_history (id INTEGER PRIMARY KEY AUTOINCREMENT,peer_id INTEGER NOT NULL,profile TEXT NOT NULL,created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS daily_contact_metrics (day TEXT NOT NULL,peer_id INTEGER NOT NULL,incoming INTEGER NOT NULL DEFAULT 0,outgoing INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(day,peer_id));
        CREATE TABLE IF NOT EXISTS daily_active_time (day TEXT NOT NULL,peer_id INTEGER NOT NULL,active_seconds INTEGER NOT NULL DEFAULT 0,last_activity TEXT NOT NULL,PRIMARY KEY(day,peer_id));
        """)
        c.executescript('''
        CREATE TABLE IF NOT EXISTS group_candidates (
          peer_id INTEGER PRIMARY KEY, group_id INTEGER NOT NULL, group_title TEXT NOT NULL,
          display_name TEXT NOT NULL, username TEXT NOT NULL DEFAULT '', message_count INTEGER NOT NULL DEFAULT 0,
          last_message TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'review', discovered TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS outbound_sends (
          peer_id INTEGER PRIMARY KEY, last_sent TEXT NOT NULL, sent_count INTEGER NOT NULL DEFAULT 0);
        ''')
        try:c.execute("ALTER TABLE learning ADD COLUMN instruction TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:pass
        for cat in ('new_contact','approval','error','insult','critical','daily_report','control'):
            c.execute('INSERT OR IGNORE INTO notification_rules(category,enabled) VALUES (?,1)',(cat,))
        for k,v in [('owner_timezone' ,'Europe/Kyiv'),('report_hour','21'),('notifications_enabled','false'),('auto_memory','false'),('humor','65'),('depth','80'),('warmth','75'),('business','60'),('initiative','60'),('reply_quality','maximum'),('memory_analysis','true'),('quality_review','true'),('conversation_analysis','true'),('persona',DEFAULT_PERSONA),('auto_reply','false'),('daily_limit','35'),('model',os.getenv('OPENAI_MODEL','gpt-4.1-mini'))]:
            c.execute('INSERT OR IGNORE INTO settings VALUES (?,?)',(k,v))
        if os.getenv('AUTO_REPLY','false').lower()=='true':
            c.execute("UPDATE settings SET value='true' WHERE key='auto_reply'")


def setting(key):
    with connect() as c:
        row=c.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
        return row['value'] if row else ''


def event(peer, category, detail):
    with connect() as c:
        c.execute('INSERT INTO events(peer_id,category,detail,created) VALUES (?,?,?,?)',(peer,category,detail,now()))
    with connect() as c:
        rule=c.execute('SELECT enabled FROM notification_rules WHERE category=?',(category,)).fetchone()
    if rule and rule['enabled']:
        try: asyncio.get_running_loop().create_task(notify_owner(f'[{category}] {detail}'+(f' (контакт {peer})' if peer else '')))
        except RuntimeError: pass

async def notify_owner(text):
    if setting('notifications_enabled')!='true': return
    token=os.getenv('NOTIFY_BOT_TOKEN',''); chat=os.getenv('NOTIFY_CHAT_ID','')
    if not token or not chat: return
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as h:
            r=await h.post(f'https://api.telegram.org/bot{token}/sendMessage',json={'chat_id':chat,'text':text[:3900]})
            r.raise_for_status()
    except Exception: log.exception('Owner notification failed')

def local_now():
    try: return datetime.now(ZoneInfo(setting('owner_timezone') or 'Europe/Kyiv'))
    except Exception: return datetime.now(timezone.utc)

def contact_memory(peer):
    with connect() as c:
        rows=c.execute("SELECT fact FROM memories WHERE peer_id=? AND status='approved' ORDER BY id DESC LIMIT 30",(peer,)).fetchall()
    return '\n'.join('- '+r['fact'] for r in rows)

def allowed_window(peer):
    with connect() as c:
        row=c.execute('SELECT * FROM schedules WHERE peer_id=?',(peer,)).fetchone()
        if not row or not row['enabled']: return True
        t=local_now()
        if str(t.weekday()) not in row['weekdays'].split(',') or not (row['start_hour'] <= t.hour < row['end_hour']): return False
        last=c.execute("SELECT created FROM messages WHERE peer_id=? AND role='assistant' ORDER BY id DESC LIMIT 1",(peer,)).fetchone()
        if last and (datetime.now(timezone.utc)-datetime.fromisoformat(last['created'])).total_seconds()<row['min_gap_hours']*3600: return False
        week=(datetime.now(timezone.utc)-timedelta(days=7)).isoformat()
        n=c.execute("SELECT COUNT(*) n FROM messages WHERE peer_id=? AND role='assistant' AND created>=?",(peer,week)).fetchone()['n']
        return n<row['weekly_max']

def is_insult(txt):
    terms=('идиот','дебил','тупица','ничтожество','fuck you','moron','кретин')
    return any(t in txt.lower() for t in terms)

def tier_limit(peer):
    with connect() as c:
        r=c.execute('SELECT MIN(created) first FROM messages WHERE peer_id=?',(peer,)).fetchone()
    if not r or not r['first']: return 12
    age=(datetime.now(timezone.utc).date()-datetime.fromisoformat(r['first']).date()).days
    return [12,18,25,35,35,35,35,35][age] if age<8 else 50



def now(): return datetime.now(timezone.utc).isoformat(timespec='seconds')


def history(peer, limit=18):
    with connect() as c:
        rows=c.execute('SELECT role,text FROM messages WHERE peer_id=? ORDER BY id DESC LIMIT ?',(peer,limit)).fetchall()
    return [dict(role=r['role'],content=r['text']) for r in reversed(rows)]


def add_message(peer, role, txt):
    with connect() as c:
        c.execute('INSERT INTO messages(peer_id,role,text,created) VALUES (?,?,?,?)',(peer,role,txt,now()))
        if role in ('user','assistant'):
            col='incoming' if role=='user' else 'outgoing'
            c.execute(f'''INSERT INTO daily_contact_metrics(day,peer_id,{col}) VALUES (?,?,1)
              ON CONFLICT(day,peer_id) DO UPDATE SET {col}={col}+1''',(now()[:10],peer))


def deterministic_review(peer, draft):
    """Fast safety/quality preflight; never substitutes for LLM review."""
    problems=[]
    if is_sensitive(draft): problems.append('Возможное финансовое обязательство или чувствительные сведения')
    if len(draft)>1800: problems.append('Слишком длинный ответ')
    normalized=' '.join(draft.lower().split())
    recent=[m['content'] for m in history(peer,12) if m['role']=='assistant']
    if normalized and any(' '.join(t.lower().split())==normalized for t in recent):
        problems.append('Точное повторение предыдущего ответа')
    if draft.count('?')>=4: problems.append('Чрезмерное количество вопросов')
    if re.search(r'\b(?:я точно знаю|гарантирую|100% гарант|без риска)\b',draft.lower()):
        problems.append('Категоричное неподтверждённое обещание')
    return problems



def is_sensitive(txt):
    # Conservative review gate: configurable future classifier could replace keyword check.
    words=('переведи деньги','отправь деньги','гарантирую доход','гарантия дохода','инвестируй','вложи деньги','паспорт','пароль','код подтверждения','договор подпиши','transfer funds','guaranteed return','send money','password','verification code')
    return any(w in txt.lower() for w in words)


async def json_ai(messages, max_tokens=950):
    if not ai: return None
    response=await ai.chat.completions.create(model=setting('model'),messages=messages,temperature=0.25,max_tokens=max_tokens,response_format={'type':'json_object'})
    raw=response.choices[0].message.content or '{}'
    return json.loads(raw)

def peer_style(peer):
    keys=('humor','depth','warmth','business','initiative')
    with connect() as c:
        row=c.execute('SELECT * FROM contact_personas WHERE peer_id=?',(peer,)).fetchone()
    return {k:(row[k] if row and row[k] is not None else int(setting(k) or '50')) for k in keys} | {'instructions':row['instructions'] if row else ''}

def contact_profile(peer):
    with connect() as c:
        row=c.execute('SELECT profile FROM contact_profiles WHERE peer_id=?',(peer,)).fetchone()
    return json.loads(row['profile']) if row else {}

def log_review(peer,original,final,verdict,notes):
    with connect() as c: c.execute('INSERT INTO review_log(peer_id,original,final,verdict,notes,created) VALUES (?,?,?,?,?,?)',(peer,original,final,verdict,notes,now()))

async def analyze_contact(peer):
    if setting('conversation_analysis')!='true' or not ai: return
    msgs=history(peer,24)
    if len(msgs)<4:return
    try:
        result=await json_ai([{'role':'system','content':'''Analyze Telegram conversation. Return ONLY JSON with keys summary (string), interests (array of strings), preferred_style (string), engagement (low/medium/high), boundaries (array), observations (array), memory_candidates (array of objects {fact,reason}). No sensitive traits, no private secrets, no psychological diagnosis, no unsupported claims. Only extract stable facts explicitly volunteered by the contact. Distinguish tentative inference. Max 4 memory candidates.'''} ,{'role':'user','content':json.dumps(msgs,ensure_ascii=False)}],1100)
        if not isinstance(result,dict):return
        profile={k:result.get(k) for k in ('summary','interests','preferred_style','engagement','boundaries','observations')}
        # Store history for explainable change tracking; do not silently promote hypotheses to facts.
        with connect() as c:
            c.execute('INSERT INTO profile_history(peer_id,profile,created) VALUES (?,?,?)',
                      (peer,json.dumps(profile,ensure_ascii=False),now()))
            c.execute('INSERT INTO contact_profiles(peer_id,profile,updated) VALUES (?,?,?) ON CONFLICT(peer_id) DO UPDATE SET profile=excluded.profile,updated=excluded.updated',(peer,json.dumps(profile,ensure_ascii=False),now()))
            for item in (result.get('memory_candidates') or [])[:4]:
                if not isinstance(item,dict):continue
                fact=str(item.get('fact','')).strip()[:400]
                if not fact:continue
                if not c.execute('SELECT 1 FROM memory_candidates WHERE peer_id=? AND fact=?',(peer,fact)).fetchone():
                    c.execute('INSERT INTO memory_candidates(peer_id,fact,reason,source,created) VALUES (?,?,?,?,?)',(peer,fact,str(item.get('reason',''))[:300],'dialogue',now()))
        event(peer,'analysis','Профиль диалога обновлён; новые факты требуют проверки владельцем')
    except Exception: log.exception('Contact analysis failed')

async def quality_check(peer,draft):
    deterministic_flags=deterministic_review(peer,draft)
    if deterministic_flags:
        log_review(peer,draft,draft,'escalate','; '.join(deterministic_flags))
        with connect() as c:
            c.execute('INSERT INTO drafts(peer_id,text,reason,created) VALUES (?,?,?,?)',
                      (peer,draft,'Автоматическая проверка: '+'; '.join(deterministic_flags),now()))
        event(peer,'approval','Ответ остановлен предварительной проверкой качества')
        return None
    if setting('quality_review')!='true':return draft
    if not ai:
        event(peer,'error','Проверка качества недоступна; ответ остановлен')
        return None
    try:
        data=await json_ai([{'role':'system','content':'''You are a strict pre-send reviewer for a Telegram assistant. Output JSON keys verdict (send/revise/escalate), revised_text (string), notes (string). Escalate for financial promises, legal commitments, private data, coercion, serious conflict, requests for verification codes, unsupported facts. Check naturalness, repetition, context, humor appropriateness, transparency, contact boundaries, and whether the message respects silence. Never invent facts. Preserve the original meaning if revising. If uncertain, escalate.'''} ,{'role':'user','content':json.dumps({'recent_dialogue':history(peer,10),'draft':draft},ensure_ascii=False)}],850)
        verdict=data.get('verdict','escalate')
        final=(data.get('revised_text') or '').strip() if verdict=='revise' else draft
        if verdict not in ('send','revise','escalate') or not final:verdict='escalate'
        log_review(peer,draft,final,verdict,str(data.get('notes',''))[:700])
        if verdict=='escalate':
            with connect() as c: c.execute('INSERT INTO drafts(peer_id,text,reason,created) VALUES (?,?,?,?)',(peer,draft,'Контроль качества: '+str(data.get('notes',''))[:400],now()))
            event(peer,'approval','Ответ отправлен на проверку владельцу')
            return None
        return final
    except Exception:
        log.exception('Quality review failed')
        event(peer,'error','Контроль качества недоступен; ответ не отправлен')
        return None

async def propose_personality_improvement(day):
    if not ai:return
    with connect() as c:
        reviews=[dict(r) for r in c.execute('SELECT verdict,notes FROM review_log ORDER BY id DESC LIMIT 35')]
        rows=[dict(r) for r in c.execute('SELECT category,detail FROM events ORDER BY id DESC LIMIT 45')]
    if len(reviews)<3:return
    try:
        data=await json_ai([{'role':'system','content':'''You audit an assistant's communication quality based on anonymized quality review notes and event summaries. Return JSON: proposal (short Russian title), reason (specific Russian evidence), instruction (one concise safe Russian system instruction to improve style; <=300 chars). Avoid invented claims, sensitive inferences, deceptive behavior, or unapproved commitments. Do not change any setting yourself.'''} ,{'role':'user','content':json.dumps({'reviews':reviews,'events':rows},ensure_ascii=False)[:12000]}],650)
        proposal=str(data.get('proposal','')).strip()[:220]
        reason=str(data.get('reason','')).strip()[:900]
        instruction=str(data.get('instruction','')).strip()[:300]
        if not all((proposal,reason,instruction)):return
        with connect() as c:c.execute('INSERT INTO learning(proposal,reason,instruction,created) VALUES (?,?,?,?)',(proposal,reason,instruction,now()))
    except Exception:log.exception('Daily personality audit failed')

def session_tick(peer):
    """Approximate active dialogue time, isolated per owner-local calendar day.

    Gaps of 5+ minutes are inactivity. Short gaps count up to 120 seconds;
    this is an estimate, not an exact measurement of human attention.
    """
    t=datetime.now(timezone.utc)
    day=local_now().date().isoformat()
    with connect() as c:
        row=c.execute('SELECT * FROM daily_active_time WHERE day=? AND peer_id=?',(day,peer)).fetchone()
        if not row:
            c.execute('INSERT INTO daily_active_time(day,peer_id,active_seconds,last_activity) VALUES (?,?,0,?)',(day,peer,now()))
            return 0
        gap=max(0,(t-datetime.fromisoformat(row['last_activity'])).total_seconds())
        extra=min(120,int(gap)) if gap<300 else 0
        total=row['active_seconds']+extra
        c.execute('UPDATE daily_active_time SET active_seconds=?,last_activity=? WHERE day=? AND peer_id=?',(total,now(),day,peer))
        return total

def time_limit(peer):
    with connect() as c:
        row=c.execute('SELECT MIN(created) first FROM messages WHERE peer_id=?',(peer,)).fetchone()
        session=c.execute('SELECT active_seconds FROM daily_active_time WHERE day=? AND peer_id=?',(local_now().date().isoformat(),peer)).fetchone()
    age=max(0,(local_now().date()-datetime.fromisoformat(row['first']).date()).days) if row and row['first'] else 0
    minutes=[10,15,20,30,30,30,30,30][age] if age<8 else 45
    return (session['active_seconds'] if session else 0)<minutes*60

client = None
ai = None
connected = False
locks = {}

async def reply_text(peer, incoming):
    with connect() as c:
        row=c.execute('SELECT notes,name FROM contacts WHERE peer_id=?',(peer,)).fetchone()
        notes=(row['notes'] if row else '')
        count=c.execute("SELECT created FROM messages WHERE peer_id=? AND role='assistant' AND created>=?",(peer,(datetime.now(timezone.utc)-timedelta(days=2)).isoformat())).fetchall()
        count=sum(datetime.fromisoformat(r['created']).astimezone(local_now().tzinfo).date()==local_now().date() for r in count)
    if count >= min(int(setting('daily_limit') or '35'),tier_limit(peer)) or not time_limit(peer):
        event(peer,'limit','Достигнут дневной лимит исходящих сообщений')
        return
    if is_sensitive(incoming):
        with connect() as c:
            c.execute("INSERT INTO drafts(peer_id,text,reason,created) VALUES (?,?,?,?)",(peer,'','Потенциально чувствительный запрос: требуется решение владельца',now()))
        event(peer,'approval','Чувствительная тема: агент остановлен до ручного решения')
        with connect() as c: c.execute("UPDATE contacts SET mode='manual' WHERE peer_id=?",(peer,))
        return
    # Per-contact outreach schedules govern proactive messages, not replies
    # to an inbound message. Never block an ordinary reply using outreach quotas.
    if not ai:
        event(peer,'error','Нет OPENAI_API_KEY — автоматический ответ недоступен')
        return
    try:
        style=peer_style(peer)
        profile=contact_profile(peer)
        response=await ai.chat.completions.create(
            model=setting('model'),
            messages=[{'role':'system','content':setting('persona')+'\nНастройки характера (0-100): юмор '+str(style['humor'])+', интеллектуальная глубина '+str(style['depth'])+', теплота '+str(style['warmth'])+', деловой тон '+str(style['business'])+', инициатива '+str(style['initiative'])+'. Это относительные ориентиры, не требование делать каждый ответ одинаковым.'+'\nДополнительные проверенные владельцем заметки о контакте: '+notes+'\nПодтверждённые факты памяти: '+contact_memory(peer)+'\nКонтактный профиль (частично предположения, не факты): '+json.dumps(profile,ensure_ascii=False)[:1500]+'\nИндивидуальные инструкции: '+style['instructions']+'\nConversation Development Engine: реагируй на актуальный интерес, развивай мысль наблюдением или уместным уточнением; не вынуждай человека отвечать, не задавай вопросы ради продолжения, допускай естественную паузу. Не повторяй фразы и вопросы.'}]+history(peer),
            temperature=0.75, max_tokens=380)
        text=(response.choices[0].message.content or '').strip()
        if not text: return
        text=await quality_check(peer,text)
        if not text:return
        if is_sensitive(text) or (setting('reply_quality')=='maximum' and len(text)>1800):
            with connect() as c:
                c.execute('INSERT INTO drafts(peer_id,text,reason,created) VALUES (?,?,?,?)',(peer,text,'Ответ содержит чувствительные утверждения',now()))
            event(peer,'approval','Черновик отправлен на согласование')
            return
        # Recheck mode after awaiting model; owner may have taken over.
        with connect() as c:
            row=c.execute('SELECT mode FROM contacts WHERE peer_id=?',(peer,)).fetchone()
        if not row or row['mode']!='auto' or setting('auto_reply')!='true': return
        await client.send_message(peer,text)
        add_message(peer,'assistant',text)
        event(peer,'sent','Агент ответил')
    except Exception as exc:
        log.exception('Reply failed')
        event(peer,'error',f'Ошибка генерации/отправки: {str(exc)[:180]}')

async def on_incoming(evt):
    if not evt.is_private or evt.out or not evt.raw_text: return
    peer=int(evt.sender_id)
    if client and peer == (await client.get_me()).id: return
    sender=await evt.get_sender()
    name=' '.join(filter(None,[getattr(sender,'first_name',None),getattr(sender,'last_name',None)]))
    username=getattr(sender,'username','') or ''
    with connect() as c:
        exists=c.execute('SELECT 1 FROM contacts WHERE peer_id=?',(peer,)).fetchone()
        c.execute("INSERT OR IGNORE INTO contacts(peer_id,name,username,last_seen) VALUES (?,?,?,?)",(peer,name,username,now()))
        c.execute('UPDATE contacts SET name=?,username=?,last_seen=? WHERE peer_id=?',(name,username,now(),peer))
        mode=c.execute('SELECT mode FROM contacts WHERE peer_id=?',(peer,)).fetchone()['mode']
    add_message(peer,'user',evt.raw_text)
    session_tick(peer)
    if setting('conversation_analysis')=='true':
        with connect() as c:
            count=c.execute("SELECT COUNT(*) n FROM messages WHERE peer_id=? AND role='user'",(peer,)).fetchone()['n']
        if count%5==0: asyncio.create_task(analyze_contact(peer))
    if is_insult(evt.raw_text):
        with connect() as c: c.execute("UPDATE contacts SET mode='paused' WHERE peer_id=?",(peer,))
        event(peer,'insult','Обнаружено возможное оскорбление: диалог приостановлен до решения владельца')
        return
    if not exists: event(peer,'new_contact',f'Новый контакт: {name or peer}')
    if setting('auto_reply')!='true' or mode!='auto': return
    lock=locks.setdefault(peer,asyncio.Lock())
    async with lock:
        # A human may take over while the previous reply is being generated.
        with connect() as c:
            current=c.execute('SELECT mode FROM contacts WHERE peer_id=?',(peer,)).fetchone()
        if current and current['mode']=='auto':
            await reply_text(peer,evt.raw_text)

async def telegram_worker():
    global client, connected
    try:
        from telethon import TelegramClient, events
    except ImportError:
        event(None,'setup','Telethon не установлен — выполните pip install -r requirements.txt')
        return
    api_id=os.getenv('TELEGRAM_API_ID','')
    api_hash=os.getenv('TELEGRAM_API_HASH','')
    if not api_id or not api_hash:
        event(None,'setup','Telegram API_ID / API_HASH не настроены')
        return
    client=TelegramClient(str(ROOT/'owner_session'),int(api_id),api_hash)
    client.add_event_handler(on_incoming,events.NewMessage(incoming=True))
    try:
        # First-time interactive login must be performed with login.py before starting server.
        await client.connect()
        if not await client.is_user_authorized():
            event(None,'setup','Telegram не авторизован: сначала запустите python login.py')
            await client.disconnect(); return
        connected=True
        event(None,'system','Telegram подключён')
        await client.run_until_disconnected()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.exception('Telegram failed')
        event(None,'error',f'Telegram: {str(exc)[:180]}')
    finally:
        connected=False
        if client and client.is_connected(): await client.disconnect()

async def telegram_supervisor():
    """Retry disconnected Telegram sessions with bounded exponential backoff."""
    delay=5
    while True:
        try:
            await telegram_worker()
            if not os.getenv('TELEGRAM_API_ID') or not os.getenv('TELEGRAM_API_HASH'):
                return
            event(None,'error','Telegram disconnected; reconnect scheduled')
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Telegram supervisor failure')
        await asyncio.sleep(delay)
        delay=min(delay*2,300)

async def daily_worker():
    while True:
        try:
            t=local_now(); day=t.date().isoformat()
            if t.hour>=int(setting('report_hour') or '21'):
                with connect() as c:
                    exists=c.execute('SELECT 1 FROM reports WHERE day=?',(day,)).fetchone()
                    if not exists:
                        events=c.execute("SELECT category,COUNT(*) n FROM events WHERE substr(created,1,10)=? GROUP BY category",(datetime.now(timezone.utc).date().isoformat(),)).fetchall()
                        counts=', '.join(f"{r['category']}: {r['n']}" for r in events) or 'Нет событий'
                        pending=c.execute("SELECT COUNT(*) n FROM drafts WHERE status='pending'").fetchone()['n']
                        contact_count=c.execute('SELECT COUNT(*) n FROM contacts').fetchone()['n']
                        reviews=c.execute("SELECT verdict,COUNT(*) n FROM review_log WHERE substr(created,1,10)=? GROUP BY verdict",(datetime.now(timezone.utc).date().isoformat(),)).fetchall()
                        quality=', '.join(f"{r['verdict']}: {r['n']}" for r in reviews) or 'нет проверок'
                        candidates=c.execute("SELECT COUNT(*) n FROM memory_candidates WHERE status='pending'").fetchone()['n']
                        body=f'Ежедневный отчёт {day}\nКонтакты: {contact_count}\nСобытия: {counts}\nПроверки ответов: {quality}\nОжидают согласования: {pending}\nКандидаты памяти: {candidates}\nИзменения личности: только после вашего утверждения.'
                        c.execute('INSERT INTO reports(day,body,created) VALUES (?,?,?)',(day,body,now()))
                        c.execute('INSERT INTO learning(proposal,reason,created) VALUES (?,?,?)',('Проверить разнообразие ответов и замечания контроля качества',f'Данные дня: {quality}. Согласование не применяет изменения автоматически.',now()))
                        await notify_owner(body)
                        await propose_personality_improvement(day)
        except Exception: log.exception('Daily report failed')
        await asyncio.sleep(60)

report_task=None
worker_task=None
@asynccontextmanager
async def lifespan(app):
    global ai,worker_task,report_task
    init_db()
    if os.getenv('OPENAI_API_KEY'): ai=AsyncOpenAI(api_key=os.environ['OPENAI_API_KEY'])
    worker_task=asyncio.create_task(telegram_supervisor())
    report_task=asyncio.create_task(daily_worker())
    yield
    if report_task:
        report_task.cancel()
        try: await report_task
        except asyncio.CancelledError: pass
    if worker_task:
        worker_task.cancel()
        try: await worker_task
        except asyncio.CancelledError: pass

app=FastAPI(title='Personal Telegram AI Agent',lifespan=lifespan)
templates=Jinja2Templates(directory=str(ROOT/'templates'))
security=HTTPBasic()

def auth(creds:HTTPBasicCredentials=Depends(security)):
    pw=os.getenv('DASHBOARD_PASSWORD','')
    if not pw or pw=='change-this-long-password':
        raise HTTPException(503,'Set a strong DASHBOARD_PASSWORD in .env')
    if not (secrets.compare_digest(creds.username,'owner') and secrets.compare_digest(creds.password,pw)):
        raise HTTPException(401,'Invalid credentials',headers={'WWW-Authenticate':'Basic'})
    return True

@app.get('/',response_class=HTMLResponse)
async def home(req:Request, ok=Depends(auth)):
    return templates.TemplateResponse('index.html',{'request':req})

@app.get('/api/state')
async def state(ok=Depends(auth)):
    with connect() as c:
        contacts=[dict(x) for x in c.execute('SELECT * FROM contacts ORDER BY last_seen DESC LIMIT 300')]
        events=[dict(x) for x in c.execute('SELECT * FROM events ORDER BY id DESC LIMIT 80')]
        drafts=[dict(x) for x in c.execute("SELECT * FROM drafts WHERE status='pending' ORDER BY id DESC LIMIT 40")]
        totals={'contacts':c.execute('SELECT COUNT(*) n FROM contacts').fetchone()['n'], 'messages':c.execute('SELECT COUNT(*) n FROM messages').fetchone()['n']}
    with connect() as c:
        reports=[dict(x) for x in c.execute('SELECT * FROM reports ORDER BY id DESC LIMIT 10')]
        learning=[dict(x) for x in c.execute('SELECT * FROM learning ORDER BY id DESC LIMIT 30')]
        memory_candidates=[dict(x) for x in c.execute("SELECT * FROM memory_candidates WHERE status='pending' ORDER BY id DESC LIMIT 60")]
        outbound=[dict(x) for x in c.execute('SELECT * FROM outbound_requests ORDER BY id DESC LIMIT 50')]
        reviews=[dict(x) for x in c.execute('SELECT * FROM review_log ORDER BY id DESC LIMIT 40')]
        versions=[dict(x) for x in c.execute('SELECT id,reason,created FROM persona_versions ORDER BY id DESC LIMIT 20')]
        notification_rules=[dict(x) for x in c.execute('SELECT * FROM notification_rules ORDER BY category')]
    return {'memory_candidates':memory_candidates,'outbound':outbound,'reviews':reviews,'versions':versions,'notification_rules':notification_rules,'reports':reports,'learning':learning,'connected':connected,'ai_ready':bool(ai),'settings':{k:setting(k) for k in ('persona','auto_reply','daily_limit','model','owner_timezone','report_hour','notifications_enabled','auto_memory','humor','depth','warmth','business','initiative','reply_quality','memory_analysis','quality_review','conversation_analysis')},'contacts':contacts,'events':events,'drafts':drafts,'totals':totals}

@app.get('/api/contact/{peer}')
async def contact(peer:int,ok=Depends(auth)):
    with connect() as c:
        row=c.execute('SELECT * FROM contacts WHERE peer_id=?',(peer,)).fetchone()
        if not row: raise HTTPException(404,'Contact not found')
        msgs=[dict(x) for x in c.execute('SELECT * FROM messages WHERE peer_id=? ORDER BY id DESC LIMIT 100',(peer,))]
    return {'contact':dict(row),'messages':list(reversed(msgs))}

class SettingsIn(BaseModel):
    persona:str=Field(min_length=30,max_length=10000)
    auto_reply:bool
    daily_limit:int=Field(ge=1,le=100)
    model:str=Field(min_length=2,max_length=100)

@app.post('/api/settings')
async def save_settings(data:SettingsIn,ok=Depends(auth)):
    with connect() as c:
        c.execute('INSERT INTO persona_versions(persona,humor,depth,warmth,business,initiative,reason,created) VALUES (?,?,?,?,?,?,?,?)',(setting('persona'),int(setting('humor')),int(setting('depth')),int(setting('warmth')),int(setting('business')),int(setting('initiative')),'Снимок перед изменением владельцем',now()))
        for k,v in data.model_dump().items():
            c.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(k,str(v).lower() if isinstance(v,bool) else str(v)))
    event(None,'settings','Владелец изменил настройки')
    return {'ok':True}

class ContactIn(BaseModel):
    mode:str
    notes:str=Field(max_length=10000)

@app.post('/api/contact/{peer}')
async def save_contact(peer:int,data:ContactIn,ok=Depends(auth)):
    if data.mode not in ('auto','manual','paused'): raise HTTPException(400,'Invalid mode')
    with connect() as c:
        cur=c.execute('UPDATE contacts SET mode=?,notes=? WHERE peer_id=?',(data.mode,data.notes,peer))
        if not cur.rowcount: raise HTTPException(404,'Contact not found')
    event(peer,'control',f'Режим изменён владельцем: {data.mode}')
    return {'ok':True}

class MessageIn(BaseModel):
    text:str=Field(min_length=1,max_length=4096)

@app.post('/api/contact/{peer}/send')
async def send_manual(peer:int,data:MessageIn,ok=Depends(auth)):
    if not connected: raise HTTPException(503,'Telegram not connected')
    with connect() as c:
        row=c.execute('SELECT mode FROM contacts WHERE peer_id=?',(peer,)).fetchone()
        if not row: raise HTTPException(404,'Contact not found')
        if row['mode']=='auto': raise HTTPException(409,'Switch contact to manual mode before sending')
    try: await client.send_message(peer,data.text)
    except Exception as exc: raise HTTPException(502,str(exc)[:200])
    add_message(peer,'assistant',data.text)
    event(peer,'manual_sent','Владелец отправил сообщение')
    return {'ok':True}

@app.post('/api/draft/{draft_id}/approve')
async def approve_draft(draft_id:int,ok=Depends(auth)):
    if not connected: raise HTTPException(503,'Telegram not connected')
    with connect() as c:
        draft=c.execute("SELECT * FROM drafts WHERE id=? AND status='pending'",(draft_id,)).fetchone()
        if not draft: raise HTTPException(404,'Draft not found')
        if not draft['text']: raise HTTPException(400,'This alert has no draft; review contact manually')
    await client.send_message(draft['peer_id'],draft['text'])
    with connect() as c: c.execute("UPDATE drafts SET status='approved' WHERE id=?",(draft_id,))
    add_message(draft['peer_id'],'assistant',draft['text'])
    event(draft['peer_id'],'approval','Владелец утвердил и отправил черновик')
    return {'ok':True}

@app.post('/api/draft/{draft_id}/reject')
async def reject_draft(draft_id:int,ok=Depends(auth)):
    with connect() as c:
        c.execute("UPDATE drafts SET status='rejected' WHERE id=? AND status='pending'",(draft_id,))
    return {'ok':True}


class AdvancedSettings(BaseModel):
    owner_timezone:str=Field(min_length=2,max_length=80)
    report_hour:int=Field(ge=0,le=23)
    notifications_enabled:bool
    humor:int=Field(ge=0,le=100)
    depth:int=Field(ge=0,le=100)
    warmth:int=Field(ge=0,le=100)
    business:int=Field(ge=0,le=100)
    initiative:int=Field(ge=0,le=100)

@app.post('/api/advanced')
async def save_advanced(data:AdvancedSettings,ok=Depends(auth)):
    try: ZoneInfo(data.owner_timezone)
    except Exception: raise HTTPException(400,'Unknown timezone')
    with connect() as c:
        for k,v in data.model_dump().items():
            c.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(k,str(v).lower() if isinstance(v,bool) else str(v)))
    event(None,'settings','Владелец изменил расширенные настройки личности и отчётов')
    return {'ok':True}

class MemoryIn(BaseModel):
    fact:str=Field(min_length=3,max_length=1000)

@app.get('/api/contact/{peer}/intelligence')
async def intelligence(peer:int,ok=Depends(auth)):
    with connect() as c:
        memories=[dict(x) for x in c.execute('SELECT * FROM memories WHERE peer_id=? ORDER BY id DESC',(peer,))]
        schedule=c.execute('SELECT * FROM schedules WHERE peer_id=?',(peer,)).fetchone()
    return {'memories':memories,'schedule':dict(schedule) if schedule else None}

@app.post('/api/contact/{peer}/memory')
async def new_memory(peer:int,data:MemoryIn,ok=Depends(auth)):
    with connect() as c:
        if not c.execute('SELECT 1 FROM contacts WHERE peer_id=?',(peer,)).fetchone(): raise HTTPException(404,'Unknown contact')
        c.execute("INSERT INTO memories(peer_id,fact,source,status,created) VALUES (?,?,'owner','approved',?)",(peer,data.fact,now()))
    event(peer,'memory','Владелец добавил подтверждённую память')
    return {'ok':True}

@app.delete('/api/memory/{memory_id}')
async def delete_memory(memory_id:int,ok=Depends(auth)):
    with connect() as c: c.execute('DELETE FROM memories WHERE id=?',(memory_id,))
    return {'ok':True}

class ScheduleIn(BaseModel):
    enabled:bool=False
    start_hour:int=Field(ge=0,le=23)
    end_hour:int=Field(ge=1,le=24)
    weekdays:str=Field(max_length=20)
    min_gap_hours:int=Field(ge=0,le=720)
    weekly_max:int=Field(ge=1,le=100)

@app.post('/api/contact/{peer}/schedule')
async def save_schedule(peer:int,data:ScheduleIn,ok=Depends(auth)):
    if data.start_hour>=data.end_hour or any(x not in '0123456' for x in data.weekdays.replace(',','')): raise HTTPException(400,'Invalid schedule')
    with connect() as c:
        c.execute('INSERT INTO schedules(peer_id,enabled,start_hour,end_hour,weekdays,min_gap_hours,weekly_max) VALUES (?,?,?,?,?,?,?) ON CONFLICT(peer_id) DO UPDATE SET enabled=excluded.enabled,start_hour=excluded.start_hour,end_hour=excluded.end_hour,weekdays=excluded.weekdays,min_gap_hours=excluded.min_gap_hours,weekly_max=excluded.weekly_max',(peer,int(data.enabled),data.start_hour,data.end_hour,data.weekdays,data.min_gap_hours,data.weekly_max))
    event(peer,'schedule','Владелец изменил расписание контакта')
    return {'ok':True}

@app.post('/api/learning/{proposal_id}/{decision}')
async def decide_learning(proposal_id:int,decision:str,ok=Depends(auth)):
    if decision not in ('approved','rejected'): raise HTTPException(400,'Invalid decision')
    with connect() as c:
        row=c.execute("SELECT * FROM learning WHERE id=? AND status='pending'",(proposal_id,)).fetchone()
        if not row: raise HTTPException(404,'Proposal not found')
        if decision=='approved' and row['instruction']:
            c.execute('INSERT INTO persona_versions(persona,humor,depth,warmth,business,initiative,reason,created) VALUES (?,?,?,?,?,?,?,?)',(setting('persona'),int(setting('humor')),int(setting('depth')),int(setting('warmth')),int(setting('business')),int(setting('initiative')),'До утверждения улучшения '+str(proposal_id),now()))
            c.execute("UPDATE settings SET value=? WHERE key='persona'",(setting('persona')+'\nУтверждённое владельцем улучшение: '+row['instruction'],))
        c.execute('UPDATE learning SET status=? WHERE id=?',(decision,proposal_id))
    event(None,'learning',f'Владелец: {decision} для рекомендации {proposal_id}')
    return {'ok':True}

@app.get('/api/health')
async def health():
    return {'service':'ok','telegram_connected':connected,'ai_configured':bool(ai)}


class PeerPersonaIn(BaseModel):
    humor:int=Field(ge=0,le=100)
    depth:int=Field(ge=0,le=100)
    warmth:int=Field(ge=0,le=100)
    business:int=Field(ge=0,le=100)
    initiative:int=Field(ge=0,le=100)
    instructions:str=Field(default='',max_length=1500)

@app.get('/api/contact/{peer}/profile')
async def get_profile(peer:int,ok=Depends(auth)):
    return {'profile':contact_profile(peer),'style':peer_style(peer)}

@app.post('/api/contact/{peer}/persona')
async def save_peer_persona(peer:int,data:PeerPersonaIn,ok=Depends(auth)):
    with connect() as c:
        if not c.execute('SELECT 1 FROM contacts WHERE peer_id=?',(peer,)).fetchone():raise HTTPException(404,'Contact not found')
        c.execute('INSERT INTO contact_personas(peer_id,humor,depth,warmth,business,initiative,instructions) VALUES (?,?,?,?,?,?,?) ON CONFLICT(peer_id) DO UPDATE SET humor=excluded.humor,depth=excluded.depth,warmth=excluded.warmth,business=excluded.business,initiative=excluded.initiative,instructions=excluded.instructions',(peer,data.humor,data.depth,data.warmth,data.business,data.initiative,data.instructions))
    event(peer,'personality','Индивидуальная личность изменена владельцем')
    return {'ok':True}

@app.post('/api/memory-candidate/{candidate_id}/{decision}')
async def memory_decision(candidate_id:int,decision:str,ok=Depends(auth)):
    if decision not in ('approved','rejected'):raise HTTPException(400,'Invalid decision')
    with connect() as c:
        row=c.execute("SELECT * FROM memory_candidates WHERE id=? AND status='pending'",(candidate_id,)).fetchone()
        if not row:raise HTTPException(404,'Candidate not found')
        c.execute('UPDATE memory_candidates SET status=? WHERE id=?',(decision,candidate_id))
        if decision=='approved':
            c.execute('INSERT INTO memories(peer_id,fact,source,status,created) VALUES (?,?,?,?,?)',(row['peer_id'],row['fact'],'owner-approved-analysis','approved',now()))
    event(row['peer_id'],'memory',f'Решение владельца о памяти: {decision}')
    return {'ok':True}

@app.post('/api/contact/{peer}/analyze')
async def analyze_now(peer:int,ok=Depends(auth)):
    with connect() as c:
        if not c.execute('SELECT 1 FROM contacts WHERE peer_id=?',(peer,)).fetchone():raise HTTPException(404,'Contact not found')
    if not ai:raise HTTPException(503,'OpenAI not configured')
    await analyze_contact(peer)
    return {'ok':True,'profile':contact_profile(peer)}

class OutboundIn(BaseModel):
    peer_id:int
    text:str=Field(min_length=1,max_length=4096)
    reason:str=Field(default='Первое исходящее сообщение',max_length=500)

@app.post('/api/outbound')
async def outbound_request(data:OutboundIn,ok=Depends(auth)):
    with connect() as c:
        c.execute('INSERT INTO outbound_requests(peer_id,text,reason,created) VALUES (?,?,?,?)',(data.peer_id,data.text,data.reason,now()))
    event(data.peer_id,'approval','Первое исходящее сообщение ожидает подтверждения владельца')
    return {'ok':True}

@app.post('/api/outbound/{request_id}/{decision}')
async def outbound_decision(request_id:int,decision:str,ok=Depends(auth)):
    if decision not in ('approved','rejected'):raise HTTPException(400,'Invalid decision')
    with connect() as c:
        row=c.execute("SELECT * FROM outbound_requests WHERE id=? AND status='pending'",(request_id,)).fetchone()
    if not row:raise HTTPException(404,'Request not found')
    if decision=='approved':
        if not connected:raise HTTPException(503,'Telegram not connected')
        if is_sensitive(row['text']):raise HTTPException(400,'Sensitive outbound message requires manual review and safe editing')
        if not allowed_window(row['peer_id']):raise HTTPException(409,'Outside allowed outbound schedule or frequency')
        await client.send_message(row['peer_id'],row['text'])
        add_message(row['peer_id'],'assistant',row['text'])
    with connect() as c:c.execute('UPDATE outbound_requests SET status=? WHERE id=?',(decision,request_id))
    event(row['peer_id'],'outbound',f'Владелец: {decision}')
    return {'ok':True}

@app.post('/api/persona/restore/{version_id}')
async def restore_persona(version_id:int,ok=Depends(auth)):
    with connect() as c:
        row=c.execute('SELECT * FROM persona_versions WHERE id=?',(version_id,)).fetchone()
        if not row:raise HTTPException(404,'Version not found')
        c.execute('INSERT INTO persona_versions(persona,humor,depth,warmth,business,initiative,reason,created) VALUES (?,?,?,?,?,?,?,?)',(setting('persona'),int(setting('humor')),int(setting('depth')),int(setting('warmth')),int(setting('business')),int(setting('initiative')),'Снимок перед откатом',now()))
        for key in ('persona','humor','depth','warmth','business','initiative'):
            c.execute('UPDATE settings SET value=? WHERE key=?',(str(row[key]),key))
    event(None,'personality',f'Владелец восстановил версию {version_id}')
    return {'ok':True}

class RulesIn(BaseModel):
    rules:dict[str,bool]

@app.post('/api/notifications')
async def set_notification_rules(data:RulesIn,ok=Depends(auth)):
    with connect() as c:
        for category,enabled in data.rules.items():
            if category not in ('new_contact','approval','error','insult','critical','daily_report','control'):raise HTTPException(400,'Unknown category')
            c.execute('INSERT INTO notification_rules(category,enabled) VALUES (?,?) ON CONFLICT(category) DO UPDATE SET enabled=excluded.enabled',(category,int(enabled)))
    return {'ok':True}

@app.get('/api/analytics')
async def detailed_analytics(ok=Depends(auth)):
    with connect() as c:
        activity=[dict(x) for x in c.execute("SELECT substr(created,1,10) day,role,COUNT(*) n FROM messages GROUP BY day,role ORDER BY day DESC LIMIT 60")]
        quality=[dict(x) for x in c.execute('SELECT verdict,COUNT(*) n FROM review_log GROUP BY verdict')]
        contacts_trend=[dict(x) for x in c.execute('''SELECT day,SUM(incoming) incoming,SUM(outgoing) outgoing,
            COUNT(DISTINCT peer_id) active_contacts FROM daily_contact_metrics GROUP BY day ORDER BY day DESC LIMIT 30''')]
        escalations=[dict(x) for x in c.execute('''SELECT substr(created,1,10) day,COUNT(*) n FROM drafts
            GROUP BY day ORDER BY day DESC LIMIT 30''')]
        modes=[dict(x) for x in c.execute('SELECT mode,COUNT(*) n FROM contacts GROUP BY mode')]
    with connect() as c:
        active_time=[dict(x) for x in c.execute('SELECT day,SUM(active_seconds) active_seconds FROM daily_active_time GROUP BY day ORDER BY day DESC LIMIT 30')]
    return {'activity':activity,'quality':quality,'modes':modes,'contacts_trend':contacts_trend,'escalations':escalations,'active_time':active_time}

@app.get('/api/contact/{peer}/insights')
async def contact_insights(peer:int,ok=Depends(auth)):
    with connect() as c:
        if not c.execute('SELECT 1 FROM contacts WHERE peer_id=?',(peer,)).fetchone():
            raise HTTPException(404,'Contact not found')
        trend=[dict(x) for x in c.execute('''SELECT day,incoming,outgoing FROM daily_contact_metrics
             WHERE peer_id=? ORDER BY day DESC LIMIT 30''',(peer,))]
        versions=[dict(x) for x in c.execute('''SELECT id,profile,created FROM profile_history
             WHERE peer_id=? ORDER BY id DESC LIMIT 10''',(peer,))]
        reviews=[dict(x) for x in c.execute('''SELECT verdict,COUNT(*) n FROM review_log
             WHERE peer_id=? GROUP BY verdict''',(peer,))]
        active_time=[dict(x) for x in c.execute('SELECT day,active_seconds FROM daily_active_time WHERE peer_id=? ORDER BY day DESC LIMIT 30',(peer,))]
        recent=[dict(x) for x in c.execute('''SELECT role,text,created FROM messages
             WHERE peer_id=? ORDER BY id DESC LIMIT 30''',(peer,))]
    total_in=sum(x['incoming'] for x in trend);total_out=sum(x['outgoing'] for x in trend)
    return {'peer_id':peer,'active_time':active_time,'trend':trend,'profile_versions':[
        {'id':x['id'],'profile':json.loads(x['profile']),'created':x['created']} for x in versions],
        'quality':reviews,'messages_30d':{'incoming':total_in,'outgoing':total_out},
        'last_messages':list(reversed(recent)),
        'caveat':'Активное время приблизительное; число сообщений и оценка ИИ не являются достоверной оценкой настроения или намерений собеседника.'}

@app.delete('/api/contact/{peer}/data')
async def erase_contact_data(peer:int,ok=Depends(auth)):
    with connect() as c:
        for table in ('messages','memories','memory_candidates','contact_profiles','contact_personas','schedules','conversation_sessions','daily_active_time','review_log','outbound_requests','drafts','profile_history','daily_contact_metrics'):
            c.execute(f'DELETE FROM {table} WHERE peer_id=?',(peer,))
        c.execute('DELETE FROM contacts WHERE peer_id=?',(peer,))
    event(None,'privacy',f'Владелец удалил данные контакта {peer}')
    return {'ok':True}


class GroupScanIn(BaseModel):
    group_id:int
    max_messages:int=Field(default=150,ge=20,le=500)

@app.get('/api/groups')
async def available_groups(ok=Depends(auth)):
    if not connected or not client: raise HTTPException(503,'Telegram not connected')
    result=[]
    async for dialog in client.iter_dialogs(limit=120):
        if dialog.is_group:
            result.append({'id':dialog.id,'title':dialog.name})
    return {'groups':result}

@app.post('/api/groups/scan')
async def scan_group(data:GroupScanIn,ok=Depends(auth)):
    """Owner-triggered preview, not autonomous targeting or mass messaging."""
    if not connected or not client: raise HTTPException(503,'Telegram not connected')
    from telethon import errors
    try:
        entity=await client.get_entity(data.group_id)
        if not getattr(entity,'megagroup',False) and not getattr(entity,'gigagroup',False):
            raise HTTPException(400,'Only joined discussion groups are supported')
        group_title=getattr(entity,'title','Group')[:180]
        me=await client.get_me()
        counts={}
        async for message in client.iter_messages(entity,limit=data.max_messages):
            sender=message.sender
            if not sender or getattr(sender,'bot',False) or getattr(sender,'deleted',False) or sender.id==me.id: continue
            if not getattr(message,'message',''): continue
            entry=counts.setdefault(sender.id,{'name':' '.join(filter(None,[getattr(sender,'first_name',''),getattr(sender,'last_name','')]))[:180], 'username':getattr(sender,'username','') or '', 'count':0})
            entry['count']+=1
        # Require evidence of participation; never auto-message.
        eligible=[(pid,entry) for pid,entry in counts.items() if entry['count']>=2]
        with connect() as c:
            for pid,entry in eligible:
                c.execute('''INSERT INTO group_candidates(peer_id,group_id,group_title,display_name,username,message_count,discovered)
                VALUES (?,?,?,?,?,?,?) ON CONFLICT(peer_id) DO UPDATE SET message_count=excluded.message_count,
                group_title=excluded.group_title,group_id=excluded.group_id,discovered=excluded.discovered''',
                (pid,data.group_id,group_title,entry['name'],entry['username'],entry['count'],now()))
        event(None,'discovery',f'Владелец просмотрел группу {group_title}; найдено {len(eligible)} активных участников. Никаких сообщений не отправлено.')
        return {'ok':True,'candidates':len(eligible),'notice':'Preview only. No automatic outreach. Respect group rules and contact consent.'}
    except HTTPException: raise
    except errors.RPCError as exc: raise HTTPException(502,f'Telegram error: {str(exc)[:150]}')

@app.get('/api/groups/candidates')
async def group_candidates(ok=Depends(auth)):
    with connect() as c:
        rows=[dict(r) for r in c.execute('SELECT * FROM group_candidates ORDER BY discovered DESC LIMIT 150')]
    return {'candidates':rows}

class CandidateDecision(BaseModel):
    status:str

@app.post('/api/groups/candidates/{peer}/decision')
async def decide_candidate(peer:int,data:CandidateDecision,ok=Depends(auth)):
    if data.status not in ('review','dismissed','approved_for_draft'):raise HTTPException(400,'Invalid status')
    with connect() as c:
        cur=c.execute('UPDATE group_candidates SET status=? WHERE peer_id=?',(data.status,peer))
        if not cur.rowcount:raise HTTPException(404,'Unknown candidate')
    return {'ok':True,'notice':'Approval does not send a message. A separate outbound draft and owner send approval are required.'}

@app.get('/api/diagnostics')
async def diagnostics(ok=Depends(auth)):
    import importlib.util
    with connect() as c:
        db_ok=c.execute('PRAGMA quick_check').fetchone()[0]=='ok'
        pending=c.execute("SELECT COUNT(*) FROM drafts WHERE status='pending'").fetchone()[0]
    return {'database_ok':db_ok,'telegram_connected':connected,'openai_configured':bool(ai),
            'notifications_configured':bool(os.getenv('NOTIFY_BOT_TOKEN') and os.getenv('NOTIFY_CHAT_ID')),
            'pending_approvals':pending,'telethon_installed':importlib.util.find_spec('telethon') is not None,
            'remote_integration_tested':False}
