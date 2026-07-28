"""Request and response bodies for the spec 6 contract."""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict, Field


class DetectionIn(BaseModel):
    #: Client-generated UUID; the idempotency key (spec 4.2).
    id: uuid.UUID
    captured_at: dt.datetime
    shazam_key: str | None = None
    title: str | None = None
    artist: str | None = None
    isrc: str | None = None
    level_dbfs: float
    method: str
    client_version: str


class DetectionBatch(BaseModel):
    venue: str
    detections: list[DetectionIn]


class BatchResult(BaseModel):
    accepted: int
    duplicates: int


class HeartbeatIn(BaseModel):
    venue: str
    client_version: str
    queue_depth: int = 0
    uptime_s: int = 0
    #: Extension beyond the spec 6 body. The daily API ceiling is client-side
    #: state that the server cannot infer, and spec 8 wants an alert when it
    #: trips. Optional, so a client that omits it still validates.
    ceiling_tripped: bool = False


class HeartbeatResult(BaseModel):
    ok: bool = True
    server_time: dt.datetime


class TrackOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    shazam_key: str
    title: str | None
    artist: str | None
    isrc: str | None


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    external_id: str
    title: str | None
    starts_at: dt.datetime | None
    ends_at: dt.datetime | None
    doors_at: dt.datetime | None
    url: str | None
    performers: list | None


class PlayOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    started_at: dt.datetime
    ended_at: dt.datetime
    detection_count: int
    event_id: int | None
    track: TrackOut


class TopTrack(BaseModel):
    track: TrackOut
    play_count: int
    detection_count: int


class TopTracks(BaseModel):
    items: list[TopTrack] = Field(default_factory=list)
