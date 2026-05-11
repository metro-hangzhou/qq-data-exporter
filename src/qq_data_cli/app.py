from __future__ import annotations

import os
from pathlib import Path
from time import monotonic
import sys
from typing import TYPE_CHECKING

import typer

from qq_data_cli.logging_utils import get_cli_log_path, get_cli_logger, setup_cli_logging
from qq_data_cli.status_display import (
    colorize_status_fields_for_ansi,
    format_export_result_lines,
    format_prefetch_media_progress_line,
)
from qq_data_cli.terminal_compat import (
    apply_cli_ui_mode_override,
    probe_terminal_environment,
    read_requested_cli_ui_mode,
    render_terminal_doctor_lines,
    resolve_cli_ui_mode,
)
from qq_data_integrations.napcat.settings import NapCatSettings

if TYPE_CHECKING:
    from qq_data_core.export_perf import ExportPerfTraceWriter
    from qq_data_integrations.napcat.gateway import NapCatGateway
    from qq_data_integrations.napcat.models import ChatTarget

app = typer.Typer(
    help="QQ chat exporter developer CLI",
    invoke_without_command=True,
    no_args_is_help=False,
)

CLI_HISTORY_SINGLE_PAGE_LIMIT = 200


def _extract_state_dir_override(argv: list[str] | None = None) -> Path | None:
    args = list(argv if argv is not None else sys.argv[1:])
    if "export-history" not in args:
        return None
    last_value: str | None = None
    for index, token in enumerate(args):
        lowered = token.casefold()
        if lowered == "--state-dir":
            if index + 1 < len(args):
                last_value = args[index + 1]
        elif lowered.startswith("--state-dir="):
            last_value = token.split("=", 1)[1]
    return Path(last_value) if last_value else None


def _init_cli_logging(*, argv: list[str] | None = None) -> NapCatSettings:
    settings = NapCatSettings.from_env()
    state_dir_override = _extract_state_dir_override(argv)
    if state_dir_override is not None:
        settings = settings.model_copy(update={"state_dir": state_dir_override})
    log_path = setup_cli_logging(settings.state_dir)
    get_cli_logger("app").info(
        "cli_entry state_dir=%s project_root=%s log_path=%s",
        settings.state_dir,
        settings.project_root,
        log_path,
    )
    return settings


def _build_settings_loader(
    current_settings: NapCatSettings,
    *,
    pinned_updates: dict[str, object] | None = None,
):
    updates = dict(pinned_updates or {})

    def _loader() -> NapCatSettings:
        refreshed = NapCatSettings.from_env()
        if not updates:
            return refreshed
        return refreshed.model_copy(update=updates)

    return _loader


def _detect_expected_runtime_session_mismatch(settings: NapCatSettings) -> str | None:
    expected_uin = str(settings.quick_login_uin or "").strip()
    if not expected_uin:
        return None
    try:
        from qq_data_integrations.napcat.login import NapCatQrLoginService, detect_session_mismatch
        from qq_data_integrations.napcat.webui_client import NapCatWebUiClient

        client = NapCatWebUiClient(
            settings.webui_url,
            raw_token=settings.webui_token,
            use_system_proxy=settings.use_system_proxy,
        )
    except Exception:
        return None
    try:
        service = NapCatQrLoginService(client)
        return detect_session_mismatch(service, expected_uin=expected_uin)
    except Exception:
        return None
    finally:
        client.close()


def _describe_runtime_session(settings: NapCatSettings) -> str | None:
    try:
        from qq_data_integrations.napcat.login import NapCatQrLoginService
        from qq_data_integrations.napcat.webui_client import NapCatWebUiClient

        client = NapCatWebUiClient(
            settings.webui_url,
            raw_token=settings.webui_token,
            use_system_proxy=settings.use_system_proxy,
        )
    except Exception:
        return None
    try:
        service = NapCatQrLoginService(client)
        info = service.get_login_info()
    except Exception:
        return None
    finally:
        client.close()
    current_uin = str(getattr(info, "uin", "") or "").strip()
    nick = str(getattr(info, "nick", "") or "").strip() or "-"
    online = bool(getattr(info, "online", False))
    if not current_uin and not online:
        return "export_session: unavailable"
    return f"export_session: uin={current_uin or '-'} nick={nick} online={online}"


@app.callback(invoke_without_command=True)
def cli(
    ctx: typer.Context,
    ui: str | None = typer.Option(
        None,
        "--ui",
        help="CLI 显示模式：auto、full、compat。",
    ),
) -> None:
    settings = _init_cli_logging(argv=list(sys.argv[1:]))
    try:
        apply_cli_ui_mode_override(ui)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    terminal_probe = probe_terminal_environment()
    ui_decision = resolve_cli_ui_mode(
        terminal_probe,
        requested_mode=read_requested_cli_ui_mode(),
    )
    if ctx.invoked_subcommand is None:
        typer.echo("正在启动 CLI，请稍候...")
        from qq_data_cli.repl import SlashRepl
        from qq_data_cli.startup_capture import get_session_startup_capture_path

        SlashRepl(
            terminal_probe=terminal_probe,
            ui_decision=ui_decision,
            startup_capture_path=get_session_startup_capture_path(),
            defer_startup_capture=True,
        ).run()


@app.command()
def shell() -> None:
    if get_cli_log_path() is None:
        _init_cli_logging()
    typer.echo("正在启动 CLI，请稍候...")
    from qq_data_cli.repl import SlashRepl
    from qq_data_cli.startup_capture import get_session_startup_capture_path

    SlashRepl(
        startup_capture_path=get_session_startup_capture_path(),
        defer_startup_capture=True,
    ).run()


@app.command("terminal-doctor")
def terminal_doctor() -> None:
    if get_cli_log_path() is None:
        _init_cli_logging()
    probe = probe_terminal_environment()
    decision = resolve_cli_ui_mode(
        probe,
        requested_mode=read_requested_cli_ui_mode(),
    )
    typer.echo("\n".join(render_terminal_doctor_lines(probe, decision)))


@app.command()
def login(
    timeout: float = typer.Option(300.0, "--timeout"),
    poll: float = typer.Option(3.0, "--poll"),
    refresh: bool = typer.Option(False, "--refresh"),
    no_quick: bool = typer.Option(False, "--no-quick"),
    quick_uin: str | None = typer.Option(None, "--quick-uin"),
    webui_url: str | None = typer.Option(None, "--webui-url"),
    webui_token: str | None = typer.Option(None, "--webui-token"),
) -> None:
    from qq_data_integrations.napcat.bootstrap import NapCatBootstrapper
    from qq_data_integrations.napcat.login import (
        NapCatQrLoginService,
        build_session_mismatch_message,
    )
    from qq_data_integrations.napcat.webui_client import NapCatWebUiClient

    base_settings = NapCatSettings.from_env()
    settings = base_settings.model_copy(
        update={
            "quick_login_uin": quick_uin or base_settings.quick_login_uin,
            "webui_url": webui_url or base_settings.webui_url,
            "webui_token": webui_token if webui_token is not None else base_settings.webui_token,
        }
    )
    start_result = NapCatBootstrapper(
        settings,
        settings_loader=_build_settings_loader(
            settings,
            pinned_updates={
                "quick_login_uin": settings.quick_login_uin,
                "webui_url": settings.webui_url,
                "webui_token": settings.webui_token,
                "state_dir": settings.state_dir,
            },
        ),
    ).ensure_endpoint("webui", quick_login_uin=settings.quick_login_uin)

    if not start_result.ready:
        raise typer.BadParameter(start_result.message)
    if (start_result.attempted_start or start_result.attempted_configure) and start_result.message:
        typer.echo(start_result.message)
        refreshed_settings = NapCatSettings.from_env()
        settings = refreshed_settings.model_copy(
            update={
                "quick_login_uin": quick_uin or refreshed_settings.quick_login_uin,
                "webui_url": webui_url or refreshed_settings.webui_url,
                "webui_token": webui_token if webui_token is not None else refreshed_settings.webui_token,
            }
        )
    client = NapCatWebUiClient(
        settings.webui_url,
        raw_token=settings.webui_token,
        use_system_proxy=settings.use_system_proxy,
    )
    try:
        service = NapCatQrLoginService(client)
        from qq_data_cli.qr import build_login_qr_image_path, render_qr_text, write_qr_png

        def on_qr(url: str) -> None:
            qr_image_path = write_qr_png(
                url,
                build_login_qr_image_path(settings.project_root),
            )
            typer.echo(f"qr_image_path={qr_image_path}")
            typer.echo("请直接打开该图片扫码登录。")
            typer.echo(render_qr_text(url))
            typer.echo(f"qr_url={url}")

        def on_status(status) -> None:
            if status.login_error:
                typer.echo(f"login_status={status.login_error}")

        initial_status = service.check_status()
        expected_quick_uin = quick_uin or str(settings.quick_login_uin or "").strip() or None
        if initial_status.effectively_logged_in():
            info = service.get_ready_login_info()
            if info is not None:
                if expected_quick_uin and info.uin and info.uin != expected_quick_uin:
                    typer.echo(
                        build_session_mismatch_message(
                            current_uin=info.uin,
                            requested_uin=expected_quick_uin,
                        )
                    )
                    return
                typer.echo("QQ already logged in.")
                typer.echo(f"uin={info.uin or ''}")
                typer.echo(f"nick={info.nick or ''}")
                typer.echo(f"online={info.online}")
                return

        quick_candidate_label: str | None = None
        desired_quick_uin = None
        if not refresh and not no_quick:
            typer.echo("login_status=QQ not logged in; attempting quick login...")
            try:
                desired_quick_uin = service.resolve_desired_quick_login_uin(preferred_uin=quick_uin)
            except Exception:
                desired_quick_uin = expected_quick_uin
            try:
                candidates = service.get_quick_login_candidates()
            except Exception:
                candidates = []
            chosen_uin = desired_quick_uin or quick_uin
            if chosen_uin is None and candidates:
                chosen_uin = candidates[0].uin
            if chosen_uin:
                for candidate in candidates:
                    if candidate.uin == chosen_uin:
                        quick_candidate_label = candidate.display_label
                        break
                if quick_candidate_label is None:
                    quick_candidate_label = chosen_uin
                typer.echo(f"quick_login_candidate={quick_candidate_label}")
                quick_info = service.try_quick_login(
                    preferred_uin=chosen_uin,
                    timeout_seconds=min(timeout, 25.0),
                    poll_interval=min(max(poll, 0.5), 2.0),
                    on_status=on_status,
                )
                if quick_info is not None:
                    typer.echo("QQ quick login succeeded.")
                    typer.echo(f"uin={quick_info.uin or ''}")
                    typer.echo(f"nick={quick_info.nick or ''}")
                    typer.echo(f"online={quick_info.online}")
                    return
            typer.echo("login_status=quick login unavailable; preparing QR login...")

        info = service.login_until_success(
            timeout_seconds=timeout,
            poll_interval=poll,
            refresh=refresh,
            on_qrcode=on_qr,
            on_status=on_status,
        )
        typer.echo("QQ login succeeded.")
        typer.echo(f"uin={info.uin or ''}")
        typer.echo(f"nick={info.nick or ''}")
        typer.echo(f"online={info.online}")
    finally:
        client.close()


@app.command("export-fixture")
def export_fixture(
    fixture_path: Path,
    out_path: Path,
    fmt: str = typer.Option("jsonl", "--format", "-f"),
) -> None:
    from qq_data_core import ChatExportService, normalize_export_format
    from qq_data_integrations import FixtureSnapshotLoader, discover_qq_media_roots

    try:
        normalized_fmt = normalize_export_format(fmt)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--format") from exc
    loader = FixtureSnapshotLoader()
    service = ChatExportService()
    settings = NapCatSettings.from_env()
    media_roots = discover_qq_media_roots()
    snapshot = loader.load_export(fixture_path)
    normalized = service.build_snapshot(snapshot)
    bundle = service.write_bundle(
        normalized,
        out_path,
        fmt=normalized_fmt,
        media_search_roots=media_roots,
        media_cache_dir=settings.state_dir / "media_index",
    )
    typer.echo(str(bundle.data_path))


@app.command("export-history")
def export_history(
    chat_type: str,
    chat_ref: str,
    out_path: Path | None = typer.Option(None, "--out"),
    fmt: str = typer.Option("jsonl", "--format", "-f"),
    chat_name: str | None = typer.Option(None, "--chat-name"),
    http_url: str | None = typer.Option(None, "--http-url"),
    access_token: str | None = typer.Option(None, "--token"),
    limit: int = typer.Option(20, "--limit"),
    output_dir: Path | None = typer.Option(None, "--output-dir"),
    state_dir: Path | None = typer.Option(None, "--state-dir"),
    refresh: bool = typer.Option(False, "--refresh"),
    strict_missing: str | None = typer.Option(
        None,
        "--strict-missing",
        help="调试导出缺失策略：off、collect、abort、threshold:N。",
    ),
) -> None:
    from qq_data_cli.export_cleanup import cleanup_gateway_media_cache
    from qq_data_core import (
        ChatExportService,
        ExportForensicsCollector,
        ExportPerfTraceWriter,
        ExportRequest,
        build_default_output_path,
        build_export_content_summary,
        format_missing_retry_hints_compact,
        normalize_export_format,
        resolve_strict_missing_policy,
    )
    from qq_data_integrations.napcat.bootstrap import NapCatBootstrapper
    from qq_data_integrations.napcat.diagnostics import collect_debug_preflight_evidence
    from qq_data_integrations.napcat.gateway import NapCatGateway
    from qq_data_integrations.napcat.models import normalize_chat_type

    logger = get_cli_logger("app")
    try:
        normalized_fmt = normalize_export_format(fmt)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--format") from exc
    try:
        normalized_chat_type = normalize_chat_type(chat_type)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="chat_type") from exc
    base_settings = NapCatSettings.from_env()
    settings = base_settings.model_copy(
        update={
            "http_url": http_url or base_settings.http_url,
            "access_token": access_token if access_token is not None else base_settings.access_token,
            "export_dir": output_dir or base_settings.export_dir,
            "state_dir": state_dir or base_settings.state_dir,
        }
    )
    start_result = NapCatBootstrapper(
        settings,
        settings_loader=_build_settings_loader(
            settings,
            pinned_updates={
                "http_url": settings.http_url,
                "access_token": settings.access_token,
                "export_dir": settings.export_dir,
                "state_dir": settings.state_dir,
            },
        ),
    ).ensure_endpoint("onebot_http")
    if not start_result.ready:
        raise typer.BadParameter(start_result.message)
    if start_result.message and (start_result.attempted_start or start_result.attempted_configure):
        typer.echo(f"runtime_note: {start_result.message}", err=True)
    if start_result.attempted_start or start_result.attempted_configure:
        refreshed_settings = NapCatSettings.from_env()
        settings = refreshed_settings.model_copy(
            update={
                "http_url": http_url or refreshed_settings.http_url,
                "access_token": access_token if access_token is not None else refreshed_settings.access_token,
                "export_dir": output_dir or refreshed_settings.export_dir,
                "state_dir": state_dir or refreshed_settings.state_dir,
            }
        )
    mismatch_message = _detect_expected_runtime_session_mismatch(settings)
    if mismatch_message:
        raise typer.BadParameter(mismatch_message)
    session_line = _describe_runtime_session(settings)
    if session_line:
        typer.echo(session_line, err=True)
    gateway = NapCatGateway(settings)
    trace = None
    forensics = None
    try:
        target_resolution_started = monotonic()
        target = _resolve_target(gateway, normalized_chat_type, chat_ref, chat_name=chat_name, refresh=refresh)
        trace = ExportPerfTraceWriter(
            settings.state_dir,
            chat_type=normalized_chat_type,
            chat_id=target.chat_id,
            mode="cli_export",
        )
        trace.write_event(
            "pipeline_stage",
            {
                "stage": "app.target_resolve",
                "status": "done",
                "elapsed_s": round(monotonic() - target_resolution_started, 4),
                "chat_type": normalized_chat_type,
                "query": chat_ref,
                "resolved_chat_id": target.chat_id,
                "resolved_chat_name": target.display_name,
                "refresh": refresh,
            },
        )
        progress_callback = _build_cli_export_progress_callback(trace)
        forensics = ExportForensicsCollector(
            settings.state_dir,
            chat_type=normalized_chat_type,
            chat_id=target.chat_id,
            policy=resolve_strict_missing_policy(strict_missing, env=os.environ),
            command_context={
                "entrypoint": "app.export-history",
                "format": normalized_fmt,
                "limit": limit,
                "strict_missing": strict_missing,
                "chat_ref": chat_ref,
                "chat_name": chat_name or target.display_name,
            },
        )
        forensics.capture_preflight(
            {
                "http_url": settings.http_url,
                "fast_history_mode": settings.fast_history_mode,
                "fast_history_url": settings.fast_history_url,
                "export_dir": str(settings.export_dir),
                "state_dir": str(settings.state_dir),
                "project_root": str(settings.project_root),
                **collect_debug_preflight_evidence(settings),
            }
        )
        trace.write_event(
            "export_start",
            {
                "chat_name": target.display_name,
                "format": normalized_fmt,
                "limit": limit,
                "target_dir": str((out_path.parent if out_path is not None else settings.export_dir).resolve()),
            },
        )
        request = ExportRequest(
            chat_type=normalized_chat_type,
            chat_id=target.chat_id,
            chat_name=chat_name or target.display_name,
            limit=limit,
        )
        history_page_size = max(100, min(limit, 500))
        with trace.timed_stage(
            "app.fetch_snapshot",
            payload={
                "chat_type": normalized_chat_type,
                "chat_id": target.chat_id,
                "limit": limit,
                "history_page_size": history_page_size,
            },
        ) as fetch_stage:
            if limit > CLI_HISTORY_SINGLE_PAGE_LIMIT:
                source_snapshot = gateway.fetch_snapshot_tail(
                    request,
                    data_count=limit,
                    page_size=history_page_size,
                    progress_callback=progress_callback,
                )
                fetch_stage.add(fetch_mode="tail")
            else:
                source_snapshot = gateway.fetch_snapshot(request, progress_callback=progress_callback)
                fetch_stage.add(fetch_mode="single_page")
            fetch_stage.add(
                source_history_source=str(source_snapshot.metadata.get("source") or ""),
                source_message_count=len(source_snapshot.messages),
            )
        service = ChatExportService()
        with trace.timed_stage(
            "app.normalize_snapshot",
            payload={"source_message_count": len(source_snapshot.messages)},
        ) as normalize_stage:
            normalized = service.build_snapshot(source_snapshot)
            normalize_stage.add(normalized_message_count=len(normalized.messages))
        target_path = out_path or build_default_output_path(
            settings.export_dir,
            chat_type=normalized_chat_type,
            chat_id=target.chat_id,
            fmt=normalized_fmt,
        )
        with trace.timed_stage(
            "app.write_bundle",
            payload={
                "target_path": str(target_path),
                "format": normalized_fmt,
                "normalized_message_count": len(normalized.messages),
            },
        ) as bundle_stage:
            bundle = service.write_bundle(
                normalized,
                target_path,
                fmt=normalized_fmt,
                media_resolution_mode="napcat_only",
                media_download_manager=(
                    gateway.build_media_download_manager()
                    if hasattr(gateway, "build_media_download_manager")
                    else None
                ),
                progress_callback=progress_callback,
                forensics_collector=forensics,
            )
            bundle_stage.add(
                copied_asset_count=bundle.copied_asset_count,
                reused_asset_count=bundle.reused_asset_count,
                missing_asset_count=bundle.missing_asset_count,
                error_asset_count=bundle.error_asset_count,
            )
        with trace.timed_stage("app.cleanup_remote_cache") as cleanup_stage:
            cleanup_stats = cleanup_gateway_media_cache(gateway, trace=trace, logger=logger)
            cleanup_stage.add(**cleanup_stats)
        with trace.timed_stage(
            "app.build_export_content_summary",
            payload={"record_count": len(normalized.messages)},
        ):
            content_summary = build_export_content_summary(
                normalized,
                bundle,
                profile="all",
                fmt=normalized_fmt,
                strict_missing=strict_missing,
            )
        with trace.timed_stage(
            "app.build_perf_summary",
            payload={"record_count": len(normalized.messages)},
        ):
            summary = trace.build_summary(record_count=len(normalized.messages))
        trace.write_event(
            "export_complete",
            {
                "out_path": str(bundle.data_path.resolve()),
                "manifest_path": str(bundle.manifest_path.resolve()),
                "copied_asset_count": bundle.copied_asset_count,
                "reused_asset_count": bundle.reused_asset_count,
                "missing_asset_count": bundle.missing_asset_count,
                "remote_cache_cleanup": cleanup_stats,
                "content_summary": content_summary,
                **summary,
            },
        )
        forensic_summary_path = None
        if forensics is not None and forensics.enabled:
            with trace.timed_stage(
                "app.forensics_finalize",
                payload={"incident_count": forensics.incident_count},
            ) as forensic_stage:
                forensic_summary_path = forensics.finalize(
                    export_completed=True,
                    aborted=False,
                    data_path=bundle.data_path,
                    manifest_path=bundle.manifest_path,
                    trace_path=trace.path,
                    log_path=get_cli_log_path(),
                )
                forensic_stage.add(summary_path=str(forensic_summary_path) if forensic_summary_path else None)
        if forensic_summary_path is not None:
            bundle.forensic_summary_path = forensic_summary_path
            bundle.forensic_run_dir = forensic_summary_path.parent
            bundle.forensic_incident_count = forensics.incident_count
        trace.close()
        report_path = trace.persist_report(record_count=len(normalized.messages))
        zero_result_hint = _build_zero_result_hint(gateway, target=target, record_count=len(normalized.messages))
        if zero_result_hint:
            typer.echo(zero_result_hint, err=True)
        typer.echo(str(bundle.data_path))
        session_line = _describe_runtime_session(settings)
        for line in format_export_result_lines(
            session_line=session_line,
            content_summary=content_summary,
            bundle=bundle,
            trace_summary=summary,
            trace_path=trace.path,
        ):
            typer.echo(colorize_status_fields_for_ansi(line), err=True)
        typer.echo(f"perf_report={report_path}", err=True)
        for retry_hint in format_missing_retry_hints_compact(content_summary, shell="cli"):
            typer.echo(colorize_status_fields_for_ansi(retry_hint), err=True)
        if int(getattr(bundle, "forensic_incident_count", 0) or 0):
            typer.echo(
                f"forensics: incidents={getattr(bundle, 'forensic_incident_count', 0)} "
                f"summary={getattr(bundle, 'forensic_summary_path', None)}",
                err=True,
            )
            typer.echo(
                "send_back: "
                f"manifest={bundle.manifest_path} "
                f"trace={trace.path} "
                f"forensic_summary={getattr(bundle, 'forensic_summary_path', None)} "
                f"log={get_cli_log_path()}",
                err=True,
            )
    except Exception as exc:
        if trace is not None:
            cleanup_stats = cleanup_gateway_media_cache(gateway, trace=trace, logger=logger)
            trace.write_event(
                "export_failed",
                {
                    "error": str(exc),
                    "remote_cache_cleanup": cleanup_stats,
                },
            )
            if forensics is not None and forensics.enabled:
                with trace.timed_stage(
                    "app.forensics_finalize",
                    payload={"incident_count": forensics.incident_count, "failure": True},
                ):
                    forensics.finalize(
                        export_completed=False,
                        aborted="strict missing aborted export" in str(exc).casefold(),
                        trace_path=trace.path,
                        log_path=get_cli_log_path(),
                        error=str(exc),
                    )
            trace.close()
        raise
    finally:
        gateway.close()


def _resolve_target(
    gateway: "NapCatGateway",
    chat_type: str,
    query: str,
    *,
    chat_name: str | None,
    refresh: bool,
) -> "ChatTarget":
    from qq_data_integrations.napcat.directory import NapCatTargetLookupError
    from qq_data_integrations.napcat.models import ChatTarget

    if query.isdigit():
        try:
            return gateway.resolve_target(chat_type, query, refresh_if_missing=True)
        except NapCatTargetLookupError:
            return ChatTarget(chat_type=chat_type, chat_id=query, name=chat_name or query)
    return gateway.resolve_target(chat_type, query, refresh_if_missing=True)


def _build_zero_result_hint(
    gateway: "NapCatGateway",
    *,
    target: "ChatTarget",
    record_count: int,
) -> str | None:
    if record_count > 0:
        return None
    from qq_data_integrations.napcat.directory import NapCatTargetLookupError

    chat_id = str(target.chat_id or "").strip()
    if not chat_id:
        return None
    try:
        gateway.resolve_target(target.chat_type, chat_id, refresh_if_missing=True)
    except NapCatTargetLookupError:
        if target.chat_type == "group":
            return (
                "zero_result_hint: 当前在线账号的 NapCat 群列表里解析不到这个群。"
                " 这通常意味着你不在该群、当前登录的 QQ 不是预期账号，或元数据缓存/会话视角不一致。"
            )
        return (
            "zero_result_hint: 当前在线账号的 NapCat 好友列表里解析不到这个好友目标。"
            " 这通常意味着当前登录的 QQ 不是预期账号，或好友元数据/会话视角不一致。"
        )
    return (
        "zero_result_hint: 目标在当前账号视角下可解析，但本次请求切片返回了 0 条消息。"
        " 这更像是当前窗口本来就没有近期消息，而不是 exporter 自身崩溃。"
    )


def _build_cli_export_progress_callback(trace: "ExportPerfTraceWriter"):
    state: dict[str, object] = {
        "last_text": "",
        "last_emit": 0.0,
        "last_pages": -1,
        "last_current": -1,
        "last_download_text": "",
        "last_download_emit": 0.0,
        "last_download_completed": -1,
    }

    def callback(update: dict[str, object]) -> None:
        phase = str(update.get("phase") or "progress")
        trace.write_event(phase, update)
        text = _format_cli_export_progress(update)
        if not text:
            return
        now = monotonic()
        if phase == "download_assets":
            stage = str(update.get("stage") or "progress")
            completed = int(update.get("completed") or update.get("download_completed") or 0)
            total = int(update.get("candidate_total") or update.get("download_total") or 0)
            last_completed = int(state.get("last_download_completed") or -1)
            if (
                stage == "progress"
                and completed < total
                and completed >= last_completed
                and completed - last_completed < 4
                and now - float(state.get("last_download_emit") or 0.0) < 0.2
            ):
                return
            if (
                text == state.get("last_download_text")
                and now - float(state.get("last_download_emit") or 0.0) < 0.75
            ):
                return
            state["last_download_completed"] = completed
            state["last_download_text"] = text
            state["last_download_emit"] = now
            typer.echo(colorize_status_fields_for_ansi(text, stream=sys.stderr), err=True)
            return
        if phase == "materialize_assets":
            current = int(update.get("current") or 0)
            total = int(update.get("total") or 0)
            last_current = int(state.get("last_current") or -1)
            if (
                current < total
                and current > last_current
                and current - last_current < 8
                and now - float(state.get("last_emit") or 0.0) < 0.25
            ):
                return
            state["last_current"] = current
        elif phase in {"bounds_scan", "interval_scan", "interval_tail_scan", "tail_scan", "full_scan"}:
            pages = int(update.get("pages_scanned") or 0)
            last_pages = int(state.get("last_pages") or -1)
            if pages == last_pages and now - float(state.get("last_emit") or 0.0) < 0.75:
                return
            state["last_pages"] = pages
        elif text == state.get("last_text"):
            return
        state["last_text"] = text
        state["last_emit"] = now
        typer.echo(colorize_status_fields_for_ansi(text, stream=sys.stderr), err=True)

    return callback


def _format_cli_export_progress(update: dict[str, object]) -> str | None:
    phase = str(update.get("phase") or "")
    elapsed_s = float(update.get("elapsed_s") or 0.0)
    rate_suffix = (
        f" rate={float(update.get('rate_per_s') or 0.0):.1f}/s elapsed={elapsed_s:.1f}s"
        if elapsed_s > 0
        else ""
    )
    pages_scanned = int(update.get("pages_scanned") or 0)

    if phase in {"interval_scan", "interval_tail_scan", "tail_scan", "full_scan"}:
        page_size = int(update.get("page_size") or 0)
        page_duration_s = float(update.get("page_duration_s") or 0.0)
        history_source = str(update.get("history_source") or "")
        duration_label = "page"
        if history_source == "napcat_fast_history_bulk":
            duration_label = "bulk"
        if phase == "full_scan":
            collected_messages = int(update.get("collected_messages") or 0)
            detail = (
                f"export_progress: scanning full history pages={pages_scanned} "
                f"collected={collected_messages} page_size={page_size} {duration_label}={page_duration_s:.2f}s"
            )
        elif phase == "interval_scan":
            matched_messages = int(update.get("matched_messages") or 0)
            detail = (
                f"export_progress: scanning interval pages={pages_scanned} "
                f"matched={matched_messages} page_size={page_size} {duration_label}={page_duration_s:.2f}s"
            )
        else:
            matched_messages = int(update.get("matched_messages") or 0)
            requested_data_count = int(update.get("requested_data_count") or 0)
            label = "scanning recent tail" if phase == "tail_scan" else "scanning interval tail"
            detail = (
                f"export_progress: {label} pages={pages_scanned} "
                f"matched={matched_messages}/{requested_data_count} page_size={page_size} {duration_label}={page_duration_s:.2f}s"
            )
        if rate_suffix:
            detail += rate_suffix
        return detail

    if phase == "write_data_file":
        stage = str(update.get("stage") or "start")
        record_count = int(update.get("record_count") or 0)
        status = "success" if stage == "done" else "in progress"
        action = "wrote" if stage == "done" else "writing"
        return f"status={status} export_progress: {action} data file records={record_count}"

    if phase in {"prefetch_media", "prefetch_media_prepare", "prefetch_media_chunk"}:
        return format_prefetch_media_progress_line(update)

    if phase == "materialize_assets":
        current = int(update.get("current") or 0)
        total = int(update.get("total") or 0)
        asset_type = str(update.get("asset_type") or "-")
        asset_role = str(update.get("asset_role") or "").strip()
        role_suffix = f".{asset_role}" if asset_role else ""
        copied = int(update.get("copied_assets") or 0)
        reused = int(update.get("reused_assets") or 0)
        missing = int(update.get("missing_assets") or 0)
        errors = int(update.get("error_assets") or 0)
        detail = (
            f"status=in progress export_progress: materializing assets {current}/{total} "
            f"{asset_type}{role_suffix} copied={copied} reused={reused} missing={missing} err={errors}"
        )
        if elapsed_s > 0 and current > 0:
            detail += f" rate={current / elapsed_s:.1f}/s elapsed={elapsed_s:.1f}s"
        return detail

    if phase == "download_assets":
        stage = str(update.get("stage") or "progress")
        total = int(update.get("candidate_total") or update.get("download_total") or 0)
        queued = int(update.get("queued") or 0)
        active = int(update.get("active") or update.get("download_inflight") or 0)
        completed = int(update.get("completed") or update.get("download_completed") or 0)
        failed = int(update.get("failed") or update.get("download_failed") or 0)
        cached = int(update.get("cached") or update.get("download_cached") or 0)
        eager = int(update.get("eager_remote_candidates") or 0)
        token = int(update.get("public_token_candidates") or 0)
        context = int(update.get("context_candidates") or 0)
        timeout_count = int(update.get("timeout_count") or 0)
        forward_context_timeouts = int(update.get("forward_context_timeout_count") or 0)
        forward_context_empty = int(update.get("forward_context_empty_count") or 0)
        forward_context_errors = int(update.get("forward_context_error_count") or 0)
        forward_context_unavailable = int(update.get("forward_context_unavailable_count") or 0)
        forward_timeout_storm_skips = int(update.get("forward_timeout_storm_skip_count") or 0)
        last_asset_type = str(update.get("last_asset_type") or "").strip()
        last_file_name = str(update.get("last_file_name") or "").strip()
        last_status = str(update.get("last_status") or "").strip()
        if stage == "done" and not total:
            return None
        status = {
            "start": "in progress",
            "progress": "in progress",
            "done": "success",
            "error": "failed",
        }.get(stage, "in progress")
        parts = [f"status={status}", f"remote_downloads(subqueue): {stage}"]
        parts.append(f"candidates={total}")
        parts.append(f"ok={completed}")
        parts.append(f"cached={cached}")
        parts.append(f"failed={failed}")
        parts.append(f"queued={queued}")
        parts.append(f"active={active}")
        if stage == "start":
            parts.append(f"sources=eager:{eager}/token:{token}/context:{context}")
        if last_asset_type and last_status:
            last_label = last_asset_type
            if last_file_name:
                last_label = f"{last_label}:{last_file_name}"
            parts.append(f"last={last_status}@{last_label}")
        diag_parts: list[str] = []
        if timeout_count > 0:
            diag_parts.append(f"timeouts={timeout_count}")
        if forward_context_timeouts > 0:
            diag_parts.append(f"forward_meta_timeout={forward_context_timeouts}")
        if forward_context_empty > 0:
            diag_parts.append(f"forward_meta_empty={forward_context_empty}")
        if forward_context_errors > 0:
            diag_parts.append(f"forward_meta_error={forward_context_errors}")
        if forward_context_unavailable > 0:
            diag_parts.append(f"forward_meta_unavailable={forward_context_unavailable}")
        if forward_timeout_storm_skips > 0:
            diag_parts.append(f"forward_timeout_breaker={forward_timeout_storm_skips}")
        if diag_parts:
            parts.append("diag=" + ",".join(diag_parts))
        return " ".join(parts)

    if phase == "forensic_incident" and str(update.get("stage") or "") == "recorded":
        if not bool(update.get("is_new_incident")):
            return None
        incident_id = str(update.get("incident_id") or "-")
        reason_category = str(update.get("reason_category") or "unknown")
        asset_type = str(update.get("asset_type") or "-")
        file_name = str(update.get("file_name") or "").strip() or "-"
        incident_path = str(update.get("incident_path") or "").strip()
        detail = (
            f"export_incident: {incident_id} reason={reason_category} "
            f"asset={asset_type}:{file_name}"
        )
        if incident_path:
            detail += f" forensic={incident_path}"
        return detail

    if phase == "materialize_asset_substep" and str(update.get("stage") or "") == "done":
        status = str(update.get("status") or "")
        if status not in {"timeout", "unavailable", "storm_skip"}:
            return None
        substep = str(update.get("substep") or "-")
        asset_type = str(update.get("asset_type") or "-")
        file_name = str(update.get("file_name") or "").strip() or "-"
        timeout_s = float(update.get("timeout_s") or 0.0)
        elapsed = float(update.get("elapsed_s") or 0.0)
        detail = (
            f"status=in progress export_progress: asset substep {status} substep={substep} "
            f"asset={asset_type}:{file_name}"
        )
        if timeout_s > 0:
            detail += f" timeout={timeout_s:.1f}s"
        if elapsed > 0:
            detail += f" elapsed={elapsed:.1f}s"
        detail += " continuing=1"
        return detail
    return None


def main() -> None:
    app()


if __name__ == "__main__":
    main()
