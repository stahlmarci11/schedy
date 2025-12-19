from sqlalchemy import (
    Column, Integer, String, DateTime, ForeignKey,
    UniqueConstraint, Boolean
)
from sqlalchemy.orm import relationship
from db import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)

    # Megjelenített név (keresztnév)
    name = Column(String, nullable=False)

    # Belépési név (username)
    username = Column(String, unique=True, index=True, nullable=False)

    # Email csak tárolásra (értesítésekhez)
    email = Column(String, unique=True, index=True, nullable=False)

    password_hash = Column(String, nullable=False)

    events = relationship("Event", back_populates="creator")
    responses = relationship("EventResponse", back_populates="user")

    suggestions = relationship("EventTimeSuggestion", back_populates="proposed_by")
    suggestion_votes = relationship("EventTimeVote", back_populates="user")

    invites = relationship("EventInvite", back_populates="user", foreign_keys="EventInvite.user_id")
    sent_invites = relationship("EventInvite", back_populates="invited_by", foreign_keys="EventInvite.invited_by_user_id")


class Event(Base):
    __tablename__ = "events"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    starts_at = Column(DateTime, nullable=False)

    description = Column(String, nullable=True)  # max 300-at app szinten ellenőrizzük

    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    creator = relationship("User", back_populates="events")

    responses = relationship("EventResponse", back_populates="event", cascade="all, delete-orphan")
    suggestions = relationship("EventTimeSuggestion", back_populates="event", cascade="all, delete-orphan")
    invites = relationship("EventInvite", back_populates="event", cascade="all, delete-orphan")


class EventInvite(Base):
    __tablename__ = "event_invites"

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)

    # akit meghívtunk
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    # aki meghívta (általában a szervező)
    invited_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    event = relationship("Event", back_populates="invites")
    user = relationship("User", back_populates="invites", foreign_keys=[user_id])
    invited_by = relationship("User", back_populates="sent_invites", foreign_keys=[invited_by_user_id])

    __table_args__ = (
        UniqueConstraint("event_id", "user_id", name="uq_event_user_invite"),
    )


class EventResponse(Base):
    __tablename__ = "event_responses"

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    status = Column(String, nullable=False)   # "yes" | "maybe" | "no"
    comment = Column(String, nullable=True)   # max 300 app szinten

    event = relationship("Event", back_populates="responses")
    user = relationship("User", back_populates="responses")

    __table_args__ = (
        UniqueConstraint("event_id", "user_id", name="uq_event_user_response"),
    )


class EventTimeSuggestion(Base):
    __tablename__ = "event_time_suggestions"

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)

    proposed_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    proposed_starts_at = Column(DateTime, nullable=False)
    accepted = Column(Boolean, nullable=False, default=False)

    event = relationship("Event", back_populates="suggestions")
    proposed_by = relationship("User", back_populates="suggestions")

    votes = relationship("EventTimeVote", back_populates="suggestion", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("event_id", "proposed_starts_at", name="uq_event_suggested_time"),
    )


class EventTimeVote(Base):
    __tablename__ = "event_time_votes"

    id = Column(Integer, primary_key=True, index=True)
    suggestion_id = Column(Integer, ForeignKey("event_time_suggestions.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    # "up" = támogatom, "down" = nekem nem jó
    vote = Column(String, nullable=False)

    suggestion = relationship("EventTimeSuggestion", back_populates="votes")
    user = relationship("User", back_populates="suggestion_votes")

    __table_args__ = (
        UniqueConstraint("suggestion_id", "user_id", name="uq_suggestion_user_vote"),
    )
