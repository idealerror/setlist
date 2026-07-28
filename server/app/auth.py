"""Bearer token auth, one token per venue (spec 6).

Tokens are stored only as a sha256 digest. The plaintext is emitted once by
`manage.py issue-token` and is not recoverable afterwards -- reissue instead.
"""

from __future__ import annotations

import hashlib
import secrets

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db
from .models import Venue


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def venue_from_token(
    authorization: str = Header(default=""),
    db: Session = Depends(get_db),
) -> Venue:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    digest = hash_token(token.strip())
    venue = db.execute(
        select(Venue).where(Venue.token_hash == digest)
    ).scalar_one_or_none()
    if venue is None:
        # Deliberately not distinguishing unknown token from wrong venue.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid token")
    return venue
