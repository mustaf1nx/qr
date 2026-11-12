import os
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", "sqlite:///./test.db")
os.environ["COOKIE_SECURE"] = "false"
os.environ["META_ACCESS_TOKEN"] = ""
import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from app.db import Base, engine, Session, Event, Staff, Ticket, WebhookEvent, now
from app.main import app, passwords
from app.services import issue_ticket, TicketError
from app.config import settings


@pytest.fixture(autouse=True)
def db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session.begin() as s:
        s.add(Staff(username="admin",password_hash=passwords.hash("test-password-123"),role="admin"))
        s.add(Staff(username="scanner",password_hash=passwords.hash("test-password-123"),role="scanner"))
        s.add(Event(name="Test", starts_at=now()-60,expires_at=now()+3600,capacity=10,keyword="БИЛЕТ",require_follow=True))


def client(role="admin"):
    c=TestClient(app, headers={"X-Requested-With":"EventPass"})
    assert c.post('/api/auth/login',json={"username":role,"password":"test-password-123"}).status_code==200
    return c


def test_one_ticket_per_user():
    a=issue_ticket(1,"123","guest",True)
    b=issue_ticket(1,"123","guest",True)
    assert a.token==b.token
    with Session() as s:
        assert s.get(Event,1).issued_count==1


@pytest.mark.parametrize("follow",[False,None])
def test_follow_not_verified(follow):
    with pytest.raises(TicketError):
        issue_ticket(1,"123","guest",follow)
    with Session() as s:
        assert s.scalar(select(func.count()).select_from(Ticket))==0


def test_capacity():
    with Session.begin() as s:
        s.get(Event,1).capacity=1
    issue_ticket(1,"1","one",True)
    with pytest.raises(TicketError):
        issue_ticket(1,"2","two",True)


def test_unlimited_ongoing_registration():
    with Session.begin() as s:
        e=s.get(Event,1)
        e.capacity=e.starts_at=e.expires_at=None
    for i in range(12):
        t=issue_ticket(1,str(i),'guest',True)
    assert client().post('/api/scanner/redeem',json={"event_id":1,"token":t.token}).json()['result']=='accepted'
    with Session() as s:
        assert s.get(Event,1).issued_count==12


def test_concurrent_redeem():
    t=issue_ticket(1,"123","guest",True)
    clients=[client("scanner") for _ in range(8)]
    def scan(c):
        return c.post('/api/scanner/redeem',json={"event_id":1,"token":t.token}).json()['result']
    with ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(scan,clients))
    assert results.count("accepted")==1
    assert results.count("used")==7


def test_revoke_and_permissions():
    t=issue_ticket(1,"123","guest",True)
    assert client("scanner").get('/api/admin/tickets?event_id=1').status_code==403
    assert TestClient(app).get('/api/events').status_code==401
    c=client()
    assert c.post(f'/api/admin/tickets/{t.id}/revoke').status_code==200
    assert c.post('/api/scanner/redeem',json={"event_id":1,"token":t.token}).json()['result']=="revoked"


def test_wrong_event_expired_and_future():
    t=issue_ticket(1,"123","guest",True)
    c=client()
    assert c.post('/api/scanner/redeem',json={"event_id":999,"token":t.token}).json()['result']=="wrong_event"
    with Session.begin() as s:
        s.get(Event,1).starts_at=now()+500
    assert c.post('/api/scanner/redeem',json={"event_id":1,"token":t.token}).json()['result']=="not_started"
    with Session.begin() as s:
        s.get(Event,1).expires_at=now()-1
    assert c.post('/api/scanner/redeem',json={"event_id":1,"token":t.token}).json()['result']=="expired"


def test_csrf_and_logout():
    c=client()
    assert c.post('/api/auth/logout',headers={"Origin":"https://evil.example"}).status_code==403
    assert c.post('/api/auth/logout').status_code==200
    assert c.get('/api/events').status_code==401


def test_webhook_signature_and_dedup(monkeypatch):
    monkeypatch.setattr(settings,"meta_app_secret","test-secret")
    monkeypatch.setattr(settings,"meta_account_id","account")
    data=json.dumps({"object":"instagram","entry":[{"id":"account","messaging":[{"sender":{"id":"123"},"message":{"mid":"m1","text":"БИЛЕТ"}}]}]}).encode()
    c=TestClient(app)
    assert c.post('/webhooks/instagram',content=data).status_code==403
    sig='sha256='+hmac.new(b'test-secret',data,hashlib.sha256).hexdigest()
    for _ in range(2):
        assert c.post('/webhooks/instagram',content=data,headers={'X-Hub-Signature-256':sig}).status_code==200
    with Session() as s:
        assert s.scalar(select(func.count()).select_from(WebhookEvent))==1


def test_public_ticket_and_qr():
    t=issue_ticket(1,"123","guest",True)
    c=TestClient(app)
    assert c.get('/api/tickets/'+t.token).json()['status']=='issued'
    r=c.get('/api/tickets/'+t.token+'/qr')
    assert r.status_code==200 and r.content.startswith(b'\x89PNG')
    assert c.get('/api/tickets/unknown').status_code==404


def test_guest_flow_without_account_and_repeat_request():
    from uuid import uuid4
    from app.db import Participant
    c=TestClient(app, headers={'X-Requested-With':'EventPass'})
    assert c.get('/api/registration/1').json()['open'] is True
    body={'request_id':str(uuid4())}
    a=c.post('/api/registration/1/tickets',json=body)
    assert a.status_code==200
    assert c.post('/api/registration/1/tickets',json=body).json()==a.json()
    b=c.post('/api/registration/1/tickets',json={'request_id':str(uuid4())})
    assert b.status_code==200 and b.json()['id']!=a.json()['id']
    token=a.json()['url'].split('/')[-1]
    assert c.get('/api/tickets/'+token).json()['anonymous'] is True
    with Session() as s:
        assert s.get(Event,1).issued_count==2
        assert all(p.follows is None for p in s.scalars(select(Participant)))
    scanner=client('scanner')
    for expected in ('accepted','used'):
        assert scanner.post('/api/scanner/redeem',json={'event_id':1,'token':token}).json()['result']==expected


def test_guest_capacity_closed_and_invalid_request():
    from uuid import uuid4
    c=TestClient(app,headers={'X-Requested-With':'EventPass'})
    assert c.post('/api/registration/1/tickets',json={}).status_code==422
    assert c.get('/api/registration/999').status_code==404
    with Session.begin() as s:
        s.get(Event,1).capacity=1
    body={'request_id':str(uuid4())}
    assert c.post('/api/registration/1/tickets',json=body).status_code==200
    assert c.post('/api/registration/1/tickets',json=body).status_code==200
    assert c.post('/api/registration/1/tickets',json={'request_id':str(uuid4())}).status_code==409
    assert c.get('/api/registration/1').json()['open'] is False
    with Session.begin() as s:
        s.get(Event,1).active=False
        s.get(Event,1).capacity=None
    assert c.post('/api/registration/1/tickets',json={'request_id':str(uuid4())}).status_code==409


def test_poster_points_to_registration(monkeypatch):
    import app.main as main
    monkeypatch.setattr(main,'qr_response',lambda value:{'encoded':value})
    assert client().get('/api/admin/poster-qr?event_id=1').json()['encoded']==settings.public_base_url+'/register/1'
    assert client('scanner').get('/api/admin/poster-qr?event_id=1').status_code==403


def test_manual_issuance_without_instagram_id():
    c=client()
    a=c.post('/api/admin/tickets',json={'event_id':1})
    b=c.post('/api/admin/tickets',json={'event_id':1})
    assert a.status_code==b.status_code==200
    assert a.json()['id']!=b.json()['id']


def test_worker_delivery_retry_without_duplicate_ticket(monkeypatch):
    import asyncio
    from app import meta
    delivered=[]
    async def profile(sender):
        return 'guest', True
    async def send(sender, text):
        delivered.append(text)
        if len(delivered)==1:
            raise RuntimeError('Temporary transport failure')
    monkeypatch.setattr(meta,'profile',profile)
    monkeypatch.setattr(meta,'send',send)
    with Session.begin() as s:
        s.add(WebhookEvent(mid='retry',sender='123',text='БИЛЕТ'))
    asyncio.run(meta.process_one())
    with Session.begin() as s:
        j=s.get(WebhookEvent,'retry')
        assert j.state=='pending' and j.reply
        j.available_at=now()-1
    asyncio.run(meta.process_one())
    with Session() as s:
        assert s.get(WebhookEvent,'retry').state=='done'
        assert s.scalar(select(func.count()).select_from(Ticket))==1
    assert delivered[0]==delivered[1]


def test_worker_profile_error_does_not_issue_ticket(monkeypatch):
    import asyncio
    import httpx
    from app import meta
    delivered=[]
    async def profile(sender):
        raise httpx.ConnectError('offline')
    async def send(sender,text):
        delivered.append(text)
    monkeypatch.setattr(meta,'profile',profile)
    monkeypatch.setattr(meta,'send',send)
    with Session.begin() as s:
        s.add(WebhookEvent(mid='error',sender='123',text='БИЛЕТ'))
    asyncio.run(meta.process_one())
    assert 'Не удалось проверить' in delivered[0]
    with Session() as s:
        assert s.scalar(select(func.count()).select_from(Ticket))==0


@pytest.mark.skipif(engine.dialect.name != 'postgresql', reason='Requires PostgreSQL row locking')
def test_postgres_concurrent_capacity_and_duplicate():
    with Session.begin() as s:
        s.get(Event,1).capacity=3
    def issue(i):
        try:
            return issue_ticket(1,str(i),'guest',True).id
        except TicketError:
            return None
    with ThreadPoolExecutor(max_workers=10) as pool:
        result=list(pool.map(issue,range(10)))
    assert sum(x is not None for x in result)==3
    with Session() as s:
        assert s.get(Event,1).issued_count==3
    with Session.begin() as s:
        s.get(Event,1).capacity=10
    with ThreadPoolExecutor(max_workers=8) as pool:
        result=list(pool.map(lambda _:issue(100),range(8)))
    assert len(set(result))==1 and result[0] is not None
