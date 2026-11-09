import asyncio
import contextlib
import hashlib
import hmac
import io
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse
import qrcode
from fastapi import FastAPI, Request, Response, HTTPException, Depends
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator
from pwdlib import PasswordHash
from sqlalchemy import select, update, func, delete, text
from sqlalchemy.exc import IntegrityError
from .config import settings
from .db import Session, Event, Participant, Ticket, Staff, LoginSession, LoginAttempt, Checkin, WebhookEvent, now
from .services import issue_ticket, TicketError
from .meta import worker

passwords = PasswordHash.recommended()
dummy_hash = passwords.hash(secrets.token_urlsafe(24))


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(worker()) if settings.meta_access_token else None
    yield
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="Event Pass", lifespan=lifespan)


@app.middleware("http")
async def security(request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path != "/webhooks/instagram":
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != settings.public_base_url.rstrip("/"):
            return PlainTextResponse("Forbidden origin", status_code=403)
        if request.headers.get("x-requested-with") != "EventPass":
            return PlainTextResponse("Missing request header", status_code=403)
    r = await call_next(request)
    r.headers["Cache-Control"] = "no-store"
    r.headers["Referrer-Policy"] = "no-referrer"
    r.headers["X-Content-Type-Options"] = "nosniff"
    r.headers["X-Frame-Options"] = "DENY"
    r.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
    return r


def staff(request: Request):
    token = request.cookies.get("session", "")
    with Session() as s:
        login = s.get(LoginSession, hashlib.sha256(token.encode()).hexdigest())
        u = s.get(Staff, login.staff_id) if login and login.expires_at > now() else None
        if not u or not u.active:
            raise HTTPException(401, "Войдите в аккаунт")
        return u


def admin(u=Depends(staff)):
    if u.role != "admin":
        raise HTTPException(403, "Нужны права организатора")
    return u


class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


@app.post("/api/auth/login")
def login(body: Credentials, request: Request, response: Response):
    key = body.username.lower()
    with Session.begin() as s:
        s.execute(delete(LoginAttempt).where(LoginAttempt.created_at < now() - 900))
        count = s.scalar(select(func.count()).select_from(LoginAttempt).where(LoginAttempt.key == key))
        if count >= 10:
            raise HTTPException(429, "Слишком много попыток. Повторите через 15 минут")
        s.add(LoginAttempt(key=key))
    with Session.begin() as s:
        u = s.scalar(select(Staff).where(Staff.username == body.username))
        valid = passwords.verify(body.password, u.password_hash if u else dummy_hash)
        if not valid or not u or not u.active:
            raise HTTPException(401, "Неверный логин или пароль")
        s.execute(delete(LoginAttempt).where(LoginAttempt.key == key))
        s.execute(delete(LoginSession).where(LoginSession.expires_at < now()))
        token = secrets.token_urlsafe(32)
        s.add(LoginSession(token_hash=hashlib.sha256(token.encode()).hexdigest(), staff_id=u.id, expires_at=now()+43200))
        response.set_cookie("session", token, httponly=True, secure=settings.cookie_secure, samesite="strict", max_age=43200)
        return {"username": u.username, "role": u.role, "id": u.id}


@app.get("/api/auth/me")
def me(u=Depends(staff)):
    return {"username": u.username, "role": u.role, "id": u.id}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    with Session.begin() as s:
        s.execute(delete(LoginSession).where(LoginSession.token_hash == hashlib.sha256(request.cookies.get("session", "").encode()).hexdigest()))
    response.delete_cookie("session")
    return {"ok": True}


class EventInput(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    starts_at: int
    expires_at: int
    capacity: int = Field(ge=1, le=1000000)
    keyword: str = Field(min_length=1, max_length=50)
    require_follow: bool = True
    active: bool = True

    @model_validator(mode="after")
    def validate_event(self):
        self.keyword = self.keyword.strip().upper()
        if not self.keyword or self.expires_at <= self.starts_at:
            raise ValueError("Укажите команду и окончание после начала")
        return self


def event_dict(e):
    return {k: getattr(e, k) for k in ("id", "name", "starts_at", "expires_at", "capacity", "issued_count", "active", "keyword", "require_follow")}


@app.get("/api/events")
def events(u=Depends(staff)):
    with Session() as s:
        return [event_dict(e) for e in s.scalars(select(Event).order_by(Event.id.desc()))]


@app.post("/api/events")
def create_event(b: EventInput, u=Depends(admin)):
    try:
        with Session.begin() as s:
            e = Event(**b.model_dump())
            s.add(e)
            s.flush()
            return event_dict(e)
    except IntegrityError:
        raise HTTPException(409, "Эта команда уже занята")


@app.put("/api/events/{event_id}")
def edit_event(event_id: int, b: EventInput, u=Depends(admin)):
    try:
        with Session.begin() as s:
            e = s.scalar(select(Event).where(Event.id == event_id).with_for_update())
            if not e:
                raise HTTPException(404)
            if b.capacity < e.issued_count:
                raise HTTPException(409, "Вместимость меньше числа выданных билетов")
            for k, v in b.model_dump().items():
                setattr(e, k, v)
            s.flush()
            return event_dict(e)
    except IntegrityError:
        raise HTTPException(409, "Эта команда уже занята")


@app.get("/api/admin/stats")
def stats(event_id: int, u=Depends(admin)):
    with Session() as s:
        counts = dict(s.execute(select(Ticket.status, func.count()).where(Ticket.event_id == event_id).group_by(Ticket.status)).all())
        return {"total": sum(counts.values()), "used": counts.get("used", 0), "issued": counts.get("issued", 0), "revoked": counts.get("revoked", 0)}


@app.get("/api/admin/tickets")
def tickets(event_id: int, q: str = "", offset: int = 0, u=Depends(admin)):
    with Session() as s:
        rows = s.execute(select(Ticket, Participant).join(Participant).where(Ticket.event_id == event_id, Participant.username.contains(q[:100], autoescape=True)).order_by(Ticket.id.desc()).offset(max(0, offset)).limit(100)).all()
        return [{"id": t.id, "username": p.username, "status": t.status, "redeemed_at": t.redeemed_at, "url": f"/tickets/{t.token}"} for t, p in rows]


class ManualInput(BaseModel):
    event_id: int
    igsid: str = Field(min_length=1, max_length=100)
    username: str = Field(min_length=1, max_length=100)


@app.post("/api/admin/tickets")
def manual(b: ManualInput, u=Depends(admin)):
    try:
        t = issue_ticket(b.event_id, b.igsid, b.username, None, override=True)
        return {"url": f"/tickets/{t.token}", "id": t.id}
    except TicketError as ex:
        raise HTTPException(409, str(ex))


@app.post("/api/admin/tickets/{ticket_id}/revoke")
def revoke(ticket_id: int, u=Depends(admin)):
    with Session.begin() as s:
        result = s.execute(update(Ticket).where(Ticket.id == ticket_id, Ticket.status == "issued").values(status="revoked"))
        if not result.rowcount:
            raise HTTPException(409, "Билет уже использован или отозван")
    return {"ok": True}


class StaffInput(Credentials):
    role: str = Field(pattern="^(admin|scanner)$")
    password: str = Field(min_length=12, max_length=200)


@app.get("/api/admin/staff")
def staff_list(u=Depends(admin)):
    with Session() as s:
        return [{"id": x.id, "username": x.username, "role": x.role, "active": x.active} for x in s.scalars(select(Staff))]


@app.post("/api/admin/staff")
def add_staff(b: StaffInput, u=Depends(admin)):
    try:
        with Session.begin() as s:
            s.add(Staff(username=b.username, password_hash=passwords.hash(b.password), role=b.role))
    except IntegrityError:
        raise HTTPException(409, "Логин занят")
    return {"ok": True}


@app.post("/api/admin/staff/{staff_id}/disable")
def disable_staff(staff_id: int, u=Depends(admin)):
    if staff_id == u.id:
        raise HTTPException(409, "Нельзя отключить свой аккаунт")
    with Session.begin() as s:
        s.execute(update(Staff).where(Staff.id == staff_id).values(active=False))
        s.execute(delete(LoginSession).where(LoginSession.staff_id == staff_id))
    return {"ok": True}


class Scan(BaseModel):
    event_id: int
    token: str = Field(min_length=1, max_length=500)


@app.post("/api/scanner/redeem")
def redeem(b: Scan, u=Depends(staff)):
    token = b.token.strip()
    if token.startswith(("http://", "https://")):
        parsed = urlparse(token)
        if parsed.netloc != urlparse(settings.public_base_url).netloc or not parsed.path.startswith("/tickets/"):
            raise HTTPException(400, "QR другого сервиса")
        token = parsed.path.split("/")[-1]
    with Session.begin() as s:
        e = s.get(Event, b.event_id)
        t = s.scalar(select(Ticket).where(Ticket.token == token))
        result = "invalid"
        if t and t.event_id != b.event_id:
            result = "wrong_event"
        elif t and e:
            # Conditional UPDATE is the authority, never a read-then-write status check.
            if not e.active or e.expires_at <= now():
                result = "expired"
            elif e.starts_at > now():
                result = "not_started"
            else:
                updated = s.execute(update(Ticket).where(Ticket.id == t.id, Ticket.status == "issued").values(status="used", redeemed_at=now(), redeemed_by=u.id).returning(Ticket.id)).scalar()
                if updated:
                    result = "accepted"
                else:
                    s.refresh(t)
                    result = t.status
        s.add(Checkin(event_id=b.event_id, ticket_id=t.id if t else None, staff_id=u.id, result=result))
        p = s.get(Participant, t.participant_id) if t and t.event_id == b.event_id else None
        return {"result": result, "username": p.username if p else None, "ticket_id": t.id if p else None}


@app.get("/api/admin/checkins")
def checkins(event_id: int, u=Depends(admin)):
    with Session() as s:
        return [{"id": c.id, "ticket_id": c.ticket_id, "staff_id": c.staff_id, "result": c.result, "created_at": c.created_at} for c in s.scalars(select(Checkin).where(Checkin.event_id == event_id).order_by(Checkin.id.desc()).limit(100))]


@app.get("/api/tickets/{token}")
def public_ticket(token: str):
    with Session() as s:
        t = s.scalar(select(Ticket).where(Ticket.token == token))
        if not t:
            raise HTTPException(404, "Билет не найден")
        e, p = s.get(Event, t.event_id), s.get(Participant, t.participant_id)
        status = "expired" if t.status == "issued" and (e.expires_at <= now() or not e.active) else t.status
        return {"id": t.id, "event": e.name, "username": p.username, "status": status, "starts_at": e.starts_at, "expires_at": e.expires_at}


def qr_response(value):
    out = io.BytesIO()
    qrcode.make(value).save(out, format="PNG")
    return Response(out.getvalue(), media_type="image/png")


@app.get("/api/tickets/{token}/qr")
def ticket_qr(token: str):
    public_ticket(token)
    return qr_response(f"{settings.public_base_url}/tickets/{token}")


@app.get("/api/admin/poster-qr")
def poster_qr(u=Depends(admin)):
    if not settings.instagram_username:
        raise HTTPException(409, "Instagram ещё не настроен")
    return qr_response(f"https://ig.me/m/{settings.instagram_username}")


@app.get("/api/admin/integration")
def integration(u=Depends(admin)):
    with Session() as s:
        failed = s.scalar(select(func.count()).select_from(WebhookEvent).where(WebhookEvent.state == "failed"))
    return {"configured": all((settings.meta_app_secret, settings.meta_verify_token, settings.meta_access_token, settings.meta_account_id)), "instagram": settings.instagram_username, "failed_messages": failed}


@app.get("/webhooks/instagram")
def verify(request: Request):
    q = request.query_params
    if settings.meta_verify_token and q.get("hub.mode") == "subscribe" and hmac.compare_digest(q.get("hub.verify_token", ""), settings.meta_verify_token):
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(403)


@app.post("/webhooks/instagram")
async def webhook(request: Request):
    if not settings.meta_app_secret:
        raise HTTPException(503, "Meta not configured")
    raw = await request.body()
    if len(raw) > 1_000_000:
        raise HTTPException(413)
    expected = "sha256=" + hmac.new(settings.meta_app_secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, request.headers.get("x-hub-signature-256", "")):
        raise HTTPException(403)
    try:
        payload = await request.json()
        if payload.get("object") != "instagram":
            return {"ok": True}
        for entry in payload.get("entry", []):
            if str(entry.get("id")) != settings.meta_account_id:
                continue
            for item in entry.get("messaging", []):
                m = item.get("message", {})
                sender = str(item.get("sender", {}).get("id", ""))
                if m.get("is_echo") or not m.get("mid") or not sender or not isinstance(m.get("text"), str):
                    continue
                try:
                    with Session.begin() as s:
                        s.add(WebhookEvent(mid=m["mid"], sender=sender, text=m["text"][:1000]))
                except IntegrityError:
                    pass
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(400, "Malformed webhook")
    return {"ok": True}


@app.get("/health")
def health():
    with Session() as s:
        s.execute(text("SELECT 1"))
    return {"status": "ok"}


dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if dist.exists():
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")


@app.get("/{path:path}")
def spa(path: str):
    if path.startswith(("api/", "webhooks/")):
        raise HTTPException(404)
    if path in ("manifest.webmanifest", "sw.js", "icon.svg") and (dist / path).exists():
        return FileResponse(dist / path)
    if (dist / "index.html").exists():
        return FileResponse(dist / "index.html")
    raise HTTPException(503, "Build frontend first")
