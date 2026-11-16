"""Synthetic-only contact binding tests: no device, session file or HTTP calls."""
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bosscli.contacts import (BASEINFO_METHOD, BASEINFO_PATH, ContactError,
    ConversationContext, SelectedJob, build_baseinfo_params, pack_friend_source,
    parse_add_friend_response, parse_baseinfo_response, parse_relation)


SEARCH_TOKEN = "synthetic-search-security-only"
CHAT_TOKEN = "synthetic-relation-chat-security"
LID = "synthetic-search-tracking-lid"


def search_job(**updates):
    return {"name": "Synthetic Python Engineer", "company": "Synthetic Company",
            "jobId": 100001, "userId": 200002, "securityId": SEARCH_TOKEN,
            "lid": LID, "hr": "Synthetic Recruiter", **updates}


def selected(**updates):
    return SelectedJob.from_search_job(search_job(**updates))


def relation(**updates):
    return {"friendId": 200002, "securityId": CHAT_TOKEN, "jobId": 100001,
            "friendSource": 0, "userSource": 0, "jobSource": 7, **updates}


def success(data):
    return {"code": 0, "message": "synthetic success", "zpData": data}


def test_search_selection_preserves_card_provenance_and_recipient():
    job = search_job(jobId="100001", userId="200002")
    choice = SelectedJob.from_search_job(job)
    assert choice.name == "Synthetic Python Engineer"
    assert choice.company == "Synthetic Company"
    assert choice.recipient_name == "Synthetic Recruiter"
    assert choice.job_id == 100001 and choice.user_id == 200002
    assert choice.add_friend_kwargs() == {"security_id": SEARCH_TOKEN,
        "job_id": "100001", "friend_id": "200002", "lid": LID}
    job["securityId"] = "synthetic-mutated-token"
    assert choice.search_security_id == SEARCH_TOKEN
    with pytest.raises(FrozenInstanceError):
        choice.job_id = 99


@pytest.mark.parametrize("field", ["name", "company", "jobId", "userId", "securityId", "lid"])
def test_selection_missing_fields_are_explicit(field):
    job = search_job()
    del job[field]
    with pytest.raises(ContactError, match=field):
        SelectedJob.from_search_job(job)


@pytest.mark.parametrize("field", ["jobId", "userId"])
@pytest.mark.parametrize("bad", [None, True, False, 0, -1, 1.5, "", " ", "1.0", "1e4", "abc-encrypted", [], {}, 1 << 63])
def test_selection_requires_positive_numeric_long_ids(field, bad):
    with pytest.raises(ContactError, match=field):
        selected(**{field: bad})


def test_encrypted_job_id_fallback_is_not_safe_for_binding():
    with pytest.raises(ContactError, match="jobId"):
        selected(jobId="synthetic-encrypt-job-id", encryptJobId="synthetic-encrypt-job-id")


@pytest.mark.parametrize("field", ["name", "company", "securityId", "lid"])
@pytest.mark.parametrize("bad", [None, "", "  ", 123, []])
def test_selection_requires_text_fields(field, bad):
    with pytest.raises(ContactError):
        selected(**{field: bad})


@pytest.mark.parametrize("bad", [None, [], "one", 2])
def test_select_exactly_one_mapping(bad):
    with pytest.raises(ContactError):
        SelectedJob.from_search_job(bad)


def test_add_response_binds_only_relation_not_search_security():
    choice = selected()
    response = success({"relation": relation(), "securityId": "synthetic-unrelated-token"})
    context = parse_add_friend_response(choice, response)
    assert context.selected_job is choice
    assert context.friend_id == choice.user_id
    assert context.job_id == choice.job_id
    assert context.job_source == 7
    assert context.security_id == CHAT_TOKEN
    assert context.security_id != choice.search_security_id
    assert choice.add_friend_kwargs()["security_id"] == SEARCH_TOKEN
    assert response["zpData"]["relation"]["friendSource"] == 0


def test_all_repr_and_str_hide_search_chat_tokens_and_tracking_lid():
    choice = selected()
    context = parse_relation(choice, relation())
    for value in (choice, context):
        for rendered in (repr(value), str(value)):
            assert SEARCH_TOKEN not in rendered
            assert CHAT_TOKEN not in rendered
            assert LID not in rendered
            assert "Synthetic Company" in rendered
    with pytest.raises(FrozenInstanceError):
        context.security_id = "other-synthetic"


@pytest.mark.parametrize("friend_id", [200003, "200003", 100001, 0, -1, None, "synthetic-id"])
def test_relation_cannot_bind_another_person(friend_id):
    with pytest.raises(ContactError, match="friendId"):
        parse_relation(selected(), relation(friendId=friend_id))


@pytest.mark.parametrize("job_id", [100002, "100002", 200002, -1, True, "opaque-job-id", None])
def test_relation_job_conflicts_or_invalid_job_id_fail(job_id):
    with pytest.raises(ContactError, match="jobId"):
        parse_relation(selected(), relation(jobId=job_id))


@pytest.mark.parametrize("unknown", [0, "0", "missing"])
def test_unknown_relation_job_id_remains_bound_to_selected_job(unknown):
    bean = relation(jobId=unknown)
    if unknown == "missing":
        del bean["jobId"]
    assert parse_relation(selected(), bean).job_id == 100001


@pytest.mark.parametrize("field", ["friendId", "securityId", "friendSource", "userSource", "jobSource"])
def test_relation_required_fields_have_clear_errors(field):
    bean = relation()
    del bean[field]
    with pytest.raises(ContactError, match=field):
        parse_relation(selected(), bean)


@pytest.mark.parametrize("field", ["friendSource", "userSource", "jobSource"])
@pytest.mark.parametrize("bad", [None, -1, True, "", "1.2", [], {}])
def test_relation_sources_do_not_get_guessed(field, bad):
    with pytest.raises(ContactError, match=field):
        parse_relation(selected(), relation(**{field: bad}))


@pytest.mark.parametrize("bad", [None, "", "   ", 123, []])
def test_relation_security_must_be_nonempty_text(bad):
    with pytest.raises(ContactError, match="securityId"):
        parse_relation(selected(), relation(securityId=bad))


@pytest.mark.parametrize("raw,user,packed", [
    (0, 0, 0), (1, 0, 1), (2, 0, 2), (3, 1, 65539),
    (1, 2, 131073), (2, 2, 131074), (65535, 65535, -1),
    (0, 32768, -(1 << 31))])
def test_pack_source_matches_java_bitwise_or(raw, user, packed):
    assert pack_friend_source(raw, user) == packed
    context = parse_relation(selected(), relation(friendSource=raw, userSource=user))
    assert context.friend_source == packed and context.user_source == user


@pytest.mark.parametrize("bad", [65536, -1, True, None])
def test_pack_source_rejects_raw_out_of_range(bad):
    with pytest.raises(ContactError):
        pack_friend_source(bad, 0)
    with pytest.raises(ContactError):
        pack_friend_source(0, bad)


@pytest.mark.parametrize("source,key", [
    (0, "friendIds"), (3, "friendIds"), (1, "dzFriendIds"),
    (2, "peerFriendIds"), ((1 << 16) | 2, "peerFriendIds"),
    ((2 << 16), "peerOwnerFriendIds"), ((2 << 16) | 1, "peerOwnerFriendIds"),
    ((2 << 16) | 2, "peerOwnerFriendIds"), (-1, "friendIds")])
def test_baseinfo_routes_one_decimal_id_by_packed_source(source, key):
    assert BASEINFO_METHOD == "POST"
    assert BASEINFO_PATH == "/api/zprelation/friend/getBaseInfo"
    assert build_baseinfo_params(200002, source) == {key: "200002"}
    # Single friend routing does not serialize a JSON array or pass any token.
    assert build_baseinfo_params("200002", str(source)) == {key: "200002"}


@pytest.mark.parametrize("bad", [[200002], "200002,200003", "[200002]", True, None, 0, -1])
def test_baseinfo_cannot_expand_to_multiple_or_invalid_people(bad):
    with pytest.raises(ContactError):
        build_baseinfo_params(bad, 0)


@pytest.mark.parametrize("bad", [None, True, 1 << 31, -(1 << 31) - 1, "1.0", []])
def test_baseinfo_requires_known_valid_packed_source(bad):
    with pytest.raises(ContactError):
        build_baseinfo_params(200002, bad)


def test_context_builds_its_own_single_friend_refresh_params():
    context = parse_relation(selected(), relation(friendSource=2, userSource=2))
    assert context.baseinfo_params() == {"peerOwnerFriendIds": "200002"}


def test_refresh_matches_selected_friend_not_first_and_replaces_context_token():
    choice = selected()
    response = success({"baseInfoList": [
        {"friendId": 200003},  # Unrelated incomplete bean must not supply any data.
        relation(securityId="synthetic-refreshed-chat-token", friendSource=1),
        {"friendId": 200004, "securityId": "synthetic-unrelated-token"}]})
    context = parse_baseinfo_response(choice, response)
    assert context.friend_id == 200002
    assert context.security_id == "synthetic-refreshed-chat-token"
    assert context.friend_source == 1
    assert context.baseinfo_params() == {"dzFriendIds": "200002"}


@pytest.mark.parametrize("entries", [[], [relation(friendId=200003)],
    [relation(friendId=200003), relation(friendId=200004)]])
def test_refresh_missing_selected_friend_never_uses_first(entries):
    with pytest.raises(ContactError, match="no matching"):
        parse_baseinfo_response(selected(), success({"baseInfoList": entries}))


def test_refresh_rejects_duplicate_matches_even_when_identical():
    with pytest.raises(ContactError, match="duplicate"):
        parse_baseinfo_response(selected(), success({"baseInfoList": [relation(), relation()]}))


def test_refresh_rejects_conflicting_job_on_matching_friend():
    with pytest.raises(ContactError, match="jobId"):
        parse_baseinfo_response(selected(), success({"baseInfoList": [relation(jobId=100002)]}))


def test_refresh_cannot_fall_back_to_search_security_when_relation_token_missing():
    bean = relation()
    del bean["securityId"]
    with pytest.raises(ContactError, match="securityId"):
        parse_baseinfo_response(selected(), success({"baseInfoList": [bean], "securityId": SEARCH_TOKEN}))


@pytest.mark.parametrize("bad", [None, {}, "entries", [None], [1], [{}], [{"friendId": None}]])
def test_refresh_malformed_list_is_not_accepted(bad):
    with pytest.raises(ContactError):
        parse_baseinfo_response(selected(), success({"baseInfoList": bad}))


@pytest.mark.parametrize("response", [None, [], {}, {"code": 7}, {"code": 36},
    {"code": "0", "zpData": {}}, {"code": False, "zpData": {}},
    {"code": 0, "zpData": None}, {"code": 0, "zpData": []},
    {"code": 0, "relation": relation()}, success({})])
def test_envelope_must_explicitly_succeed_and_hold_expected_schema(response):
    for parse in (parse_add_friend_response, parse_baseinfo_response):
        with pytest.raises(ContactError):
            parse(selected(), response)


def test_error_message_never_echoes_raw_server_message_or_tokens():
    response = {"code": 99, "message": SEARCH_TOKEN + CHAT_TOKEN + LID, "zpData": {}}
    with pytest.raises(ContactError) as raised:
        parse_add_friend_response(selected(), response)
    assert not any(value in str(raised.value) for value in (SEARCH_TOKEN, CHAT_TOKEN, LID))


def test_manual_context_constructor_cannot_bypass_target_job_or_source_checks():
    context = parse_relation(selected(), relation())
    for updates in ({"friend_id": 200003}, {"job_id": 100002},
                    {"security_id": ""}, {"friend_source": 65536},
                    {"selected_job": {}}):
        with pytest.raises(ContactError):
            replace(context, **updates)


def test_synthetic_parse_jobs_to_relation_end_to_end_without_transport(monkeypatch):
    from bosscli.client import BossClient, CARDLIST_PATH
    def no_transport(*args, **kwargs):
        pytest.fail("contact parsing must not perform HTTP")
    monkeypatch.setattr(BossClient, "_http", no_transport)
    raw_card = {**search_job(), "positionName": {"name": "Synthetic Python Engineer"},
                "brandName": "Synthetic Company", "bossName": "Synthetic Recruiter"}
    response = {"zpData": {CARDLIST_PATH: {"zpData": {"cardList": [
        {"positionSearchCardList": [raw_card]}]}}}}
    rows = BossClient.parse_jobs(response)
    choice = SelectedJob.from_search_job(rows[0])
    context = parse_add_friend_response(choice, success({"relation": relation()}))
    assert choice.add_friend_kwargs()["security_id"] == SEARCH_TOKEN
    assert context.security_id == CHAT_TOKEN and context.friend_id == raw_card["userId"]
