from datetime import datetime, date
import calendar as cal

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from sqlalchemy import or_
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


def clip(s: str | None) -> str | None:
    if s is None:
        return None
    s = s.strip()
    return s if s else None


def parse_usernames_csv(s: str | None) -> list[str]:
    if not s:
        return []
    parts = [p.strip().lower() for p in s.split(",")]
    parts = [p for p in parts if p]
    # unique, keep order
    out = []
    seen = set()
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


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


def build_month_calendar(year: int, month: int):
    c = cal.Calendar(firstweekday=0)  # Monday-first
    weeks = c.monthdayscalendar(year, month)
    month_name = cal.month_name[month]
    return {"year": year, "month": month, "month_name": month_name, "weeks": weeks}


def is_event_visible_to_user(db, event_id: int, user: User) -> bool:
    e = db.query(Event).filter(Event.id == event_id).first()
    if not e:
        return False
    if e.created_by_user_id == user.id:
        return True
    inv = db.query(EventInvite).filter(EventInvite.event_id == event_id, EventInvite.user_id == user.id).first()
    return inv is not None


def visible_events_query(db, user: User | None):
    if not user:
        # Privát rendszer: bejelentkezés nélkül nem mutatunk eseményeket
        return db.query(Event).filter(Event.id == -1)
    return (
        db.query(Event)
        .outerjoin(EventInvite, EventInvite.event_id == Event.id)
        .filter(or_(Event.created_by_user_id == user.id, EventInvite.user_id == user.id))
        .distinct()
    )


def day_status_for_user(user: User | None, events_on_day: list[Event], resp_index: dict[tuple[int, int], EventResponse]):
    if not events_on_day:
        return "empty"
    if not user:
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


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    user = get_current_user(request)
    today = date.today()
    year, month = today.year, today.month
    month_cal = build_month_calendar(year, month)

    db = SessionLocal()
    try:
        events = visible_events_query(db, user).order_by(Event.starts_at).all()

        # résztvevők és válaszok csak a látható eseményekhez
        event_ids = [e.id for e in events]

        responses = []
        suggestions = []
        votes = []
        invites = []
        if event_ids:
            responses = db.query(EventResponse).filter(EventResponse.event_id.in_(event_ids)).all()
            suggestions = db.query(EventTimeSuggestion).filter(EventTimeSuggestion.event_id.in_(event_ids)).all()
            votes = db.query(EventTimeVote).all()  # vote suggestion_id alapján lesz összesítve
            invites = db.query(EventInvite).filter(EventInvite.event_id.in_(event_ids)).all()

        resp_index: dict[tuple[int, int], EventResponse] = {}
        for r in responses:
            resp_index[(r.event_id, r.user_id)] = r

        # invites grouped by event -> user list
        invited_user_ids_by_event: dict[int, set[int]] = {}
        for inv in invites:
            invited_user_ids_by_event.setdefault(inv.event_id, set()).add(inv.user_id)

        # users lookup for participants display
        participant_ids = set()
        for e in events:
            participant_ids.add(e.created_by_user_id)
            participant_ids |= invited_user_ids_by_event.get(e.id, set())

        users_by_id = {}
        if participant_ids:
            us = db.query(User).filter(User.id.in_(list(participant_ids))).all()
            users_by_id = {u.id: u for u in us}

        # calendar
        events_by_date: dict[date, list[Event]] = {}
        for e in events:
            events_by_date.setdefault(e.starts_at.date(), []).append(e)

        day_classes: dict[str, str] = {}
        if user:
            for d, es in events_by_date.items():
                if d.year == year and d.month == month:
                    day_classes[d.isoformat()] = day_status_for_user(user, es, resp_index)

        # votes grouped by suggestion (up/down)
        votes_by_suggestion: dict[int, dict[str, int]] = {}
        for v in votes:
            dct = votes_by_suggestion.setdefault(v.suggestion_id, {"up": 0, "down": 0})
            if v.vote in ("up", "down"):
                dct[v.vote] += 1

        sug_by_event: dict[int, list[EventTimeSuggestion]] = {}
        for s in suggestions:
            sug_by_event.setdefault(s.event_id, []).append(s)

        dashboard = []
        for e in events:
            # participants = creator + invited
            pids = set()
            pids.add(e.created_by_user_id)
            pids |= invited_user_ids_by_event.get(e.id, set())

            participant_users = [users_by_id[pid] for pid in pids if pid in users_by_id]
            participant_users.sort(key=lambda u: u.name.lower())

            rows = []
            for u in participant_users:
                r = resp_index.get((e.id, u.id))
                rows.append(
                    {
                        "user_name": u.name,
                        "status": (r.status if r else None),
                        "comment": (r.comment if r else None),
                    }
                )

            sug_view = []
            ss = sorted(sug_by_event.get(e.id, []), key=lambda x: (not x.accepted, x.proposed_starts_at))
            for s in ss:
                vc = votes_by_suggestion.get(s.id, {"up": 0, "down": 0})
                sug_view.append(
                    {
                        "time": s.proposed_starts_at,
                        "by": (s.proposed_by.name if s.proposed_by else "ismeretlen"),
                        "accepted": s.accepted,
                        "up": vc["up"],
                        "down": vc["down"],
                    }
                )

            dashboard.append(
                {
                    "id": e.id,
                    "title": e.title,
                    "starts_at": e.starts_at,
                    "description": e.description,
                    "creator_name": (e.creator.name if e.creator else None),
                    "rows": rows,
                    "suggestions": sug_view,
                }
            )

        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "user": user,
                "month_cal": month_cal,
                "day_classes": day_classes,
                "dashboard": dashboard,
                "events_exist": len(events) > 0,
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
    year, month = today.year, today.month
    month_cal = build_month_calendar(year, month)

    db = SessionLocal()
    try:
        events = visible_events_query(db, user).order_by(Event.starts_at).all()
        event_ids = [e.id for e in events]

        my_responses = []
        suggestions = []
        invites = []
        if event_ids:
            my_responses = db.query(EventResponse).filter(EventResponse.user_id == user.id, EventResponse.event_id.in_(event_ids)).all()
            suggestions = db.query(EventTimeSuggestion).filter(EventTimeSuggestion.event_id.in_(event_ids)).all()
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

        # user lookup for invite list
        invite_user_ids = set()
        for e in events:
            invite_user_ids |= invited_user_ids_by_event.get(e.id, set())
        users_by_id = {}
        if invite_user_ids:
            us = db.query(User).filter(User.id.in_(list(invite_user_ids))).all()
            users_by_id = {u.id: u for u in us}

        def make_event_view(e: Event):
            r = responses_by_event.get(e.id)

            # invitees for display
            invitees = []
            for uid in sorted(list(invited_user_ids_by_event.get(e.id, set()))):
                u = users_by_id.get(uid)
                if u:
                    invitees.append({"name": u.name, "username": u.username})

            sug_list = []
            for s in suggestions_by_event.get(e.id, []):
                up = sum(1 for vv in s.votes if vv.vote == "up")
                down = sum(1 for vv in s.votes if vv.vote == "down")
                sug_list.append(
                    {
                        "id": s.id,
                        "proposed_starts_at": s.proposed_starts_at,
                        "proposed_by_name": (s.proposed_by.name if s.proposed_by else None),
                        "accepted": s.accepted,
                        "my_vote": my_vote_by_suggestion.get(s.id),
                        "up": up,
                        "down": down,
                    }
                )
            sug_list = sorted(sug_list, key=lambda x: (not x["accepted"], x["proposed_starts_at"]))

            return {
                "id": e.id,
                "title": e.title,
                "starts_at": e.starts_at,
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

        # calendar classes
        events_by_date: dict[date, list[Event]] = {}
        for e in events:
            events_by_date.setdefault(e.starts_at.date(), []).append(e)

        resp_index: dict[tuple[int, int], EventResponse] = {}
        for r in my_responses:
            resp_index[(r.event_id, user.id)] = r

        day_classes: dict[str, str] = {}
        for d, es in events_by_date.items():
            if d.year == year and d.month == month:
                day_classes[d.isoformat()] = day_status_for_user(user, es, resp_index)

        return templates.TemplateResponse(
            "tasks.html",
            {
                "request": request,
                "user": user,
                "month_cal": month_cal,
                "day_classes": day_classes,
                "pending_events": pending_events,
                "answered_events": answered_events,
            },
        )
    finally:
        db.close()


@app.post("/register")
def register(
    name: str = Form(...),
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
):
    if len(password.encode("utf-8")) > 72:
        return RedirectResponse(url="/?error=PW_TOO_LONG", status_code=303)

    name = clip(name) or ""
    username = (clip(username) or "").lower()
    if not name:
        return RedirectResponse(url="/?error=NAME_REQUIRED", status_code=303)
    if not username:
        return RedirectResponse(url="/?error=USERNAME_REQUIRED", status_code=303)

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

        response = RedirectResponse(url="/tasks", status_code=303)
        response.set_cookie("session", make_session_token(user.id), httponly=True)
        return response
    finally:
        db.close()


@app.post("/login")
def login(username: str = Form(...), password: str = Form(...)):
    if len(password.encode("utf-8")) > 72:
        return RedirectResponse(url="/?error=PW_TOO_LONG", status_code=303)

    username = username.strip().lower()

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == username).first()
        if not user or not verify_password(password, user.password_hash):
            return RedirectResponse(url="/?error=BAD_LOGIN", status_code=303)

        response = RedirectResponse(url="/tasks", status_code=303)
        response.set_cookie("session", make_session_token(user.id), httponly=True)
        return response
    finally:
        db.close()


@app.post("/logout")
def logout():
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie("session")
    return response


@app.post("/add")
def add_event(
    request: Request,
    title: str = Form(...),
    date_: str = Form(...),
    time: str = Form(...),
    description: str = Form(""),
    invitees: str = Form(""),
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/?error=LOGIN_REQUIRED", status_code=303)

    description = clip(description)
    if description and len(description) > MAX_TEXT:
        return RedirectResponse(url="/tasks?error=DESC_TOO_LONG", status_code=303)

    starts_at = datetime.fromisoformat(f"{date_}T{time}")
    usernames = parse_usernames_csv(invitees)

    db = SessionLocal()
    try:
        e = Event(title=title, starts_at=starts_at, description=description, created_by_user_id=user.id)
        db.add(e)
        db.commit()
        db.refresh(e)

        # meghívottak feloldása user rekordokra
        if usernames:
            found_users = db.query(User).filter(User.username.in_(usernames)).all()
            found_by_username = {u.username: u for u in found_users}
            missing = [u for u in usernames if u not in found_by_username]

            if missing:
                # ha hibás username, ne maradjon félkész állapot -> töröljük az eseményt is
                db.delete(e)
                db.commit()
                return RedirectResponse(url="/tasks?error=INVITE_UNKNOWN", status_code=303)

            for u in found_users:
                if u.id == user.id:
                    continue
                db.add(EventInvite(event_id=e.id, user_id=u.id, invited_by_user_id=user.id))

            db.commit()
    finally:
        db.close()

    return RedirectResponse(url="/tasks", status_code=303)


@app.post("/invite")
def invite_to_event(request: Request, event_id: int = Form(...), username: str = Form(...)):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/?error=LOGIN_REQUIRED", status_code=303)

    username = username.strip().lower()
    if not username:
        return RedirectResponse(url="/tasks?error=INVITE_UNKNOWN", status_code=303)

    db = SessionLocal()
    try:
        e = db.query(Event).filter(Event.id == event_id).first()
        if not e:
            return RedirectResponse(url="/tasks?error=NO_EVENT", status_code=303)

        if e.created_by_user_id != user.id:
            return RedirectResponse(url="/tasks?error=NOT_CREATOR", status_code=303)

        u = db.query(User).filter(User.username == username).first()
        if not u:
            return RedirectResponse(url="/tasks?error=INVITE_UNKNOWN", status_code=303)

        if u.id == user.id:
            return RedirectResponse(url="/tasks", status_code=303)

        existing = db.query(EventInvite).filter(EventInvite.event_id == event_id, EventInvite.user_id == u.id).first()
        if not existing:
            db.add(EventInvite(event_id=event_id, user_id=u.id, invited_by_user_id=user.id))
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

    db = SessionLocal()
    try:
        if not is_event_visible_to_user(db, event_id, user):
            return RedirectResponse(url="/tasks?error=FORBIDDEN", status_code=303)
    finally:
        db.close()

    status = status.strip().lower()
    if status not in ALLOWED_STATUSES:
        return RedirectResponse(url="/tasks?error=BAD_STATUS", status_code=303)

    comment = clip(comment)
    if comment and len(comment) > MAX_TEXT:
        return RedirectResponse(url="/tasks?error=COMMENT_TOO_LONG", status_code=303)

    db = SessionLocal()
    try:
        existing = (
            db.query(EventResponse)
            .filter(EventResponse.event_id == event_id, EventResponse.user_id == user.id)
            .first()
        )

        if existing:
            existing.status = status
            existing.comment = comment
        else:
            db.add(EventResponse(event_id=event_id, user_id=user.id, status=status, comment=comment))

        db.commit()
    finally:
        db.close()

    return RedirectResponse(url="/tasks", status_code=303)


@app.post("/suggest_time")
def suggest_time(
    request: Request,
    event_id: int = Form(...),
    date_: str = Form(...),
    time: str = Form(...),
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/?error=LOGIN_REQUIRED", status_code=303)

    proposed_starts_at = datetime.fromisoformat(f"{date_}T{time}")

    db = SessionLocal()
    try:
        if not is_event_visible_to_user(db, event_id, user):
            return RedirectResponse(url="/tasks?error=FORBIDDEN", status_code=303)

        existing = (
            db.query(EventTimeSuggestion)
            .filter(
                EventTimeSuggestion.event_id == event_id,
                EventTimeSuggestion.proposed_starts_at == proposed_starts_at,
            )
            .first()
        )
        if not existing:
            db.add(
                EventTimeSuggestion(
                    event_id=event_id,
                    proposed_by_user_id=user.id,
                    proposed_starts_at=proposed_starts_at,
                )
            )
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

    vote = vote.strip().lower()
    if vote not in ("up", "down"):
        return RedirectResponse(url="/tasks?error=BAD_VOTE", status_code=303)

    db = SessionLocal()
    try:
        s = db.query(EventTimeSuggestion).filter(EventTimeSuggestion.id == suggestion_id).first()
        if not s:
            return RedirectResponse(url="/tasks?error=NO_SUGGESTION", status_code=303)
        if not is_event_visible_to_user(db, s.event_id, user):
            return RedirectResponse(url="/tasks?error=FORBIDDEN", status_code=303)

        existing = (
            db.query(EventTimeVote)
            .filter(EventTimeVote.suggestion_id == suggestion_id, EventTimeVote.user_id == user.id)
            .first()
        )

        if existing:
            if existing.vote == vote:
                db.delete(existing)  # visszavonás
            else:
                existing.vote = vote  # váltás up<->down
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

        e.starts_at = s.proposed_starts_at

        all_s = db.query(EventTimeSuggestion).filter(EventTimeSuggestion.event_id == e.id).all()
        for x in all_s:
            x.accepted = (x.id == s.id)

        # mindenkinél új válasz kell
        db.query(EventResponse).filter(EventResponse.event_id == e.id).delete()

        db.commit()
    finally:
        db.close()

    return RedirectResponse(url="/tasks", status_code=303)
