"""Machine defaults live in an unexported sidecar, never in distributable code.

The sidecar contains paths and policy settings, not credentials or job state.
Explicit workflow JSON remains authoritative for a scoped operation.
"""
import json
from pathlib import Path
import re

PROJECT = Path(__file__).resolve().parent.parent
LOCAL_PROFILE = PROJECT / "runtime/workflow.local.json"
PATH_KEYS = frozenset({"download_dir", "bundle_root", "prepared_root", "tokenizer_json",
                       "tokenizer_runtime", "configuration", "zotero_storage"})
VALUE_KEYS = frozenset({"credential_container", "embedding_enabled", "embedding_authorization", "transport_base_url"})


def read_local_profile(path):
    path = Path(path)
    if not path.exists():
        return {}
    if not path.is_file() or path.stat().st_size > 16384:
        raise ValueError("LOCAL_WORKFLOW_PROFILE_INVALID")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("LOCAL_WORKFLOW_PROFILE_INVALID")
            result[key] = value
        return result
    try:
        profile = json.loads(path.read_text("utf-8"), object_pairs_hook=unique)
        if (not isinstance(profile, dict) or set(profile) != {"version", "defaults"}
                or type(profile["version"]) is not int or profile["version"] != 1
                or not isinstance(profile["defaults"], dict)):
            raise ValueError
        values = profile["defaults"]
        if set(values) - PATH_KEYS - VALUE_KEYS:
            raise ValueError
        for key, value in values.items():
            if key == "embedding_enabled":
                if type(value) is not bool:
                    raise ValueError
            elif not isinstance(value, str) or not value.strip() or len(value) > 4096 or any(ord(c) < 32 for c in value):
                raise ValueError
        if "credential_container" in values and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", values["credential_container"]):
            raise ValueError
        if "embedding_authorization" in values and not re.fullmatch(r"[A-Za-z0-9_]{1,128}", values["embedding_authorization"]):
            raise ValueError
        return values
    except (ValueError, KeyError, UnicodeError):
        raise ValueError("LOCAL_WORKFLOW_PROFILE_INVALID") from None


LOCAL_DEFAULTS = read_local_profile(LOCAL_PROFILE)


def local_setting(name, fallback=None):
    return LOCAL_DEFAULTS.get(name, fallback)


def default_path(name, fallback=None):
    value = local_setting(name, fallback)
    if value is None:
        return None
    path = Path(value).expanduser()
    return (path if path.is_absolute() else PROJECT / path).resolve()
