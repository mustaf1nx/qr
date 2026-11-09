import asyncio
import httpx
from sqlalchemy import select, update
from .config import settings
from .db import Session, WebhookEvent, Event, now
from .services import issue_ticket, TicketError


async def profile(sender):
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"https://graph.instagram.com/{settings.meta_api_version}/{sender}", params={"fields": "username,is_user_follow_business"}, headers={"Authorization": f"Bearer {settings.meta_access_token}"})
        r.raise_for_status()
        d = r.json()
        status = d.get("is_user_follow_business")
        return d.get("username", sender), status if isinstance(status, bool) else None


async def send(sender, text):
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"https://graph.instagram.com/{settings.meta_api_version}/{settings.meta_account_id}/messages", headers={"Authorization": f"Bearer {settings.meta_access_token}"}, json={"recipient": {"id": sender}, "message": {"text": text}})
        r.raise_for_status()


async def process_one():
    with Session.begin() as s:
        job = s.scalar(select(WebhookEvent).where(WebhookEvent.state.in_(["pending", "working"]), WebhookEvent.available_at <= now()).order_by(WebhookEvent.created_at).with_for_update(skip_locked=True).limit(1))
        if not job:
            return False
        job.state, job.available_at, job.attempts = "working", now() + 120, job.attempts + 1
        mid, sender, command, reply, attempts, created = job.mid, job.sender, job.text.upper().strip(), job.reply, job.attempts, job.created_at
    try:
        if created < now() - 23 * 3600:
            with Session.begin() as s:
                s.execute(update(WebhookEvent).where(WebhookEvent.mid == mid).values(state="failed", error="Messaging window expired"))
            return True
        if not reply:
            with Session() as s:
                e = s.scalar(select(Event).where(Event.keyword == command, Event.active.is_(True)))
            if not e:
                with Session.begin() as s:
                    s.execute(update(WebhookEvent).where(WebhookEvent.mid == mid).values(state="ignored"))
                return True
            try:
                username, follows = await profile(sender)
            except (httpx.HTTPError, ValueError):
                username, follows = sender, None
            try:
                if follows is None and e.require_follow:
                    raise TicketError("Не удалось проверить подписку. Повторите команду позже или обратитесь к организатору.")
                ticket = issue_ticket(e.id, sender, username, follows)
                reply = f"Ваш билет на {e.name}: {settings.public_base_url}/tickets/{ticket.token}"
            except TicketError as ex:
                reply = str(ex)
            with Session.begin() as s:
                s.execute(update(WebhookEvent).where(WebhookEvent.mid == mid).values(reply=reply))
        await send(sender, reply)
        with Session.begin() as s:
            s.execute(update(WebhookEvent).where(WebhookEvent.mid == mid).values(state="done", error=None))
    except Exception as ex:
        with Session.begin() as s:
            s.execute(update(WebhookEvent).where(WebhookEvent.mid == mid).values(state="failed" if attempts >= 8 else "pending", available_at=now() + min(3600, 2 ** attempts * 10), error=type(ex).__name__))
    return True


async def worker():
    while True:
        try:
            if not await process_one():
                await asyncio.sleep(2)
        except Exception:
            await asyncio.sleep(5)
