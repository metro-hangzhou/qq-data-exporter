from __future__ import annotations

from qq_data_cli.export_commands import ParsedExportCommand
from prompt_toolkit.buffer import Buffer, CompletionState
from prompt_toolkit.completion import Completion
from prompt_toolkit.document import Document
from prompt_toolkit.shortcuts import CompleteStyle

from qq_data_cli.repl import (
    SlashRepl,
    _completion_menu_reserve_lines,
    _completion_style_for_ui_profile,
    _is_login_completion_context,
    _navigate_completion_menu_without_inserting,
    _should_auto_refresh_completion,
    _should_select_first_completion,
)
from qq_data_cli.terminal_compat import CliUiProfile, TerminalProbe
from qq_data_integrations.napcat.models import NapCatLoginInfo, NapCatLoginStatus, NapCatQuickLoginAccount
from qq_data_integrations.napcat.runtime import NapCatStartResult


class _QuickLoginOnlyService:
    def __init__(self) -> None:
        self.quick_calls: list[str | None] = []

    def check_status(self) -> NapCatLoginStatus:
        return NapCatLoginStatus()

    def get_ready_login_info(self):
        return None

    def get_quick_login_candidates(self):
        return [NapCatQuickLoginAccount(uin="3956020260", nick_name="wiki")]

    def get_default_quick_login_uin(self):
        return "3956020260"

    def resolve_desired_quick_login_uin(self, *, preferred_uin: str | None = None):
        return preferred_uin or self.get_default_quick_login_uin()

    def try_quick_login(self, *, preferred_uin=None, **_kwargs):
        self.quick_calls.append(preferred_uin)
        return NapCatLoginInfo(uin="3956020260", nick="wiki", online=True)


class _MismatchedLoggedInService(_QuickLoginOnlyService):
    def check_status(self) -> NapCatLoginStatus:
        return NapCatLoginStatus(is_login=True)

    def get_ready_login_info(self):
        return NapCatLoginInfo(uin="1507833383", nick="other", online=True)


class _BrokenQuickLookupService(_QuickLoginOnlyService):
    def resolve_desired_quick_login_uin(self, *, preferred_uin: str | None = None):
        raise RuntimeError("quick lookup failed")

    def get_quick_login_candidates(self):
        raise RuntimeError("quick lookup failed")

    def login_until_success(self, **_kwargs):
        return NapCatLoginInfo(uin="3956020260", nick="wiki", online=True)


def test_repl_login_accepts_positional_quick_login_uin(monkeypatch) -> None:
    repl = SlashRepl()
    service = _QuickLoginOnlyService()

    monkeypatch.setattr(repl, "_ensure_endpoint_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(repl, "_refresh_settings", lambda: None)
    monkeypatch.setattr(repl, "_require_login_service", lambda: service)
    monkeypatch.setattr(repl, "_print_login_info", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(repl, "_prime_target_cache", lambda *_args, **_kwargs: None)

    repl._handle_login(["3956020260"])

    assert service.quick_calls == ["3956020260"]


def test_repl_login_reports_session_mismatch_for_requested_quick_uin(monkeypatch) -> None:
    repl = SlashRepl()
    service = _MismatchedLoggedInService()
    messages: list[str] = []

    monkeypatch.setattr(repl, "_ensure_endpoint_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(repl, "_refresh_settings", lambda: None)
    monkeypatch.setattr(repl, "_require_login_service", lambda: service)
    monkeypatch.setattr(repl._console, "print", lambda message, *args, **kwargs: messages.append(str(message)))

    repl._handle_login(["3956020260"])

    assert any("QQ session mismatch." in message for message in messages)


def test_repl_login_falls_back_to_qr_when_quick_lookup_errors(monkeypatch) -> None:
    repl = SlashRepl()
    service = _BrokenQuickLookupService()
    printed: list[str] = []

    monkeypatch.setattr(repl, "_ensure_endpoint_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(repl, "_refresh_settings", lambda: None)
    monkeypatch.setattr(repl, "_require_login_service", lambda: service)
    monkeypatch.setattr(repl, "_render_login_qr", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(repl, "_render_login_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(repl, "_print_login_info", lambda *_args, **_kwargs: printed.append("printed"))
    monkeypatch.setattr(repl, "_prime_target_cache", lambda *_args, **_kwargs: None)

    repl._handle_login([])

    assert printed == ["printed"]
    assert service.quick_calls == []


def test_repl_ensure_endpoint_ready_rejects_mismatched_session(monkeypatch) -> None:
    repl = SlashRepl()
    repl._settings = repl._settings.model_copy(update={"quick_login_uin": "3956020260"})
    service = _MismatchedLoggedInService()

    class _ReadyResult:
        ready = True
        already_running = False
        attempted_start = False
        attempted_configure = False
        message = ""

    monkeypatch.setattr(repl, "_require_bootstrapper", lambda: type("B", (), {"ensure_endpoint": lambda *_a, **_k: _ReadyResult()})())
    monkeypatch.setattr(repl, "_require_login_service", lambda: service)

    try:
        repl._ensure_endpoint_ready("onebot_http")
    except RuntimeError as exc:
        assert "QQ session mismatch." in str(exc)
        assert "requested_uin=3956020260" in str(exc)
    else:
        raise AssertionError("expected session mismatch runtime error")


def test_repl_export_tail_page_size_matches_cli_cap(monkeypatch) -> None:
    repl = SlashRepl()
    captured: dict[str, int] = {}

    class _Gateway:
        def fetch_snapshot_tail(self, _request, *, data_count, page_size, progress_callback=None):
            captured["data_count"] = data_count
            captured["page_size"] = page_size
            return "snapshot"

    monkeypatch.setattr(repl, "_require_gateway", lambda: _Gateway())

    parsed = ParsedExportCommand(
        chat_type="group",
        target_query="922065597",
        batch_target_queries=(),
        interval=None,
        fmt="jsonl",
        out_path=None,
        limit=2000,
        data_count=2000,
        profile="all",
        include_raw=False,
        refresh=False,
        strict_missing=None,
    )
    target = type("Target", (), {"chat_type": "group", "chat_id": "922065597", "display_name": "蕾米二次元萌萌群"})()

    result = repl._build_export_snapshot(parsed, target=target)

    assert result == "snapshot"
    assert captured == {"data_count": 2000, "page_size": 500}


def test_repl_quick_login_completion_returns_pinned_uin_without_blocking_on_service(monkeypatch) -> None:
    repl = SlashRepl()
    repl._settings = repl._settings.model_copy(update={"quick_login_uin": "3956020260"})

    def _boom():
        raise RuntimeError("should not synchronously query quick-login service during completion")

    monkeypatch.setattr(repl, "_require_login_service", _boom)

    results = repl._lookup_quick_login_candidates_for_completion(None, 10)

    assert [item.uin for item in results] == ["3956020260"]


def test_repl_quick_login_completion_prefers_napcat_candidates_before_local_pin(monkeypatch) -> None:
    repl = SlashRepl()
    repl._settings = repl._settings.model_copy(update={"quick_login_uin": "3956020260"})
    repl._quick_login_candidates_cache = [("1507833383", "blank"), ("3956020260", "wiki")]
    repl._quick_login_candidates_cached_at = 1.0

    monkeypatch.setattr("qq_data_cli.repl.monotonic", lambda: 2.0)

    results = repl._lookup_quick_login_candidates_for_completion(None, 10)

    assert [item.uin for item in results] == ["1507833383", "3956020260"]


def test_repl_quick_login_completion_background_prime_populates_cache(monkeypatch) -> None:
    repl = SlashRepl()

    class _Service:
        def get_quick_login_candidates(self):
            return [NapCatQuickLoginAccount(uin="3956020260", nick_name="wiki")]

    monkeypatch.setattr(repl, "_require_login_service", lambda: _Service())

    repl._kickoff_quick_login_candidates_prime_if_needed(announce=False)
    thread = repl._quick_login_candidates_prime_thread
    assert thread is not None
    thread.join(timeout=2.0)

    results = repl._lookup_quick_login_candidates_for_completion(None, 10)

    assert any(item.uin == "3956020260" for item in results)


def test_repl_quick_login_completion_background_prime_falls_back_to_active_login_info(monkeypatch) -> None:
    repl = SlashRepl()

    class _Service:
        def get_quick_login_candidates(self):
            return []

        def get_ready_login_info(self):
            return NapCatLoginInfo(uin="3956020260", nick="wiki", online=True)

        def get_login_info(self):
            return NapCatLoginInfo(uin="3956020260", nick="wiki", online=True)

    monkeypatch.setattr(repl, "_require_login_service", lambda: _Service())

    repl._kickoff_quick_login_candidates_prime_if_needed(announce=False)
    thread = repl._quick_login_candidates_prime_thread
    assert thread is not None
    thread.join(timeout=2.0)

    results = repl._lookup_quick_login_candidates_for_completion(None, 10)

    assert any(item.uin == "3956020260" for item in results)


def test_repl_quick_login_completion_empty_prime_does_not_mark_cache_fresh(monkeypatch) -> None:
    repl = SlashRepl()

    class _Service:
        def get_quick_login_candidates(self):
            return []

        def get_ready_login_info(self):
            return None

        def get_login_info(self):
            return NapCatLoginInfo()

    monkeypatch.setattr(repl, "_require_login_service", lambda: _Service())

    repl._kickoff_quick_login_candidates_prime_if_needed(announce=False)
    thread = repl._quick_login_candidates_prime_thread
    assert thread is not None
    thread.join(timeout=2.0)

    assert repl._quick_login_candidates_cache == []


def test_completion_menu_reserve_lines_grows_in_compat_mode() -> None:
    profile = CliUiProfile(
        mode="compat",
        show_completion_menu=True,
        complete_while_typing=True,
        watch_full_screen=False,
        use_custom_scrollbar=False,
        use_highlight_style=False,
    )
    probe = TerminalProbe(
        platform_system="Windows",
        windows_release="11",
        windows_version="10.0.26100",
        windows_build=26100,
        terminal_host="classic_console",
        shell_name="cmd",
        stdin_tty=True,
        stdout_tty=True,
        columns=120,
        lines=30,
        stdout_encoding="utf-8",
        preferred_encoding="utf-8",
        term=None,
        term_program=None,
        wt_session=False,
        vscode_terminal=False,
        conemu_session=False,
        ansicon_present=False,
        virtual_terminal_enabled=True,
        stdout_console_mode=7,
    )

    assert _completion_menu_reserve_lines(profile, probe) == 10


def test_completion_style_uses_column_menu_in_compat_mode() -> None:
    profile = CliUiProfile(
        mode="compat",
        show_completion_menu=True,
        complete_while_typing=True,
        watch_full_screen=False,
        use_custom_scrollbar=False,
        use_highlight_style=False,
    )

    assert _completion_style_for_ui_profile(profile) == CompleteStyle.COLUMN


def test_repl_startup_warm_napcat_service_uses_pinned_quick_login_uin(monkeypatch) -> None:
    repl = SlashRepl()
    repl._settings = repl._settings.model_copy(update={"quick_login_uin": "3956020260"})
    captured: dict[str, object] = {}
    printed: list[str] = []

    class _Bootstrapper:
        def ensure_endpoint(self, endpoint, **kwargs):
            captured["endpoint"] = endpoint
            captured["kwargs"] = kwargs
            return NapCatStartResult(endpoint="webui", ready=True, message="NapCat WebUI ready")

    monkeypatch.setattr(repl, "_require_bootstrapper", lambda: _Bootstrapper())
    monkeypatch.setattr(repl._console, "print", lambda message, *args, **kwargs: printed.append(str(message)))

    repl._warm_napcat_service_for_startup()

    assert captured["endpoint"] == "webui"
    assert captured["kwargs"]["quick_login_uin"] == "3956020260"
    assert "NapCat WebUI ready" in " ".join(printed)


def test_login_completion_never_auto_selects_first_candidate() -> None:
    assert _should_select_first_completion("/login") is False
    assert _should_select_first_completion("/login ") is False
    assert _should_select_first_completion("/login 39") is False
    assert _should_select_first_completion("/login --quick-uin") is False
    assert _should_select_first_completion("/login --quick-uin ") is False


def test_login_completion_context_detection_is_login_only() -> None:
    assert _is_login_completion_context("/login")
    assert _is_login_completion_context("/login ")
    assert _is_login_completion_context("/login --quick-uin ")
    assert not _is_login_completion_context("/watch group")


def test_login_completion_navigation_keeps_buffer_text_unchanged() -> None:
    buffer = Buffer(document=Document("/login ", cursor_position=len("/login ")))
    completions = [
        Completion(text="3956020260", start_position=0),
        Completion(text="1507833383", start_position=0),
        Completion(text="498603055", start_position=0),
    ]
    buffer.complete_state = CompletionState(buffer.document, completions, None)

    _navigate_completion_menu_without_inserting(buffer, direction=1)
    assert buffer.text == "/login "
    assert buffer.complete_state is not None
    assert buffer.complete_state.complete_index == 0

    _navigate_completion_menu_without_inserting(buffer, direction=1)
    assert buffer.text == "/login "
    assert buffer.complete_state is not None
    assert buffer.complete_state.complete_index == 1

    _navigate_completion_menu_without_inserting(buffer, direction=-1)
    assert buffer.text == "/login "
    assert buffer.complete_state is not None
    assert buffer.complete_state.complete_index == 0


def test_auto_refresh_completion_covers_root_commands_and_login_inputs() -> None:
    assert _should_auto_refresh_completion("/")
    assert _should_auto_refresh_completion("/l")
    assert _should_auto_refresh_completion("/login ")
    assert _should_auto_refresh_completion("/login 3")
    assert _should_auto_refresh_completion("/login --quick-uin")
    assert not _should_auto_refresh_completion("/watch group")
