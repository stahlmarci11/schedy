from sqlalchemy import (
    Column, Integer, String, DateTime, ForeignKey,
    UniqueConstraint, Boolean
)
from sqlalchemy.orm import relationship
from db import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    username = Column(String, unique=True, index=True, nullable=False)
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

    # ✅ esemény időintervallum
    starts_at = Column(DateTime, nullable=False)
    ends_at = Column(DateTime, nullable=False)

    description = Column(String, nullable=True)

    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    creator = relationship("User", back_populates="events")

    responses = relationship("EventResponse", back_populates="event", cascade="all, delete-orphan")
    suggestions = relationship("EventTimeSuggestion", back_populates="event", cascade="all, delete-orphan")
    invites = relationship("EventInvite", back_populates="event", cascade="all, delete-orphan")


class EventInvite(Base):
    __tablename__ = "event_invites"

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    invited_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    event = relationship("Event", back_populates="invites")
    user = relationship("User", back_populates="invites", foreign_keys=[user_id])
    invited_by = relationship("User", back_populates="sent_invites", foreign_keys=[invited_by_user_id])

    __table_args__ = (UniqueConstraint("event_id", "user_id", name="uq_event_user_invite"),)


class EventResponse(Base):
    __tablename__ = "event_responses"

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    status = Column(String, nullable=False)  # "yes" | "maybe" | "no"
    comment = Column(String, nullable=True)

    event = relationship("Event", back_populates="responses")
    user = relationship("User", back_populates="responses")

    __table_args__ = (UniqueConstraint("event_id", "user_id", name="uq_event_user_response"),)


class EventTimeSuggestion(Base):
    __tablename__ = "event_time_suggestions"

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)

    proposed_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    # ✅ javaslat is időintervallum
    proposed_starts_at = Column(DateTime, nullable=False)
    proposed_ends_at = Column(DateTime, nullable=False)

    accepted = Column(Boolean, nullable=False, default=False)
    comment = Column(String, nullable=True)

    event = relationship("Event", back_populates="suggestions")
    proposed_by = relationship("User", back_populates="suggestions")

    votes = relationship("EventTimeVote", back_populates="suggestion", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("event_id", "proposed_starts_at", "proposed_ends_at", name="uq_event_suggested_range"),
    )


class EventTimeVote(Base):
    __tablename__ = "event_time_votes"

    id = Column(Integer, primary_key=True, index=True)
    suggestion_id = Column(Integer, ForeignKey("event_time_suggestions.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    vote = Column(String, nullable=False)  # "up" | "down"

    suggestion = relationship("EventTimeSuggestion", back_populates="votes")
    user = relationship("User", back_populates="suggestion_votes")

    __table_args__ = (UniqueConstraint("suggestion_id", "user_id", name="uq_suggestion_user_vote"),)
