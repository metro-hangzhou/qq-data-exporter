from __future__ import annotations

from qq_data_cli import app as cli_app
from qq_data_integrations.napcat.directory import NapCatTargetLookupError
from qq_data_integrations.napcat.models import NapCatLoginInfo, NapCatLoginStatus
from qq_data_integrations.napcat.models import ChatTarget
from qq_data_integrations.napcat.settings import NapCatSettings


class _BootstrapResult:
    ready = True
    attempted_start = False
    attempted_configure = False
    message = ""


class _Bootstrapper:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def ensure_endpoint(self, _endpoint: str, **_kwargs) -> _BootstrapResult:
        return _BootstrapResult()


class _WebUiClient:
    def __init__(self, *_args, **_kwargs) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _QuickLoginService:
    def __init__(self, _client) -> None:
        self.quick_calls: list[str | None] = []
        self.qr_calls = 0
        self.status_checks = 0

    def check_status(self) -> NapCatLoginStatus:
        self.status_checks += 1
        return NapCatLoginStatus()

    def get_login_info(self) -> NapCatLoginInfo:
        return NapCatLoginInfo(uin="3956020260", nick="wiki", online=True)

    def get_ready_login_info(self) -> NapCatLoginInfo | None:
        info = self.get_login_info()
        return info if info.is_usable_session() else None

    def get_quick_login_candidates(self):
        return []

    def get_default_quick_login_uin(self) -> str | None:
        return "3956020260"

    def resolve_desired_quick_login_uin(self, *, preferred_uin: str | None = None) -> str | None:
        return preferred_uin or self.get_default_quick_login_uin()

    def try_quick_login(self, *, preferred_uin=None, **_kwargs):
        self.quick_calls.append(preferred_uin)
        return NapCatLoginInfo(uin="3956020260", nick="wiki", online=True)

    def login_until_success(self, **_kwargs):
        self.qr_calls += 1
        return NapCatLoginInfo(uin="3956020260", nick="wiki", online=True)


class _QrOnlyService(_QuickLoginService):
    def try_quick_login(self, *, preferred_uin=None, **_kwargs):
        self.quick_calls.append(preferred_uin)
        return None


class _AlreadyLoggedInService(_QuickLoginService):
    def check_status(self) -> NapCatLoginStatus:
        self.status_checks += 1
        return NapCatLoginStatus(is_login=True)


class _DifferentLoggedInService(_QuickLoginService):
    def check_status(self) -> NapCatLoginStatus:
        self.status_checks += 1
        return NapCatLoginStatus(is_login=True)

    def get_login_info(self) -> NapCatLoginInfo:
        return NapCatLoginInfo(uin="1507833383", nick="other", online=True)


class _GhostLoggedInService(_QuickLoginService):
    def check_status(self) -> NapCatLoginStatus:
        self.status_checks += 1
        return NapCatLoginStatus(is_login=True)

    def get_login_info(self) -> NapCatLoginInfo:
        return NapCatLoginInfo(uin=None, nick=None, online=None)


def _patch_login_stack(monkeypatch, service_cls) -> None:
    settings = NapCatSettings.from_env()
    monkeypatch.setattr(NapCatSettings, "from_env", classmethod(lambda cls: settings))
    import qq_data_integrations.napcat.bootstrap as bootstrap_module
    import qq_data_integrations.napcat.login as login_module
    import qq_data_integrations.napcat.webui_client as webui_module

    monkeypatch.setattr(bootstrap_module, "NapCatBootstrapper", _Bootstrapper)
    monkeypatch.setattr(login_module, "NapCatQrLoginService", service_cls)
    monkeypatch.setattr(webui_module, "NapCatWebUiClient", _WebUiClient)


def test_cli_login_prefers_quick_login(monkeypatch, capsys) -> None:
    _patch_login_stack(monkeypatch, _QuickLoginService)

    cli_app.login(timeout=10.0, poll=1.0, refresh=False, no_quick=False, quick_uin=None)

    output = capsys.readouterr().out
    assert "QQ quick login succeeded." in output
    assert "uin=3956020260" in output
    assert "QQ login succeeded." not in output


def test_cli_login_can_skip_quick_login(monkeypatch, capsys) -> None:
    _patch_login_stack(monkeypatch, _QrOnlyService)

    cli_app.login(timeout=10.0, poll=1.0, refresh=False, no_quick=True, quick_uin=None)

    output = capsys.readouterr().out
    assert "QQ quick login succeeded." not in output
    assert "QQ login succeeded." in output


def test_cli_login_reports_existing_session_without_claiming_quick_login(monkeypatch, capsys) -> None:
    _patch_login_stack(monkeypatch, _AlreadyLoggedInService)

    cli_app.login(timeout=10.0, poll=1.0, refresh=False, no_quick=False, quick_uin=None)

    output = capsys.readouterr().out
    assert "QQ already logged in." in output
    assert "QQ quick login succeeded." not in output
    assert "quick_login_candidate=" not in output


def test_cli_login_reports_session_mismatch_for_requested_quick_uin(monkeypatch, capsys) -> None:
    _patch_login_stack(monkeypatch, _DifferentLoggedInService)

    cli_app.login(timeout=10.0, poll=1.0, refresh=False, no_quick=False, quick_uin="3956020260")

    output = capsys.readouterr().out
    assert "QQ session mismatch." in output
    assert "current_uin=1507833383" in output
    assert "requested_uin=3956020260" in output
    assert "QQ already logged in." not in output


def test_cli_login_does_not_claim_existing_session_when_login_info_is_blank(monkeypatch, capsys) -> None:
    _patch_login_stack(monkeypatch, _GhostLoggedInService)

    cli_app.login(timeout=10.0, poll=1.0, refresh=False, no_quick=False, quick_uin=None)

    output = capsys.readouterr().out
    assert "QQ already logged in." not in output


def test_detect_expected_runtime_session_mismatch_reports_wrong_account(monkeypatch) -> None:
    _patch_login_stack(monkeypatch, _DifferentLoggedInService)
    settings = NapCatSettings.from_env().model_copy(update={"quick_login_uin": "3956020260"})

    message = cli_app._detect_expected_runtime_session_mismatch(settings)

    assert message is not None
    assert "QQ session mismatch." in message
    assert "current_uin=1507833383" in message
    assert "requested_uin=3956020260" in message


def test_describe_runtime_session_reports_current_account(monkeypatch) -> None:
    _patch_login_stack(monkeypatch, _QuickLoginService)
    settings = NapCatSettings.from_env()

    line = cli_app._describe_runtime_session(settings)

    assert line == "export_session: uin=3956020260 nick=wiki online=True"


class _ResolvableGateway:
    def resolve_target(self, _chat_type: str, _chat_id: str, *, refresh_if_missing: bool = True):
        return ChatTarget(chat_type="group", chat_id="922065597", name="蕾米二次元萌萌群")


class _MissingTargetGateway:
    def resolve_target(self, _chat_type: str, _chat_id: str, *, refresh_if_missing: bool = True):
        raise NapCatTargetLookupError("missing target")


def test_build_zero_result_hint_reports_missing_group_target() -> None:
    hint = cli_app._build_zero_result_hint(
        _MissingTargetGateway(),
        target=ChatTarget(chat_type="group", chat_id="751365230", name="史数据统计群"),
        record_count=0,
    )

    assert hint is not None
    assert "群列表里解析不到这个群" in hint


def test_build_zero_result_hint_reports_resolved_empty_slice() -> None:
    hint = cli_app._build_zero_result_hint(
        _ResolvableGateway(),
        target=ChatTarget(chat_type="group", chat_id="922065597", name="蕾米二次元萌萌群"),
        record_count=0,
    )

    assert hint is not None
    assert "返回了 0 条消息" in hint
