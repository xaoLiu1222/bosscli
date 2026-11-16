"""Bind one explicitly selected search job to its server-issued chat context.

Pure parsing/parameter construction: importing or calling this module never performs
I/O, reads a session, or contacts a person. Search-card ``securityId`` belongs ONLY
in friend/add. Only ``relation.securityId`` (or the matching getBaseInfo entry) may
be used for chat/image upload. Do not silently substitute one token for the other.

Static contract: Android 14.180 GeekCreateFriendResponse.relation is a
ServerAddFriendBean; ContactBean.fromServerGeekAddFriendBean copies its fields and
packs friendSource with hh/y.a(friendSource, userSource). See the offline evidence
in work/contacts_baseinfo_contract_static_20261111.txt. No live contract claim.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import re
from typing import Any

BASEINFO_PATH = "/api/zprelation/friend/getBaseInfo"
BASEINFO_METHOD = "POST"
_LONG_MAX = (1 << 63) - 1


class ContactError(ValueError):
    """Missing, ambiguous or mismatched contact context; never includes raw tokens."""


def _integer(value: Any, label: str, *, minimum: int = 0, maximum: int = _LONG_MAX) -> int:
    # bool is an int in Python, but never a valid server identifier/source.
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ContactError(f"{label} must be an integer")
    if isinstance(value, str) and not re.fullmatch(r"[0-9]+", value):
        raise ContactError(f"{label} must be a decimal integer")
    number = int(value)
    if not minimum <= number <= maximum:
        raise ContactError(f"{label} is outside its allowed range")
    return number


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContactError(f"{label} is required and must be non-empty text")
    return value


def _required(data: Mapping, name: str, label: str) -> Any:
    if name not in data or data[name] is None:
        raise ContactError(f"{label}.{name} is required")
    return data[name]


@dataclass(frozen=True)
class SelectedJob:
    """Snapshot of one parse_jobs row, not a search result list or job-detail token.

    ``name`` is the job title; ``recipient_name`` is the HR/boss display name.
    Numeric jobId/userId are required: parse_jobs can fall back to encryptJobId,
    but an opaque encrypted ID cannot safely be compared to relation.jobId (long).
    Tokens and tracking lid are intentionally excluded from repr/str.
    """
    name: str
    company: str
    job_id: int
    user_id: int
    search_security_id: str = field(repr=False)
    lid: str = field(repr=False)
    recipient_name: str = ""

    def __post_init__(self) -> None:
        _text(self.name, "selected.name")
        _text(self.company, "selected.company")
        object.__setattr__(self, "job_id", _integer(self.job_id, "selected.jobId", minimum=1))
        object.__setattr__(self, "user_id", _integer(self.user_id, "selected.userId", minimum=1))
        _text(self.search_security_id, "selected.searchSecurityId")
        _text(self.lid, "selected.lid")
        if not isinstance(self.recipient_name, str):
            raise ContactError("selected.recipient_name must be text")

    @classmethod
    def from_search_job(cls, job: Mapping[str, Any]) -> "SelectedJob":
        """Accept exactly one row returned by BossClient.parse_jobs, without I/O."""
        if not isinstance(job, Mapping):
            raise ContactError("selected search job must be one object")
        return cls(name=_required(job, "name", "selected"),
                   company=_required(job, "company", "selected"),
                   job_id=_required(job, "jobId", "selected"),
                   user_id=_required(job, "userId", "selected"),
                   search_security_id=_required(job, "securityId", "selected"),
                   lid=_required(job, "lid", "selected"),
                   recipient_name=job.get("hr") or "")

    def add_friend_kwargs(self) -> dict[str, str]:
        """Explicit kwargs for client.add_friend; this helper does NOT send them."""
        return {"security_id": self.search_security_id, "job_id": str(self.job_id),
                "friend_id": str(self.user_id), "lid": self.lid}


def pack_friend_source(friend_source: int, user_source: int) -> int:
    """hh/y.a(II)I: raw friendSource | (userSource << 16) when userSource > 0.

    Inputs are the two unpacked unsigned 16-bit source components from the bean.
    Do not pass an already-packed ContactBean.friendSource as friend_source here.
    """
    low = _integer(friend_source, "relation.friendSource", maximum=0xFFFF)
    high = _integer(user_source, "relation.userSource", maximum=0xFFFF)
    packed = low | (high << 16) if high > 0 else low
    # Match Java's signed int representation, including the rarely used high bit.
    return packed if packed < (1 << 31) else packed - (1 << 32)


def _packed_source(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ContactError("friend_source must be a packed Java int")
    if isinstance(value, str) and not re.fullmatch(r"-?[0-9]+", value):
        raise ContactError("friend_source must be a packed Java int")
    number = int(value)
    if not -(1 << 31) <= number < (1 << 31):
        raise ContactError("friend_source must be a packed Java int")
    return number


@dataclass(frozen=True)
class ConversationContext:
    """Validated chat context bound to the selected person AND selected job.

    friend_source is the packed ContactBean value (not raw relation.friendSource).
    security_id is a relation/baseInfo token, never the search-card token.
    job_id may come from the selection only when relation.jobId is absent or zero.
    """
    selected_job: SelectedJob
    friend_id: int
    friend_source: int
    security_id: str = field(repr=False)
    job_id: int
    job_source: int
    user_source: int

    def __post_init__(self) -> None:
        if not isinstance(self.selected_job, SelectedJob):
            raise ContactError("selected_job must be a SelectedJob")
        object.__setattr__(self, "friend_id", _integer(self.friend_id, "context.friendId", minimum=1))
        object.__setattr__(self, "job_id", _integer(self.job_id, "context.jobId", minimum=1))
        object.__setattr__(self, "friend_source", _packed_source(self.friend_source))
        object.__setattr__(self, "job_source", _integer(self.job_source, "context.jobSource", maximum=0x7FFFFFFF))
        object.__setattr__(self, "user_source", _integer(self.user_source, "context.userSource", maximum=0xFFFF))
        _text(self.security_id, "context.securityId")
        if self.friend_id != self.selected_job.user_id:
            raise ContactError("context.friendId does not match selected.userId")
        if self.job_id != self.selected_job.job_id:
            raise ContactError("context.jobId conflicts with selected.jobId")
        if ((self.friend_source >> 16) & 0xFFFF) != self.user_source:
            raise ContactError("context.friendSource and userSource are inconsistent")

    def baseinfo_params(self) -> dict[str, str]:
        return build_baseinfo_params(self.friend_id, self.friend_source)


def parse_relation(selected: SelectedJob, relation: Mapping[str, Any]) -> ConversationContext:
    """Validate one ServerAddFriendBean. No fallbacks to unrelated response fields.

    friendId/securityId/friendSource/userSource/jobSource must be explicit. Android
    jobId is a primitive long: absent/zero means unknown; the selected job remains
    authoritative. Any explicit nonzero conflicting jobId is rejected.
    """
    if not isinstance(selected, SelectedJob):
        raise ContactError("selected must be a SelectedJob")
    if not isinstance(relation, Mapping):
        raise ContactError("relation must be an object")
    friend_id = _integer(_required(relation, "friendId", "relation"), "relation.friendId", minimum=1)
    if friend_id != selected.user_id:
        raise ContactError("relation.friendId does not match selected.userId")
    job_id = _integer(relation.get("jobId", 0), "relation.jobId")
    if job_id and job_id != selected.job_id:
        raise ContactError("relation.jobId conflicts with selected.jobId")
    security_id = _text(_required(relation, "securityId", "relation"), "relation.securityId")
    raw_source = _required(relation, "friendSource", "relation")
    user_source = _integer(_required(relation, "userSource", "relation"), "relation.userSource", maximum=0xFFFF)
    friend_source = pack_friend_source(raw_source, user_source)
    job_source = _integer(_required(relation, "jobSource", "relation"), "relation.jobSource", maximum=0x7FFFFFFF)
    return ConversationContext(selected, friend_id, friend_source, security_id,
                               job_id or selected.job_id, job_source, user_source)


def _response_data(response: Mapping[str, Any], operation: str) -> Mapping[str, Any]:
    if not isinstance(response, Mapping):
        raise ContactError(f"{operation} response must be an object")
    code = _required(response, "code", operation)
    if type(code) is not int or code != 0:
        # Raw server messages may include identifiers/tokens; do not echo them.
        raise ContactError(f"{operation} did not return integer code 0; context not bound")
    data = _required(response, "zpData", operation)
    if not isinstance(data, Mapping):
        raise ContactError(f"{operation}.zpData must be an object")
    return data


def parse_add_friend_response(selected: SelectedJob, response: Mapping[str, Any]) -> ConversationContext:
    """Bind only successful response.zpData.relation, never top-level/search tokens."""
    data = _response_data(response, "friend/add")
    return parse_relation(selected, _required(data, "relation", "friend/add.zpData"))


def build_baseinfo_params(friend_id: int, friend_source: int) -> dict[str, str]:
    """Build POST getBaseInfo params for ONE selected friend; no securityId needed.

    ff/c.a routes packed source high16==2 -> peerOwnerFriendIds; otherwise
    low16==2 -> peerFriendIds; low16==1 -> dzFriendIds; else friendIds. All
    four fields are strings; multi-ID encoding in i.b(List) is comma-delimited,
    NOT JSON. This intentionally accepts no list and emits no unrelated IDs.
    """
    identifier = _integer(friend_id, "friend_id", minimum=1)
    source = _packed_source(friend_source)
    low, high = source & 0xFFFF, (source >> 16) & 0xFFFF
    key = ("peerOwnerFriendIds" if high == 2 else "peerFriendIds" if low == 2
           else "dzFriendIds" if low == 1 else "friendIds")
    return {key: str(identifier)}


def parse_baseinfo_response(selected: SelectedJob, response: Mapping[str, Any]) -> ConversationContext:
    """Bind exactly one matching baseInfoList item; never take the first entry.

    Unrelated entries are ignored without reading their security/source fields.
    Missing/invalid friendId or duplicate matches are ambiguous and fail closed.
    The matching bean must satisfy precisely the same contract as friend/add.
    """
    if not isinstance(selected, SelectedJob):
        raise ContactError("selected must be a SelectedJob")
    data = _response_data(response, "getBaseInfo")
    entries = _required(data, "baseInfoList", "getBaseInfo.zpData")
    if not isinstance(entries, list):
        raise ContactError("getBaseInfo.zpData.baseInfoList must be a list")
    matches = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ContactError("baseInfoList entries must be objects")
        identifier = _integer(_required(entry, "friendId", "baseInfoList entry"),
                              "baseInfoList entry.friendId", minimum=1)
        if identifier == selected.user_id:
            matches.append(entry)
    if not matches:
        raise ContactError("baseInfoList has no matching selected.userId")
    if len(matches) != 1:
        raise ContactError("baseInfoList has duplicate matching friendId entries")
    return parse_relation(selected, matches[0])
