"""Safety gates for selection/confirmation; no real HTTP/IM calls or sessions."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bosscli import cli
from bosscli.client import CARDLIST_PATH


@pytest.fixture
def image(tmp_path):
    p = tmp_path / "portfolio.png"
    Image.new("RGB", (8, 12), "white").save(p)
    return p


class Client:
    s = {"uid": 42, "secretKey": "synthetic", "t2": "synthetic"}
    def __init__(self):
        self.closed = False
        self.http = SimpleNamespace(close=lambda: setattr(self, "closed", True))
    def search(self, query, city=None):
        return {"code": 0, "zpData": {CARDLIST_PATH: {"code": 0, "zpData": {}}}}
    def parse_jobs(self, response):
        return [{"name": "Python工程师", "company": "Example", "userId": 123,
                 "jobId": 456, "lid": "synthetic-lid", "securityId": "synthetic-search-token",
                 "hr": "示例招聘者", "salary": "20-30K", "city": "北京"}]


def setup(monkeypatch):
    c = Client()
    monkeypatch.setattr(cli, "_client", lambda args: c)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    import bosscli.im_sender as im
    def forbidden(*args, **kwargs):
        raise AssertionError("unconfirmed selection must not open an IM sender")
    monkeypatch.setattr(im, "ImageSender", forbidden)
    return c


@pytest.mark.parametrize("flags", [[], ["--confirm"], ["--select", "1"],
                                   ["--expected-job-id", "456", "--confirm"]])
def test_no_receiver_confirmation_means_no_write(image, monkeypatch, capsys, flags):
    c = setup(monkeypatch)
    cli.main(["outreach", "Python", "--image", str(image), *flags])
    output = capsys.readouterr().out
    assert "jobId=456" in output
    assert c.closed


def test_noninteractive_confirmation_must_pin_job_id(image, monkeypatch, capsys):
    c = setup(monkeypatch)
    with pytest.raises(SystemExit) as ex:
        cli.main(["outreach", "Python", "--image", str(image), "--select", "1", "--confirm"])
    assert ex.value.code == 2 and c.closed
    assert "expected-job-id" in capsys.readouterr().err


def test_changed_search_order_aborts_even_if_confirmed(image, monkeypatch, capsys):
    c = setup(monkeypatch)
    with pytest.raises(SystemExit) as ex:
        cli.main(["outreach", "Python", "--image", str(image), "--select", "1", "--expected-job-id", "999", "--confirm"])
    assert ex.value.code == 2 and c.closed
    assert "岗位已变化" in capsys.readouterr().err


def test_confirmed_workflow_calls_only_explicit_job(image, monkeypatch, capsys):
    c = setup(monkeypatch)
    import bosscli.im_sender as im
    import bosscli.outreach as workflow
    sender = object()
    monkeypatch.setattr(im, "ImageSender", lambda session: sender)
    calls = []
    def execute(client, chosen_sender, selected, prepared, **kw):
        calls.append((client, chosen_sender, selected, prepared, kw))
        return {"status": "sent", "server_mid": 789, "client_mid": 1234}
    monkeypatch.setattr(workflow, "execute_selected", execute)
    cli.main(["outreach", "Python", "--image", str(image), "--select", "1", "--expected-job-id", "456", "--expected-user-id", "123", "--confirm"])
    assert len(calls) == 1 and calls[0][2].job_id == 456 and calls[0][2].user_id == 123
    assert calls[0][4]["confirmed"] is True
    assert "serverMid=789" in capsys.readouterr().out and c.closed


def test_matching_job_but_changed_recruiter_aborts(image, monkeypatch, capsys):
    c = setup(monkeypatch)
    with pytest.raises(SystemExit) as ex:
        cli.main(["outreach", "Python", "--image", str(image), "--select", "1", "--expected-job-id", "456", "--expected-user-id", "999", "--confirm"])
    assert ex.value.code == 2 and c.closed
    assert "招聘者已变化" in capsys.readouterr().err


def test_tty_preconfirmed_without_double_pin_is_rejected(image, monkeypatch, capsys):
    c = setup(monkeypatch)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    with pytest.raises(SystemExit) as ex:
        cli.main(["outreach", "Python", "--image", str(image), "--select", "1", "--confirm"])
    assert ex.value.code == 2 and c.closed
    assert "expected-user-id" in capsys.readouterr().err


def test_confirmation_display_escapes_terminal_and_bidi_controls():
    value = "HR\x1b[2J\r\n\u202eSpoof\u202c"
    safe = cli._terminal_safe(value)
    assert "\x1b" not in safe and "\r" not in safe and "\n" not in safe
    assert "\u202e" not in safe and "\\u001b" in safe and "\\u202e" in safe


def test_write_phase_auth_expired_has_safe_message(image, monkeypatch, capsys):
    c = setup(monkeypatch)
    import bosscli.im_sender as im
    import bosscli.outreach as workflow
    monkeypatch.setattr(im, "ImageSender", lambda session: object())
    canary = "synthetic-secret-must-not-appear"
    def fail(*args, **kwargs):
        raise cli.AuthExpired(canary)
    monkeypatch.setattr(workflow, "execute_selected", fail)
    with pytest.raises(SystemExit) as ex:
        cli.main(["outreach", "Python", "--image", str(image), "--select", "1", "--expected-job-id", "456", "--expected-user-id", "123", "--confirm"])
    output = capsys.readouterr()
    assert ex.value.code == 2 and c.closed
    assert canary not in output.out + output.err and "登录状态已失效" in output.err


def test_eof_cancels_without_writing_or_traceback(image, monkeypatch, capsys):
    c = setup(monkeypatch)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    def eof(prompt):
        raise EOFError()
    monkeypatch.setattr("builtins.input", eof)
    cli.main(["outreach", "Python", "--image", str(image)])
    output = capsys.readouterr()
    assert "已取消" in output.out and not output.err and c.closed


def test_interrupt_after_write_started_is_not_reported_as_zero_writes(image, monkeypatch, capsys):
    c = setup(monkeypatch)
    import bosscli.im_sender as im
    import bosscli.outreach as workflow
    monkeypatch.setattr(im, "ImageSender", lambda session: object())
    def stop(*a, **k):
        raise KeyboardInterrupt()
    monkeypatch.setattr(workflow, "execute_selected", stop)
    with pytest.raises(SystemExit) as ex:
        cli.main(["outreach", "Python", "--image", str(image), "--select", "1", "--expected-job-id", "456", "--expected-user-id", "123", "--confirm"])
    output = capsys.readouterr()
    assert ex.value.code == 2 and "可能不确定" in output.err
    assert "未执行打招呼" not in output.out and c.closed


def test_tty_one_selection_then_one_yes(image, monkeypatch, capsys):
    c = setup(monkeypatch)
    import bosscli.im_sender as im
    import bosscli.outreach as workflow
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    prompts = []
    answers = iter(["1", "yes"])
    def answer(prompt):
        prompts.append(prompt)
        return next(answers)
    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr(im, "ImageSender", lambda session: object())
    calls = []
    monkeypatch.setattr(workflow, "execute_selected", lambda *a, **k: (calls.append(k) or {"status":"sent", "server_mid":789}))
    cli.main(["outreach", "Python", "--image", str(image)])
    assert len(prompts) == 2 and len(calls) == 1 and calls[0]["confirmed"]
    assert c.closed
