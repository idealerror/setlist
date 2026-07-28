"""Venue setlist logger -- Windows capture client.

Passively identifies music playing in a room and logs it locally. Audio is
never retained: chunks live in a temp file only long enough to fingerprint,
then are deleted in a finally block (spec 11).
"""

__version__ = "0.3.0"
