"""Explicit job allowlists for cross-stage coordination (None != empty)."""
import re

from handoff_saved_download import HandoffError

_JOB_KEY = re.compile(r"(?:pubmed:[0-9]+|arxiv:(?:[0-9]{4}\.[0-9]{4,5}|[a-z][a-z0-9.-]*/[0-9]{7})(?:v[0-9]+)?)\Z")


def validate_job_scope(job_keys):
    if job_keys is None:
        return None
    if not isinstance(job_keys, (set, frozenset, list, tuple)) or len(job_keys) > 1000:
        raise HandoffError("job_keys must be an explicit bounded collection")
    if any(not isinstance(key, str) or len(key) > 100 or not _JOB_KEY.fullmatch(key)
           for key in job_keys):
        raise HandoffError("job_keys contains an invalid stable publication identifier")
    if len(set(job_keys)) != len(job_keys):
        raise HandoffError("job_keys must not contain duplicates")
    return frozenset(job_keys)
