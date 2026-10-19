"""Shared startup parsing for the supervised and standalone ViewFS cache."""

import os

DEFAULT_CONTENT_CACHE_BYTES = 256 * 1024 * 1024
CONTENT_CACHE_ENV = "GIT_VIEWSPACE_FS_CONTENT_CACHE_BYTES"


def content_cache_budget(value=None):
    """An explicit value wins; only read the environment when no value is supplied."""
    if value is None:
        value = os.environ.get(CONTENT_CACHE_ENV, DEFAULT_CONTENT_CACHE_BYTES)
    if type(value) not in (str, int):
        raise ValueError("content cache budget must be nonnegative integer bytes")
    try:
        budget = int(value)
    except ValueError as error:
        raise ValueError("content cache budget must be nonnegative integer bytes") from error
    if budget < 0:
        raise ValueError("content cache budget must be nonnegative integer bytes")
    return budget
