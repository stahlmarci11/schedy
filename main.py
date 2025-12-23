from datetime import datetime, date, timedelta
import calendar as cal
import re
from typing import List

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from sqlalchemy import or_
from sqlalchemy.orm import selectinload

from db import Base, engine, SessionLocal
from models import (
    Event, User, EventResponse,
    EventTimeSuggestion, EventTimeVote,
    EventInvite,
)
from auth import hash_password, verify_password, make_session_token, read_session_token

app = FastAPI()
templates = Jinja2Templates(directory="templates")
Base.metadata.create_all(bind=engine)

ALLOWED_STATUSES = {"yes", "maybe", "no"}
MAX_TEXT = 300


# -----------------------------
# Helpers
# -----------------------------
def clip(s: str | None) -> str | None:
    if s is None:
        return None
    s = s.strip()
    return s if s else None


def normalize_username_from_name(name: str) -> str:
    s = (name or "").strip().lower()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^a-z0-9_áéíóöőúüű\-]", "", s)
    return s


def get_current_user(request: Request) -> User | None:
    token = request.cookies.get("session")
    if not token:
        return None
    user_id = read_session_token(token)
    if not user_id:
        return None
    db = SessionLocal()
    try:
        return db.query(User).filter(User.id == user_id).first()
    finally:
        db.close()


def visible_events_query(db, user: User | None):
    if not user:
        return db.query(Event).filter(Event.id == -1)
    return (
        db.query(Event)
        .outerjoin(EventInvite, EventInvite.event_id == Event.id)
        .filter(or_(Event.created_by_user_id == user.id, EventInvite.user_id == user.id))
        .distinct()
    )


def is_event_visible_to_user(db, event_id: int, user: User) -> bool:
    e = db.query(Event).filter(Event.id == event_id).first()
    if not e:
        return False
    if e.created_by_user_id == user.id:
        return True
    inv = db.query(EventInvite).filter(
        EventInvite.event_id == event_id,
        EventInvite.user_id == user.id
    ).first()
    return inv is not None


def cleanup_past_events(db):
    """Delete events that already ended."""
    now = datetime.now()
    past = db.query(Event).filter(Event.ends_at < now).all()
    if past:
        for e in past:
            db.delete(e)
        db.commit()


def month_add(year: int, month: int, delta: int):
    m = (year * 12 + (month - 1)) + delta
    ny = m // 12
    nm = (m % 12) + 1
    return ny, nm


def month_in_range(y: int, m: int, min_y: int, min_m: int, max_y: int, max_m: int) -> bool:
    x = y * 12 + (m - 1)
    mn = min_y * 12 + (min_m - 1)
    mx = max_y * 12 + (max_m - 1)
    return mn <= x <= mx


def clamp_month(y: int, m: int, min_y: int, min_m: int, max_y: int, max_m: int):
    if month_in_range(y, m, min_y, min_m, max_y, max_m):
        return y, m
    x = y * 12 + (m - 1)
    mn = min_y * 12 + (min_m - 1)
    mx = max_y * 12 + (max_m - 1)
    if x < mn:
        return min_y, min_m
    return max_y, max_m


def build_month_calendar(year: int, month: int):
    c = cal.Calendar(firstweekday=0)  # Monday-first
    weeks = c.monthdayscalendar(year, month)
    month_name = cal.month_name[month]
    return {"year": year, "month": month, "month_name": month_name, "weeks": weeks}


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())  # Monday


def daterange_inclusive(d1: date, d2: date):
    cur = d1
    while cur <= d2:
        yield cur
        cur += timedelta(days=1)


def overlaps_day(e: Event, d: date) -> bool:
    """Event overlaps day if intersects [d 00:00, d+1 00:00)."""
    day_start = datetime(d.year, d.month, d.day, 0, 0)
    next_start = day_start + timedelta(days=1)
    return e.starts_at < next_start and e.ends_at > day_start


def day_status_for_user(user: User | None, events_on_day: list[Event], resp_index: dict[tuple[int, int], EventResponse]):
    if not events_on_day or not user:
        return "empty"

    saw_no = saw_pending = saw_maybe = saw_yes = False

    for e in events_on_day:
        r = resp_index.get((e.id, user.id))
        if not r:
            saw_pending = True
        else:
            if r.status == "no":
                saw_no = True
            elif r.status == "maybe":
                saw_maybe = True
            elif r.status == "yes":
                saw_yes = True

    if saw_no:
        return "no"
    if saw_pending:
        return "pending"
    if saw_maybe:
        return "maybe"
    if saw_yes:
        return "yes"
    return "pending"


def build_calendar_payload(user, events, responses, year, month, mode: str, anchor_day: date):
    """Multi-day aware grouping and per-day counts/classes for month/week/day views."""
    resp_index = {(r.event_id, r.user_id): r for r in responses}

    day_events: dict[str, list[Event]] = {}

    def add_event_to_day(d: date, e: Event):
        key = d.isoformat()
        day_events.setdefault(key, []).append(e)

    if mode == "month":
        month_start = date(year, month, 1)
        last_day = cal.monthrange(year, month)[1]
        month_end = date(year, month, last_day)

        for e in events:
            d1 = max(e.starts_at.date(), month_start)
            d2 = min(e.ends_at.date(), month_end)
            for d in daterange_inclusive(d1, d2):
                if overlaps_day(e, d):
                    add_event_to_day(d, e)

    elif mode == "week":
        ws = week_start(anchor_day)
        we = ws + timedelta(days=6)

        for e in events:
            d1 = max(e.starts_at.date(), ws)
            d2 = min(e.ends_at.date(), we)
            for d in daterange_inclusive(d1, d2):
                if overlaps_day(e, d):
                    add_event_to_day(d, e)

    else:  # day
        for e in events:
            if overlaps_day(e, anchor_day):
                add_event_to_day(anchor_day, e)

    day_classes: dict[str, str] = {}
    day_counts: dict[str, int] = {}
    for key, es in day_events.items():
        day_counts[key] = len(es)
        if user:
            day_classes[key] = day_status_for_user(user, es, resp_index)

    return day_events, day_classes, day_counts


def parse_dt(d: str, t: str) -> datetime | None:
    try:
        return datetime.fromisoformat(f"{d}T{t}")
    except Exception:
        return None


# -----------------------------
# Routes
# -----------------------------
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    user = get_current_user(request)

    today = date.today()
    min_y, min_m = today.year, today.month
    max_y, max_m = today.year + 2, 12

    mode = request.query_params.get("mode") or "month"
    if mode not in ("month", "week", "day"):
        mode = "month"

    day_str = request.query_params.get("day")
    try:
        anchor = date.fromisoformat(day_str) if day_str else today
    except ValueError:
        anchor = today

    # month navigation uses y,m
    try:
        y = int(request.query_params.get("y") or anchor.year)
        m = int(request.query_params.get("m") or anchor.month)
    except ValueError:
        y, m = anchor.year, anchor.month
    y, m = clamp_month(y, m, min_y, min_m, max_y, max_m)

    month_cal = build_month_calendar(y, m)

    prev_y, prev_m = month_add(y, m, -1)
    next_y, next_m = month_add(y, m, 1)
    can_prev = month_in_range(prev_y, prev_m, min_y, min_m, max_y, max_m)
    can_next = month_in_range(next_y, next_m, min_y, min_m, max_y, max_m)

    # ✅ computed here (no timedelta in Jinja)
    week_prev_day = (anchor - timedelta(days=7)).isoformat()
    week_next_day = (anchor + timedelta(days=7)).isoformat()
    day_prev = (anchor - timedelta(days=1)).isoformat()
    day_next = (anchor + timedelta(days=1)).isoformat()

    db = SessionLocal()
    try:
        cleanup_past_events(db)

        events = visible_events_query(db, user).order_by(Event.starts_at).all()
        event_ids = [e.id for e in events]

        responses = []
        suggestions = []
        votes = []
        invites = []

        if event_ids:
            responses = db.query(EventResponse).filter(EventResponse.event_id.in_(event_ids)).all()

            suggestions = (
                db.query(EventTimeSuggestion)
                .filter(EventTimeSuggestion.event_id.in_(event_ids))
                .options(selectinload(EventTimeSuggestion.proposed_by), selectinload(EventTimeSuggestion.votes))
                .all()
            )

            invites = db.query(EventInvite).filter(EventInvite.event_id.in_(event_ids)).all()

            suggestion_ids = [s.id for s in suggestions]
            if suggestion_ids:
                votes = db.query(EventTimeVote).filter(EventTimeVote.suggestion_id.in_(suggestion_ids)).all()

        day_events, day_classes, day_counts = build_calendar_payload(
            user=user,
            events=events,
            responses=responses,
            year=y,
            month=m,
            mode=mode,
            anchor_day=anchor,
        )

        votes_by_suggestion: dict[int, dict[str, int]] = {}
        for v in votes:
            dct = votes_by_suggestion.setdefault(v.suggestion_id, {"up": 0, "down": 0})
            if v.vote in ("up", "down"):
                dct[v.vote] += 1

        sug_by_event: dict[int, list[EventTimeSuggestion]] = {}
        for s in suggestions:
            sug_by_event.setdefault(s.event_id, []).append(s)

        # participants
        invited_user_ids_by_event: dict[int, set[int]] = {}
        for inv in invites:
            invited_user_ids_by_event.setdefault(inv.event_id, set()).add(inv.user_id)

        participant_ids = set()
        for e in events:
            participant_ids.add(e.created_by_user_id)
            participant_ids |= invited_user_ids_by_event.get(e.id, set())

        users_by_id = {}
        if participant_ids:
            us = db.query(User).filter(User.id.in_(list(participant_ids))).all()
            users_by_id = {u.id: u for u in us}

        resp_index: dict[tuple[int, int], EventResponse] = {(r.event_id, r.user_id): r for r in responses}

        # dashboard view
        dashboard = []
        for e in events:
            pids = {e.created_by_user_id} | invited_user_ids_by_event.get(e.id, set())
            participant_users = [users_by_id[pid] for pid in pids if pid in users_by_id]
            participant_users.sort(key=lambda u0: u0.name.lower())

            rows = []
            for u0 in participant_users:
                r = resp_index.get((e.id, u0.id))
                rows.append(
                    {"user_name": u0.name, "status": (r.status if r else None), "comment": (r.comment if r else None)}
                )

            sug_view = []
            ss = sorted(sug_by_event.get(e.id, []), key=lambda x: (not x.accepted, x.proposed_starts_at))
            for s in ss:
                vc = votes_by_suggestion.get(s.id, {"up": 0, "down": 0})
                sug_view.append(
                    {
                        "time_from": s.proposed_starts_at,
                        "time_to": s.proposed_ends_at,
                        "by": (s.proposed_by.name if s.proposed_by else "ismeretlen"),
                        "accepted": s.accepted,
                        "up": vc["up"],
                        "down": vc["down"],
                        "comment": s.comment,
                    }
                )

            dashboard.append(
                {
                    "id": e.id,
                    "title": e.title,
                    "starts_at": e.starts_at,
                    "ends_at": e.ends_at,
                    "description": e.description,
                    "creator_name": (e.creator.name if e.creator else None),
                    "rows": rows,
                    "suggestions": sug_view,
                }
            )

        # week/day lists
        week_days = []
        day_list_events = []
        if mode == "week":
            ws = week_start(anchor)
            week_days = [ws + timedelta(days=i) for i in range(7)]
        if mode == "day":
            day_list_events = sorted([e for e in events if overlaps_day(e, anchor)], key=lambda x: x.starts_at)

        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "user": user,
                "mode": mode,
                "anchor_day": anchor,
                "month_cal": month_cal,
                "day_classes": day_classes,
                "day_counts": day_counts,
                "day_events": day_events,
                "week_days": week_days,
                "day_list_events": day_list_events,
                "dashboard": dashboard,
                "events_exist": len(events) > 0,
                "prev_y": prev_y,
                "prev_m": prev_m,
                "next_y": next_y,
                "next_m": next_m,
                "can_prev": can_prev,
                "can_next": can_next,
                "week_prev_day": week_prev_day,
                "week_next_day": week_next_day,
                "day_prev": day_prev,
                "day_next": day_next,
            },
        )
    finally:
        db.close()


@app.get("/tasks", response_class=HTMLResponse)
def tasks(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/?error=LOGIN_REQUIRED", status_code=303)

    today = date.today()
    min_y, min_m = today.year, today.month
    max_y, max_m = today.year + 2, 12

    view = request.query_params.get("view") or "pending"
    if view not in ("new", "pending", "answered"):
        view = "pending"

    mode = request.query_params.get("mode") or "month"
    if mode not in ("month", "week", "day"):
        mode = "month"

    day_str = request.query_params.get("day")
    try:
        anchor = date.fromisoformat(day_str) if day_str else today
    except ValueError:
        anchor = today

    try:
        y = int(request.query_params.get("y") or anchor.year)
        m = int(request.query_params.get("m") or anchor.month)
    except ValueError:
        y, m = anchor.year, anchor.month
    y, m = clamp_month(y, m, min_y, min_m, max_y, max_m)

    month_cal = build_month_calendar(y, m)
    prev_y, prev_m = month_add(y, m, -1)
    next_y, next_m = month_add(y, m, 1)
    can_prev = month_in_range(prev_y, prev_m, min_y, min_m, max_y, max_m)
    can_next = month_in_range(next_y, next_m, min_y, min_m, max_y, max_m)

    week_prev_day = (anchor - timedelta(days=7)).isoformat()
    week_next_day = (anchor + timedelta(days=7)).isoformat()
    day_prev = (anchor - timedelta(days=1)).isoformat()
    day_next = (anchor + timedelta(days=1)).isoformat()

    db = SessionLocal()
    try:
        cleanup_past_events(db)

        # users for picker (exclude self)
        all_users = db.query(User).order_by(User.name.asc()).all()
        users_for_pick = [{"id": u.id, "name": u.name} for u in all_users if u.id != user.id]

        events = visible_events_query(db, user).order_by(Event.starts_at).all()
        event_ids = [e.id for e in events]

        my_responses = []
        suggestions = []
        invites = []
        if event_ids:
            my_responses = db.query(EventResponse).filter(
                EventResponse.user_id == user.id,
                EventResponse.event_id.in_(event_ids),
            ).all()

            suggestions = (
                db.query(EventTimeSuggestion)
                .filter(EventTimeSuggestion.event_id.in_(event_ids))
                .options(selectinload(EventTimeSuggestion.votes), selectinload(EventTimeSuggestion.proposed_by))
                .all()
            )

            invites = db.query(EventInvite).filter(EventInvite.event_id.in_(event_ids)).all()

        responses_by_event: dict[int, EventResponse] = {r.event_id: r for r in my_responses}

        my_votes = db.query(EventTimeVote).filter(EventTimeVote.user_id == user.id).all()
        my_vote_by_suggestion = {v.suggestion_id: v.vote for v in my_votes}

        suggestions_by_event: dict[int, list[EventTimeSuggestion]] = {}
        for s in suggestions:
            suggestions_by_event.setdefault(s.event_id, []).append(s)

        invited_user_ids_by_event: dict[int, set[int]] = {}
        for inv in invites:
            invited_user_ids_by_event.setdefault(inv.event_id, set()).add(inv.user_id)

        # lookup invitees for display
        invite_user_ids = set()
        for e in events:
            invite_user_ids |= invited_user_ids_by_event.get(e.id, set())

        users_by_id = {}
        if invite_user_ids:
            us = db.query(User).filter(User.id.in_(list(invite_user_ids))).all()
            users_by_id = {u.id: u for u in us}

        def make_event_view(e: Event):
            r = responses_by_event.get(e.id)

            invitees = []
            for uid in sorted(list(invited_user_ids_by_event.get(e.id, set()))):
                u0 = users_by_id.get(uid)
                if u0:
                    invitees.append({"name": u0.name, "username": u0.username})

            sug_list = []
            for s in suggestions_by_event.get(e.id, []):
                up = sum(1 for vv in s.votes if vv.vote == "up")
                down = sum(1 for vv in s.votes if vv.vote == "down")
                sug_list.append(
                    {
                        "id": s.id,
                        "proposed_starts_at": s.proposed_starts_at,
                        "proposed_ends_at": s.proposed_ends_at,
                        "proposed_by_name": (s.proposed_by.name if s.proposed_by else None),
                        "accepted": s.accepted,
                        "my_vote": my_vote_by_suggestion.get(s.id),
                        "up": up,
                        "down": down,
                        "comment": s.comment,
                    }
                )
            sug_list = sorted(sug_list, key=lambda x: (not x["accepted"], x["proposed_starts_at"]))

            return {
                "id": e.id,
                "title": e.title,
                "starts_at": e.starts_at,
                "ends_at": e.ends_at,
                "description": e.description,
                "creator_name": (e.creator.name if e.creator else None),
                "is_creator": (e.created_by_user_id == user.id),
                "my_status": (r.status if r else None),
                "my_comment": (r.comment if r else None),
                "suggestions": sug_list,
                "invitees": invitees,
            }

        pending_events = []
        answered_events = []
        for e in events:
            if e.id in responses_by_event:
                answered_events.append(make_event_view(e))
            else:
                pending_events.append(make_event_view(e))

        day_events, day_classes, day_counts = build_calendar_payload(
            user=user,
            events=events,
            responses=my_responses,
            year=y,
            month=m,
            mode="month",  # tasks page calendar stays monthly
            anchor_day=anchor,
        )

        return templates.TemplateResponse(
            "tasks.html",
            {
                "request": request,
                "user": user,
                "users": users_for_pick,
                "view": view,
                "mode": mode,
                "anchor_day": anchor,
                "month_cal": month_cal,
                "day_classes": day_classes,
                "day_counts": day_counts,
                "day_events": day_events,
                "week_prev_day": week_prev_day,
                "week_next_day": week_next_day,
                "day_prev": day_prev,
                "day_next": day_next,
                "pending_events": pending_events,
                "answered_events": answered_events,
                "prev_y": prev_y,
                "prev_m": prev_m,
                "next_y": next_y,
                "next_m": next_m,
                "can_prev": can_prev,
                "can_next": can_next,
                "day_filter": request.query_params.get("day"),
            },
        )
    finally:
        db.close()


# -----------------------------
# Auth
# -----------------------------
@app.post("/register")
def register(name: str = Form(...), email: str = Form(...), password: str = Form(...)):
    if len(password.encode("utf-8")) > 72:
        return RedirectResponse(url="/?error=PW_TOO_LONG", status_code=303)

    name = clip(name) or ""
    if not name:
        return RedirectResponse(url="/?error=NAME_REQUIRED", status_code=303)

    username = normalize_username_from_name(name)

    db = SessionLocal()
    try:
        if db.query(User).filter(User.username == username).first():
            return RedirectResponse(url="/?error=USERNAME_EXISTS", status_code=303)
        if db.query(User).filter(User.email == email).first():
            return RedirectResponse(url="/?error=EMAIL_EXISTS", status_code=303)

        user = User(name=name, username=username, email=email, password_hash=hash_password(password))
        db.add(user)
        db.commit()
        db.refresh(user)

        resp = RedirectResponse(url="/", status_code=303)
        resp.set_cookie("session", make_session_token(user.id), httponly=True, samesite="lax")
        return resp
    finally:
        db.close()


@app.post("/login")
def login(name: str = Form(...), password: str = Form(...)):
    if len(password.encode("utf-8")) > 72:
        return RedirectResponse(url="/?error=PW_TOO_LONG", status_code=303)

    name = clip(name) or ""
    if not name:
        return RedirectResponse(url="/?error=NAME_REQUIRED", status_code=303)

    username = normalize_username_from_name(name)

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == username).first()
        if not user or not verify_password(password, user.password_hash):
            return RedirectResponse(url="/?error=BAD_LOGIN", status_code=303)

        resp = RedirectResponse(url="/", status_code=303)
        resp.set_cookie("session", make_session_token(user.id), httponly=True, samesite="lax")
        return resp
    finally:
        db.close()


@app.post("/logout")
def logout():
    resp = RedirectResponse(url="/", status_code=303)
    resp.delete_cookie("session")
    return resp


# -----------------------------
# Events
# -----------------------------
@app.post("/add")
def add_event(
    request: Request,
    title: str = Form(...),
    start_date: str = Form(...),
    start_time: str = Form(...),
    end_date: str = Form(...),
    end_time: str = Form(...),
    description: str = Form(""),
    invitee_ids: List[int] = Form(default=[]),
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/?error=LOGIN_REQUIRED", status_code=303)

    title = clip(title) or ""
    description = clip(description)
    if description and len(description) > MAX_TEXT:
        return RedirectResponse(url="/tasks?error=DESC_TOO_LONG", status_code=303)

    starts_at = parse_dt(start_date, start_time)
    ends_at = parse_dt(end_date, end_time)
    if not starts_at or not ends_at or ends_at <= starts_at:
        return RedirectResponse(url="/tasks?error=BAD_RANGE", status_code=303)

    db = SessionLocal()
    try:
        e = Event(
            title=title,
            starts_at=starts_at,
            ends_at=ends_at,
            description=description,
            created_by_user_id=user.id,
        )
        db.add(e)
        db.commit()
        db.refresh(e)

        # ✅ organizer auto yes
        db.add(EventResponse(event_id=e.id, user_id=user.id, status="yes", comment=None))
        db.commit()

        # invites
        if invitee_ids:
            picked = db.query(User).filter(User.id.in_(invitee_ids)).all()
            for u0 in picked:
                if u0.id == user.id:
                    continue
                existing = db.query(EventInvite).filter(
                    EventInvite.event_id == e.id,
                    EventInvite.user_id == u0.id
                ).first()
                if not existing:
                    db.add(EventInvite(event_id=e.id, user_id=u0.id, invited_by_user_id=user.id))
            db.commit()
    finally:
        db.close()

    return RedirectResponse(url="/tasks?view=pending", status_code=303)


@app.post("/invite")
def invite_to_event(
    request: Request,
    event_id: int = Form(...),
    invitee_ids: List[int] = Form(default=[]),
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/?error=LOGIN_REQUIRED", status_code=303)

    db = SessionLocal()
    try:
        e = db.query(Event).filter(Event.id == event_id).first()
        if not e:
            return RedirectResponse(url="/tasks?error=NO_EVENT", status_code=303)
        if e.created_by_user_id != user.id:
            return RedirectResponse(url="/tasks?error=NOT_CREATOR", status_code=303)

        if invitee_ids:
            picked = db.query(User).filter(User.id.in_(invitee_ids)).all()
            for u0 in picked:
                if u0.id == user.id:
                    continue
                existing = db.query(EventInvite).filter(
                    EventInvite.event_id == event_id,
                    EventInvite.user_id == u0.id
                ).first()
                if not existing:
                    db.add(EventInvite(event_id=event_id, user_id=u0.id, invited_by_user_id=user.id))
            db.commit()
    finally:
        db.close()

    return RedirectResponse(url="/tasks", status_code=303)


@app.post("/respond")
def respond(
    request: Request,
    event_id: int = Form(...),
    status: str = Form(...),
    comment: str = Form(""),
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/?error=LOGIN_REQUIRED", status_code=303)

    status = (status or "").strip().lower()
    if status not in ALLOWED_STATUSES:
        return RedirectResponse(url="/tasks?error=BAD_STATUS", status_code=303)

    comment = clip(comment)
    if comment and len(comment) > MAX_TEXT:
        return RedirectResponse(url="/tasks?error=COMMENT_TOO_LONG", status_code=303)

    db = SessionLocal()
    try:
        if not is_event_visible_to_user(db, event_id, user):
            return RedirectResponse(url="/tasks?error=FORBIDDEN", status_code=303)

        existing = db.query(EventResponse).filter(
            EventResponse.event_id == event_id,
            EventResponse.user_id == user.id
        ).first()

        if existing:
            existing.status = status
            existing.comment = comment
        else:
            db.add(EventResponse(event_id=event_id, user_id=user.id, status=status, comment=comment))

        db.commit()
    finally:
        db.close()

    return RedirectResponse(url="/tasks", status_code=303)


# -----------------------------
# Time suggestions
# -----------------------------
@app.post("/suggest_time")
def suggest_time(
    request: Request,
    event_id: int = Form(...),
    start_date: str = Form(...),
    start_time: str = Form(...),
    end_date: str = Form(...),
    end_time: str = Form(...),
    comment: str = Form(""),
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/?error=LOGIN_REQUIRED", status_code=303)

    p_start = parse_dt(start_date, start_time)
    p_end = parse_dt(end_date, end_time)
    if not p_start or not p_end or p_end <= p_start:
        return RedirectResponse(url="/tasks?error=BAD_RANGE", status_code=303)

    comment = clip(comment)
    if comment and len(comment) > MAX_TEXT:
        return RedirectResponse(url="/tasks?error=COMMENT_TOO_LONG", status_code=303)

    db = SessionLocal()
    try:
        if not is_event_visible_to_user(db, event_id, user):
            return RedirectResponse(url="/tasks?error=FORBIDDEN", status_code=303)

        # create suggestion if not exists
        existing = db.query(EventTimeSuggestion).filter(
            EventTimeSuggestion.event_id == event_id,
            EventTimeSuggestion.proposed_starts_at == p_start,
            EventTimeSuggestion.proposed_ends_at == p_end,
        ).first()

        if not existing:
            db.add(EventTimeSuggestion(
                event_id=event_id,
                proposed_by_user_id=user.id,
                proposed_starts_at=p_start,
                proposed_ends_at=p_end,
                comment=comment,
            ))

        # ✅ proposer auto "no" for current schedule
        r = db.query(EventResponse).filter(
            EventResponse.event_id == event_id,
            EventResponse.user_id == user.id
        ).first()
        if r:
            r.status = "no"
        else:
            db.add(EventResponse(event_id=event_id, user_id=user.id, status="no", comment=None))

        db.commit()
    finally:
        db.close()

    return RedirectResponse(url="/tasks", status_code=303)


@app.post("/vote_time")
def vote_time(
    request: Request,
    suggestion_id: int = Form(...),
    vote: str = Form(...),
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/?error=LOGIN_REQUIRED", status_code=303)

    vote = (vote or "").strip().lower()
    if vote not in ("up", "down"):
        return RedirectResponse(url="/tasks?error=BAD_VOTE", status_code=303)

    db = SessionLocal()
    try:
        s = db.query(EventTimeSuggestion).filter(EventTimeSuggestion.id == suggestion_id).first()
        if not s:
            return RedirectResponse(url="/tasks?error=NO_SUGGESTION", status_code=303)

        if not is_event_visible_to_user(db, s.event_id, user):
            return RedirectResponse(url="/tasks?error=FORBIDDEN", status_code=303)

        existing = db.query(EventTimeVote).filter(
            EventTimeVote.suggestion_id == suggestion_id,
            EventTimeVote.user_id == user.id
        ).first()

        if existing:
            if existing.vote == vote:
                db.delete(existing)  # toggle off
            else:
                existing.vote = vote
        else:
            db.add(EventTimeVote(suggestion_id=suggestion_id, user_id=user.id, vote=vote))

        db.commit()
    finally:
        db.close()

    return RedirectResponse(url="/tasks", status_code=303)


@app.post("/accept_time")
def accept_time(request: Request, suggestion_id: int = Form(...)):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/?error=LOGIN_REQUIRED", status_code=303)

    db = SessionLocal()
    try:
        s = db.query(EventTimeSuggestion).filter(EventTimeSuggestion.id == suggestion_id).first()
        if not s:
            return RedirectResponse(url="/tasks?error=NO_SUGGESTION", status_code=303)

        e = db.query(Event).filter(Event.id == s.event_id).first()
        if not e:
            return RedirectResponse(url="/tasks?error=NO_EVENT", status_code=303)

        if e.created_by_user_id != user.id:
            return RedirectResponse(url="/tasks?error=NOT_CREATOR", status_code=303)

        # ✅ apply tól–ig
        e.starts_at = s.proposed_starts_at
        e.ends_at = s.proposed_ends_at

        # mark accepted (only one)
        all_s = db.query(EventTimeSuggestion).filter(EventTimeSuggestion.event_id == e.id).all()
        for x in all_s:
            x.accepted = (x.id == s.id)

        # require everyone to answer again
        db.query(EventResponse).filter(EventResponse.event_id == e.id).delete()

        # ✅ organizer auto yes after accepting
        db.add(EventResponse(event_id=e.id, user_id=user.id, status="yes", comment=None))

        db.commit()
    finally:
        db.close()

    return RedirectResponse(url="/tasks", status_code=303)
