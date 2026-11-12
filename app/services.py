import secrets
from sqlalchemy import select, update, or_
from sqlalchemy.exc import IntegrityError
from .db import Session, Event, Participant, Ticket, now


class TicketError(Exception):
    pass


def issue_ticket(event_id, igsid, username, follows, override=False, first_name=None, last_name=None, phone=None):
    # Event row is locked BEFORE counting/creating: serializes capacity and duplicates.
    with Session.begin() as s:
        e = s.scalar(select(Event).where(Event.id == event_id).with_for_update())
        if not e or not e.active or (e.expires_at is not None and e.expires_at <= now()):
            raise TicketError("Регистрация закрыта")
        p = s.scalar(select(Participant).where(Participant.igsid == igsid))
        if not p:
            try:
                with s.begin_nested():
                    p = Participant(igsid=igsid, username=username, follows=follows)
                    s.add(p)
                    s.flush()
            except IntegrityError:
                p = s.scalar(select(Participant).where(Participant.igsid == igsid))
        existing = s.scalar(select(Ticket).where(Ticket.event_id == event_id, Ticket.participant_id == p.id))
        if existing:
            return existing
        if e.require_follow and follows is not True and not override:
            raise TicketError("Подпишитесь и отправьте команду ещё раз" if follows is False else "Не удалось проверить подписку. Повторите позже")
        reserved = s.execute(update(Event).where(Event.id == event_id, or_(Event.capacity.is_(None), Event.issued_count < Event.capacity)).values(issued_count=Event.issued_count + 1))
        if not reserved.rowcount:
            raise TicketError("Все билеты уже выданы")
        p.username, p.follows = username, follows
        p.first_name, p.last_name, p.phone = first_name, last_name, phone
        t = Ticket(event_id=event_id, participant_id=p.id, token=secrets.token_urlsafe(32))
        s.add(t)
        s.flush()
        return t
