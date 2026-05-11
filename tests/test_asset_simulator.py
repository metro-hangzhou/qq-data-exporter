from __future__ import annotations

from qq_data_integrations.napcat.asset_simulator import (
    AssetResolutionScenario,
    all_asset_resolution_scenarios,
    default_asset_resolution_pair_cases,
    default_asset_resolution_triplet_cases,
    default_cross_run_reset_cases,
    default_asset_resolution_scenarios,
    default_direct_file_id_scope_cases,
    default_future_local_identity_promotion_cases,
    default_forward_candidate_priority_cases,
    default_forward_parent_public_timeout_scope_cases,
    default_second_pass_gate_cases,
    default_public_timeout_scope_cases,
    default_shared_outcome_scope_cases,
    default_forward_timeout_matrix,
    run_cross_run_reset_matrix,
    run_asset_resolution_pair_matrix,
    run_asset_resolution_triplet_matrix,
    run_forward_candidate_priority_case,
    run_forward_candidate_priority_matrix,
    run_forward_parent_public_timeout_scope_matrix,
    run_direct_file_id_scope_matrix,
    run_future_local_identity_promotion_matrix,
    run_public_timeout_scope_matrix,
    run_prefetch_planning_matrix,
    run_asset_resolution_matrix,
    run_asset_resolution_sequence,
    run_asset_resolution_scenario,
    run_forward_timeout_simulation,
    run_second_pass_gate_matrix,
    run_shared_outcome_scope_matrix,
    exact_friend_speech_current_reduction_scenarios,
    historical_exact_friend_speech_reference_scenarios,
    summarize_forward_candidate_priority_results,
    summarize_forward_parent_public_timeout_scope_results,
    summarize_asset_resolution_pair_results,
    summarize_asset_resolution_triplet_results,
    summarize_cross_run_reset_results,
    summarize_prefetch_planning_results,
    summarize_asset_resolution_catalog,
    summarize_asset_resolution_results,
    summarize_simulator_cross_track_join_schema,
    summarize_simulator_evidence_dimension_manifest,
    summarize_simulator_global_evidence_registry,
    summarize_simulator_result_algebra_spec,
    summarize_simulator_value_witness_ledger,
    validate_join_schema_scenario,
    summarize_direct_file_id_scope_results,
    summarize_future_local_identity_promotion_results,
    summarize_forward_timeout_results,
    summarize_public_timeout_scope_results,
    summarize_second_pass_gate_results,
    summarize_simulator_coverage_manifest,
    summarize_shared_outcome_scope_results,
)


def test_public_token_forward_video_same_parent_short_circuits_siblings() -> None:
    result = run_forward_timeout_simulation(
        route="public-token",
        asset_type="video",
        parents=1,
        siblings_per_parent=6,
        delay_s=0.0,
    )

    assert result.total_requests == 6
    assert result.backend_timeout_calls == 6
    assert result.short_circuited_requests == 0
    assert result.equivalent_live_timeout_s == result.timeout_budget_s * result.total_requests


def test_public_token_forward_video_unique_parents_pay_one_timeout_each() -> None:
    result = run_forward_timeout_simulation(
        route="public-token",
        asset_type="video",
        parents=6,
        siblings_per_parent=1,
        delay_s=0.0,
    )

    assert result.total_requests == 6
    assert result.backend_timeout_calls == 6
    assert result.short_circuited_requests == 0
    assert result.equivalent_live_timeout_s == result.timeout_budget_s * 6


def test_forward_materialize_same_parent_short_circuits_siblings() -> None:
    result = run_forward_timeout_simulation(
        route="forward-materialize",
        asset_type="video",
        parents=1,
        siblings_per_parent=5,
        delay_s=0.0,
    )

    assert result.total_requests == 5
    assert result.backend_timeout_calls == 1
    assert result.short_circuited_requests == 4


def test_default_matrix_includes_video_and_speech_routes() -> None:
    results = default_forward_timeout_matrix(delay_s=0.0)

    assert len(results) >= 54
    assert any(item.route == "public-token" and item.asset_type == "video" for item in results)
    assert any(item.route == "forward-materialize" and item.asset_type == "video" for item in results)
    assert any(item.route == "public-token" and item.asset_type == "speech" for item in results)
    assert any(item.age_days >= 180 for item in results)
    assert any(item.age_days < 30 for item in results)


def test_forward_timeout_budget_no_longer_changes_from_age_alone() -> None:
    recent = run_forward_timeout_simulation(
        route="public-token",
        asset_type="video",
        parents=4,
        siblings_per_parent=1,
        age_days=20,
        delay_s=0.0,
    )
    old = run_forward_timeout_simulation(
        route="public-token",
        asset_type="video",
        parents=4,
        siblings_per_parent=1,
        age_days=260,
        delay_s=0.0,
    )

    assert old.timeout_budget_s == recent.timeout_budget_s
    assert old.equivalent_live_timeout_s == recent.equivalent_live_timeout_s


def test_forward_timeout_summary_reports_age_buckets_and_worst_case() -> None:
    summary = summarize_forward_timeout_results(default_forward_timeout_matrix(delay_s=0.0))

    assert summary["total"] >= 54
    assert summary["age_bucket_counts"]["recent"] > 0
    assert summary["age_bucket_counts"]["old_forward"] > 0
    assert summary["storm_risk_count"] > 0
    assert summary["short_circuit_help_count"] > 0
    assert summary["breaker_savings_total_s"] > 0
    assert summary["worst_case"]["equivalent_live_timeout_s"] > 0
    assert summary["threshold_counts"]["over_30s"] > 0
    assert summary["trace_status_by_route"]


def test_asset_resolution_matrix_matches_expectations() -> None:
    results = run_asset_resolution_matrix()

    assert len(results) >= 458
    assert all(item.matched for item in results)


def test_asset_resolution_matrix_includes_core_failure_and_remote_recovery_paths() -> None:
    results = {item.name: item for item in run_asset_resolution_matrix()}

    assert results["top_level_image_placeholder_zero_byte"].actual_resolver is None
    assert results["top_level_image_placeholder_zero_byte"].actual_path_kind == "missing"

    assert results["top_level_speech_public_token_remote"].actual_resolver == "napcat_public_token_get_record_remote_url"
    assert results["top_level_speech_public_token_remote"].actual_path_kind == "remote"
    assert results["top_level_sticker_relative_remote_gif"].actual_resolver == "sticker_remote_download"
    assert results["top_level_sticker_relative_remote_gif"].actual_path_kind == "remote"

    assert results["forward_old_video_public_token_timeout"].actual_resolver == "qq_expired_after_napcat"
    assert results["forward_old_video_public_token_timeout"].actual_path_kind == "missing"

    assert results["forward_old_video_materialize_timeout"].actual_resolver == "qq_expired_after_napcat"
    assert results["forward_old_video_materialize_timeout"].actual_path_kind == "missing"
    assert results["forward_old_video_materialize_timeout"].cost_matched is True

    assert results["forward_video_relative_remote_url"].actual_resolver == "napcat_forward_remote_url"
    assert results["forward_video_relative_remote_url"].actual_path_kind == "remote"

    assert results["forward_old_video_route_unavailable"].actual_resolver == "qq_expired_after_napcat"
    assert results["forward_old_video_route_unavailable"].actual_path_kind == "missing"
    assert results["forward_video_missing_parent_element_id"].actual_resolver is None
    assert results["forward_video_missing_parent_element_id"].actual_path_kind == "missing"
    assert results["forward_video_stale_path_live_remote_url"].actual_resolver == "napcat_forward_remote_url"
    assert results["forward_video_stale_path_live_remote_url"].actual_path_kind == "remote"
    assert results["nested_forward_video_missing_peer_uid_live_http"].actual_resolver == "napcat_forward_remote_url"
    assert results["nested_forward_video_missing_peer_uid_live_http"].actual_path_kind == "remote"
    assert results["forward_video_very_old_empty_terminal"].actual_resolver == "qq_expired_after_napcat"
    assert results["forward_video_very_old_empty_terminal"].actual_path_kind == "missing"
    assert results["forward_video_very_old_materialize_error"].actual_resolver == "qq_expired_after_napcat"
    assert results["forward_video_very_old_materialize_error"].actual_path_kind == "missing"
    assert results["forward_video_very_old_public_not_found"].actual_resolver == "qq_expired_after_napcat"
    assert results["forward_video_very_old_public_not_found"].actual_path_kind == "missing"
    assert results["forward_video_very_old_direct_not_found"].actual_resolver == "qq_expired_after_napcat"
    assert results["forward_video_very_old_direct_not_found"].actual_path_kind == "missing"
    assert results["nested_forward_speech_very_old_timeout"].actual_resolver == "qq_expired_after_napcat"
    assert results["nested_forward_speech_very_old_timeout"].actual_path_kind == "missing"
    assert results["nested_forward_speech_very_old_materialize_error"].actual_resolver == "qq_expired_after_napcat"
    assert results["nested_forward_speech_very_old_materialize_error"].actual_path_kind == "missing"
    assert results["nested_forward_file_recent_relative_http_remote_recovery"].actual_resolver == "napcat_forward_remote_url"
    assert results["nested_forward_file_recent_relative_http_remote_recovery"].actual_path_kind == "remote"
    assert results["nested_forward_sticker_relative_http_remote_recovery"].actual_resolver == "sticker_remote_download"
    assert results["nested_forward_sticker_relative_http_remote_recovery"].actual_path_kind == "remote"
    assert results["forward_sticker_missing_peer_uid_live_http"].actual_resolver == "sticker_remote_download"
    assert results["forward_sticker_missing_peer_uid_live_http"].actual_path_kind == "remote"
    assert results["exhaustive_forward_image_recent_none_dead_remote_metadata_timeout_materialize_empty"].actual_resolver == "qq_expired_after_napcat"
    assert results["exhaustive_forward_image_recent_none_dead_remote_metadata_timeout_materialize_empty"].actual_path_kind == "missing"
    assert results["exhaustive_nested_forward_image_old_stale_missing_dead_remote_metadata_timeout_materialize_empty"].actual_resolver == "qq_expired_after_napcat"
    assert results["exhaustive_nested_forward_image_old_stale_missing_dead_remote_metadata_timeout_materialize_empty"].actual_path_kind == "missing"
    assert results["exhaustive_forward_image_recent_no_remote_metadata_timeout_terminal"].actual_resolver == "qq_expired_after_napcat"
    assert results["exhaustive_forward_image_recent_no_remote_metadata_timeout_terminal"].actual_path_kind == "missing"
    assert results["exhaustive_nested_forward_image_relative_http_unavailable_remote_wins"].actual_resolver == "napcat_forward_remote_url"
    assert results["exhaustive_nested_forward_image_relative_http_unavailable_remote_wins"].actual_path_kind == "remote"
    assert results["top_level_speech_stale_public_not_found_fallback_terminal_recent"].actual_resolver == "qq_expired_after_napcat"
    assert results["top_level_speech_stale_public_not_found_fallback_terminal_recent"].actual_path_kind == "missing"
    assert results["top_level_speech_stale_blank_public_payload_terminal_recent"].actual_resolver == "qq_expired_after_napcat"
    assert results["top_level_speech_stale_blank_public_payload_terminal_recent"].actual_path_kind == "missing"
    assert results["current_full_dev_top_level_speech_not_found_fallback_background_mid_age"].actual_resolver == "qq_expired_after_napcat"
    assert results["current_full_dev_top_level_speech_not_found_fallback_background_mid_age"].actual_path_kind == "missing"


def test_asset_resolution_case_reports_known_bad_video_token() -> None:
    scenario = {
        item.name: item
        for item in default_asset_resolution_scenarios()
    }["forward_video_known_bad_public_token"]

    result = run_asset_resolution_scenario(scenario)

    assert result.actual_resolver == "napcat_video_url_unavailable"
    assert result.actual_path_kind == "missing"


def test_asset_resolution_matrix_can_filter_by_suite() -> None:
    route_health = run_asset_resolution_matrix(suite="route_health")
    suites = {item.suite for item in route_health}

    assert route_health
    assert suites == {"route_health"}


def test_asset_resolution_scenario_catalog_is_systematic() -> None:
    scenarios = all_asset_resolution_scenarios()
    names = {item.name for item in scenarios}

    assert len(scenarios) >= 392
    assert len(names) == len(scenarios)
    assert any(item.topology == "nested_forward" for item in scenarios)
    assert any(item.suite == "family_diff_matrix" for item in scenarios)
    assert any(item.suite == "exhaustive_old_forward_terminal" for item in scenarios)
    assert any(item.suite == "exhaustive_sticker_forward_parent" for item in scenarios)
    assert any(item.suite == "exhaustive_local_path_states" for item in scenarios)
    assert any(item.suite == "exhaustive_old_forward_direct_file_id" for item in scenarios)
    assert any(item.suite == "public_token_shape_drift" for item in scenarios)
    assert any(item.suite == "exhaustive_old_forward_payload_file_id" for item in scenarios)
    assert any(item.suite == "exhaustive_old_public_zero_byte" for item in scenarios)
    assert any(item.suite == "exhaustive_forward_image_terminal" for item in scenarios)
    assert any(item.suite == "prefetch_seeded_image_interactions" for item in scenarios)
    assert any(item.suite == "prefetch_seeded_forward_media_interactions" for item in scenarios)
    assert any(item.suite == "top_level_speech_terminal_evidence" for item in scenarios)
    assert any(item.suite == "terminal_evidence_age_invariance" for item in scenarios)
    assert any(item.suite == "request_state_payload_state_terminal_equivalence" for item in scenarios)
    assert any("public_not_found" in item.name for item in scenarios)
    assert any("direct_not_found" in item.name for item in scenarios)
    assert any(item.asset_type == "sticker" and item.topology == "nested_forward" for item in scenarios)


def test_asset_resolution_summary_reports_no_mismatches_and_catalog_shape() -> None:
    results = run_asset_resolution_matrix()
    summary = summarize_asset_resolution_results(results)

    assert summary["total"] >= 458
    assert summary["mismatched"] == 0
    assert summary["cost_overruns"] == 0
    assert summary["suite_counts"]["route_health"] > 0
    assert summary["suite_counts"]["exhaustive_forward_image_terminal"] == 60
    assert summary["asset_type_counts"]["video"] > 0
    assert summary["topology_counts"]["nested_forward"] > 0
    assert summary["age_bucket_counts"]["old_forward"] > 0
    assert summary["call_cost_totals"]["asset_type:video"]["cases"] > 0
    assert summary["terminal_missing_quality"]["classified_missing_count"] > 0
    assert summary["cost_vs_result_cross_tab"]["matched_and_cheap"] == summary["total"]
    assert "<none>" in summary["resolver_counts"] or any(
        key.startswith("napcat_") or key == "qq_expired_after_napcat"
        for key in summary["resolver_counts"]
    )


def test_asset_resolution_catalog_reports_state_coverage() -> None:
    summary = summarize_asset_resolution_catalog()

    assert summary["total"] >= 458
    assert summary["suite_counts"]["public_token_shape_drift"] == 36
    assert summary["suite_counts"]["exhaustive_forward_image_terminal"] == 60
    assert summary["suite_counts"]["request_state_payload_state_terminal_equivalence"] == 6
    assert summary["suite_counts"]["prefetch_seeded_image_interactions"] == 8
    assert summary["suite_counts"]["prefetch_seeded_forward_media_interactions"] == 12
    assert summary["suite_counts"]["top_level_speech_terminal_evidence"] == 4
    assert summary["suite_counts"]["exact_friend_speech_current_reduction"] == 1
    assert summary["state_field_counts"]["hint_remote_state"]["live_http"] > 0
    assert summary["state_field_counts"]["hint_remote_state"]["relative_gchatpic"] > 0
    assert summary["state_field_counts"]["hint_remote_state"]["relative_download_dead"] > 0
    assert summary["state_field_counts"]["hint_file_id_state"]["public_token"] > 0
    assert summary["state_field_counts"]["public_fallback_result_state"]["valid_remote_only"] > 0
    assert summary["state_field_counts"]["forward_parent_state"]["missing_peer_uid"] > 0
    assert summary["state_field_counts"]["direct_file_result_state"]["not_found"] > 0
    assert summary["asset_role_counts"]["<none>"] == summary["total"]
    assert summary["terminality_flags"]["expected_terminal_missing"] > 0
    assert summary["route_signal_flags"]["has_forward_parent"] > 0
    assert summary["shared_cache_risk_flags"]["old_forward_forward_video"] > 0
    assert summary["payload_shape_counts"]["forward_payload_state"]["public_token"] > 0


def test_terminal_evidence_age_invariance_suite_matches_recent_and_old_results() -> None:
    results = run_asset_resolution_matrix(suite="terminal_evidence_age_invariance")
    by_name = {item.name: item for item in results}

    assert len(results) == 24
    assert all(item.matched for item in results)

    for stem in (
        "top_level_image_public_token_dead_remote",
        "forward_image_dead_remote_public_timeout",
        "forward_image_no_payload_terminal",
        "forward_video_blank_public_payload",
        "forward_video_direct_not_found",
        "forward_speech_blank_public_payload",
    ):
        recent = by_name[f"{stem}_recent"]
        old = by_name[f"{stem}_old"]
        assert recent.actual_resolver == old.actual_resolver
        assert recent.actual_path_kind == old.actual_path_kind


def test_request_state_payload_state_terminal_equivalence_suite_matches_recent_and_old_results() -> None:
    results = run_asset_resolution_matrix(suite="request_state_payload_state_terminal_equivalence")
    by_name = {item.name: item for item in results}

    assert len(results) == 6
    assert all(item.matched for item in results)

    for stem in (
        "top_level_image_weak_gchatpic_context_no_path",
        "top_level_image_weak_gchatpic_context_stale_local",
        "top_level_image_local_download_dead",
    ):
        recent = by_name[f"{stem}_recent"]
        old = by_name[f"{stem}_old"]
        assert recent.actual_resolver == old.actual_resolver
        assert recent.actual_path_kind == old.actual_path_kind

    assert by_name["top_level_image_weak_gchatpic_context_no_path_recent"].actual_resolver == "qq_not_downloaded_local_placeholder"
    assert by_name["top_level_image_weak_gchatpic_context_stale_local_recent"].actual_resolver == "qq_not_downloaded_local_placeholder"
    assert by_name["top_level_image_local_download_dead_recent"].actual_resolver == "qq_expired_after_napcat"
    assert by_name["top_level_image_weak_gchatpic_context_no_path_recent"].actual_path_kind == "missing"
    assert by_name["top_level_image_local_download_dead_recent"].actual_path_kind == "missing"


def test_asset_resolution_prefetch_seeded_image_interaction_suite_matches_expectations() -> None:
    results = run_asset_resolution_matrix(suite="prefetch_seeded_image_interactions")
    by_name = {item.name: item for item in results}

    assert len(results) == 8
    assert all(item.matched for item in results)

    assert by_name["top_level_image_prefetch_payload_only_gchatpic_empty_local"].actual_resolver == "qq_not_downloaded_local_placeholder"
    assert by_name["top_level_image_prefetch_payload_only_gchatpic_stale_local"].actual_resolver == "qq_not_downloaded_local_placeholder"
    assert by_name["top_level_image_prefetch_remote_failed_download_dead_empty_local"].actual_resolver == "qq_not_downloaded_local_placeholder"
    assert by_name["top_level_image_prefetch_remote_failed_download_dead_stale_local"].actual_resolver == "qq_not_downloaded_local_placeholder"
    assert by_name["forward_image_prefetch_payload_only_dead_remote_terminal"].actual_resolver == "qq_expired_after_napcat"
    assert by_name["forward_image_prefetch_payload_only_no_remote_terminal"].actual_resolver == "qq_expired_after_napcat"
    assert by_name["nested_forward_image_prefetch_payload_only_dead_remote_terminal"].actual_resolver == "qq_expired_after_napcat"
    assert by_name["nested_forward_image_prefetch_payload_only_live_remote_wins"].actual_resolver == "napcat_forward_remote_url"
    assert by_name["nested_forward_image_prefetch_payload_only_live_remote_wins"].actual_path_kind == "remote"


def test_asset_resolution_prefetch_seeded_forward_media_interaction_suite_matches_expectations() -> None:
    results = run_asset_resolution_matrix(suite="prefetch_seeded_forward_media_interactions")
    by_name = {item.name: item for item in results}

    assert len(results) == 12
    assert all(item.matched for item in results)

    assert by_name["forward_video_prefetch_public_payload_only_live_remote_wins"].actual_resolver == "napcat_forward_remote_url"
    assert by_name["forward_video_prefetch_public_payload_only_live_remote_wins"].actual_path_kind == "remote"
    assert by_name["forward_file_prefetch_public_remote_failed_terminal"].actual_resolver == "qq_expired_after_napcat"
    assert by_name["forward_file_prefetch_public_remote_failed_terminal"].actual_path_kind == "missing"
    assert by_name["forward_speech_prefetch_public_remote_failed_terminal"].actual_resolver == "qq_expired_after_napcat"
    assert by_name["forward_speech_prefetch_public_remote_failed_terminal"].actual_path_kind == "missing"
    assert by_name["nested_forward_speech_prefetch_payload_only_live_forward_remote_wins"].actual_resolver == "napcat_forward_remote_url"
    assert by_name["nested_forward_speech_prefetch_payload_only_live_forward_remote_wins"].actual_path_kind == "remote"
    assert by_name["nested_forward_video_prefetch_payload_only_no_remote_nonterminal"].actual_resolver == "qq_expired_after_napcat"
    assert by_name["nested_forward_video_prefetch_payload_only_no_remote_nonterminal"].actual_path_kind == "missing"


def test_second_pass_gate_matrix_matches_expectations() -> None:
    results = run_second_pass_gate_matrix()
    summary = summarize_second_pass_gate_results(results)
    by_name = {item.name: item for item in results}

    assert len(results) == len(default_second_pass_gate_cases())
    assert summary["mismatched"] == 0
    assert summary["topology_counts"]["top_level"] > 0
    assert summary["topology_counts"]["forward"] > 0
    assert summary["asset_type_counts"]["image"] > 0
    assert summary["public_prefetch_state_counts"]["payload_only"] > 0
    assert summary["retry_counts"]["retry"] > 0
    assert summary["retry_counts"]["skip"] > 0

    assert by_name["top_level_image_no_prefetch_direct_public_token"].actual_should_retry is False
    assert by_name["top_level_image_pending_future_payload_only"].actual_should_retry is True
    assert by_name["top_level_image_done_not_finalized_payload_only"].actual_should_retry is True
    assert by_name["top_level_image_cached_payload_only"].actual_should_retry is False
    assert by_name["top_level_image_cached_remote_attempted_failed"].actual_should_retry is False
    assert by_name["top_level_image_cached_terminal_result"].actual_should_retry is False
    assert by_name["top_level_image_request_state_terminal_context_placeholder"].actual_should_retry is False
    assert by_name["top_level_image_request_state_public_token"].actual_should_retry is False
    assert by_name["top_level_video_request_state_terminal_blank_public_payload"].actual_should_retry is False


def test_prefetch_planning_matrix_reports_large_window_pressure_shapes() -> None:
    results = run_prefetch_planning_matrix()
    summary = summarize_prefetch_planning_results(results)

    assert len(results) == 20
    assert summary["profile_counts"]["recent_image_heavy"] == 5
    assert summary["profile_counts"]["old_forward_video_heavy"] == 5
    assert summary["max_batch_size"] == 200
    assert summary["large_window_case_count"] == 12
    assert summary["large_window_batch_size_min"] == 50
    assert summary["large_window_batch_size_max"] == 50
    assert summary["max_remote_workers"] >= 4
    assert summary["max_public_token_workers"] >= 2
    assert summary["old_forward_total"] > 0
    assert summary["duplicate_shared_key_total"] > 0
    assert summary["worst_case"]["request_count"] >= 16384


def test_forward_candidate_priority_matrix_matches_expectations() -> None:
    results = run_forward_candidate_priority_matrix()
    summary = summarize_forward_candidate_priority_results(results)

    assert len(results) == 42
    assert summary["mismatched"] == 0
    assert summary["profile_counts"]["recoverability_tiebreak"] == 24
    assert summary["profile_counts"]["signal_priority"] == 18
    assert summary["asset_type_counts"]["video"] > 0
    assert summary["resolver_counts"]["napcat_forward_hydrated"] > 0
    assert summary["resolver_counts"]["napcat_forward_remote_url"] > 0
    assert summary["resolver_counts"]["napcat_public_token_get_file"] > 0
    assert summary["path_kind_counts"]["public"] > 0
    assert summary["path_kind_counts"]["remote"] > 0


def test_forward_candidate_priority_case_file_biz_id_beats_filename_local_decoy() -> None:
    cases = {item.name: item for item in default_forward_candidate_priority_cases()}

    result = run_forward_candidate_priority_case(cases["video_signal_file_biz_id_over_filename"])

    assert result.matched is True
    assert result.actual_winner == "primary"
    assert result.expected_path_kind == "public"
    assert result.resolver == "napcat_public_token_get_file"
    assert result.path_kind == "public"


def test_shared_outcome_scope_matrix_matches_expectations() -> None:
    results = run_shared_outcome_scope_matrix()
    summary = summarize_shared_outcome_scope_results(results)

    assert len(results) == len(default_shared_outcome_scope_cases())
    assert summary["mismatched"] == 0
    assert summary["asset_type_counts"]["video"] > 0
    assert summary["topology_counts"]["forward"] > 0
    assert summary["identity_mode_counts"]["file_name_only"] > 0


def test_public_timeout_scope_matrix_matches_expectations() -> None:
    results = run_public_timeout_scope_matrix()
    summary = summarize_public_timeout_scope_results(results)
    by_name = {item.name: item for item in results}

    assert len(results) == len(default_public_timeout_scope_cases())
    assert summary["mismatched"] == 0
    assert summary["asset_type_counts"]["image"] > 0
    assert summary["asset_type_counts"]["video"] > 0
    assert summary["relationship_counts"]["same_parent_new_token"] > 0
    assert by_name["image_same_parent_same_token_same_request"].actual_same_key is True
    assert by_name["image_same_parent_new_token"].actual_same_key is True
    assert by_name["image_same_parent_same_token_new_file"].actual_same_key is True
    assert by_name["image_different_parent_same_token"].actual_same_key is False
    assert by_name["image_non_forward_ignored"].actual_same_key is False


def test_forward_parent_public_timeout_scope_matrix_matches_candidate_policy() -> None:
    results = run_forward_parent_public_timeout_scope_matrix()
    summary = summarize_forward_parent_public_timeout_scope_results(results)
    by_name = {item.name: item for item in results}

    assert len(results) == len(default_forward_parent_public_timeout_scope_cases())
    assert summary["mismatched"] == 0
    assert summary["asset_type_counts"]["video"] > 0
    assert summary["asset_type_counts"]["file"] > 0
    assert by_name["video_aged_same_parent_new_token"].actual_same_key is True
    assert by_name["video_aged_same_parent_same_token_new_file"].actual_same_key is True
    assert by_name["video_aged_different_parent_same_token"].actual_same_key is False
    assert by_name["video_recent_same_parent_new_token"].actual_same_key is False
    assert by_name["file_aged_same_parent_new_token"].actual_same_key is True
    assert by_name["file_recent_same_parent_new_token"].actual_same_key is False


def test_asset_resolution_pair_matrix_matches_expectations() -> None:
    results = run_asset_resolution_pair_matrix()
    summary = summarize_asset_resolution_pair_results(results)
    by_name = {item.name: item for item in results}

    assert len(results) == len(default_asset_resolution_pair_cases())
    assert summary["mismatched"] == 0
    assert summary["resolver_counts"]["napcat_public_token_get_image_remote_url"] > 0
    assert summary["resolver_counts"]["napcat_segment_file_id_get_file_remote_url"] > 0
    assert summary["path_kind_counts"]["remote"] == len(results)
    assert by_name["top_level_image_placeholder_then_forward_remote"].actual_second_resolver == "napcat_forward_remote_url"
    assert by_name["forward_image_unresolved_then_nested_forward_remote"].actual_second_resolver == "napcat_forward_remote_url"
    assert by_name["nested_forward_terminal_then_top_level_public_remote"].actual_second_resolver == "napcat_public_token_get_image_remote_url"


def test_asset_resolution_pair_matrix_covers_cross_topology_distribution_cases() -> None:
    results = {item.name: item for item in run_asset_resolution_pair_matrix()}

    assert results["top_level_image_placeholder_then_forward_remote"].matched is True
    assert results["top_level_image_placeholder_then_forward_remote"].actual_second_resolver == "napcat_forward_remote_url"
    assert results["top_level_image_placeholder_then_forward_remote"].actual_second_path_kind == "remote"

    assert results["forward_image_unresolved_then_nested_forward_remote"].matched is True
    assert results["forward_image_unresolved_then_nested_forward_remote"].actual_second_resolver == "napcat_forward_remote_url"
    assert results["forward_image_unresolved_then_nested_forward_remote"].actual_second_path_kind == "remote"

    assert results["top_level_video_old_timeout_then_forward_direct_remote"].matched is True
    assert results["top_level_video_old_timeout_then_forward_direct_remote"].actual_second_resolver == "napcat_segment_file_id_get_file_remote_url"
    assert results["top_level_video_old_timeout_then_forward_direct_remote"].actual_second_path_kind == "remote"

    assert results["top_level_file_old_timeout_then_nested_forward_direct_remote"].matched is True
    assert results["top_level_file_old_timeout_then_nested_forward_direct_remote"].actual_second_resolver == "napcat_segment_file_id_get_file_remote_url"
    assert results["top_level_file_old_timeout_then_nested_forward_direct_remote"].actual_second_path_kind == "remote"


def test_asset_resolution_triplet_matrix_preserves_weak_identity_semantics_after_strong_recovery() -> None:
    results = run_asset_resolution_triplet_matrix()
    summary = summarize_asset_resolution_triplet_results(results)
    by_name = {item.name: item for item in results}

    assert len(results) == len(default_asset_resolution_triplet_cases())
    assert summary["mismatched"] == 0
    assert summary["third_path_kind_counts"]["missing"] == len(results) - 1
    assert summary["third_path_kind_counts"]["local"] == 1
    assert summary["third_resolver_counts"]["<none>"] >= 1

    assert by_name["top_level_image_weak_then_strong_then_weak_repeat"].actual_second_resolver == "napcat_public_token_get_image_remote_url"
    assert by_name["top_level_image_weak_then_strong_then_weak_repeat"].actual_third_resolver == "qq_not_downloaded_local_placeholder"
    assert by_name["forward_image_weak_then_top_level_strong_then_forward_repeat"].actual_second_resolver == "napcat_public_token_get_image_remote_url"
    assert by_name["forward_image_weak_then_top_level_strong_then_forward_repeat"].actual_third_resolver == "qq_expired_after_napcat"
    assert by_name["top_level_video_weak_then_forward_strong_then_top_level_repeat"].actual_third_resolver is None
    assert by_name["top_level_file_weak_then_nested_forward_strong_then_top_level_repeat"].actual_third_resolver is None
    assert by_name["join_nonmutation_top_level_timeout_then_forward_direct_then_repeat"].actual_second_resolver == "napcat_segment_file_id_get_file_remote_url"
    assert by_name["join_nonmutation_top_level_timeout_then_forward_direct_then_repeat"].actual_third_resolver == "napcat_segment_file_id_get_file_remote_url"


def test_future_local_identity_promotion_matrix_matches_expectations() -> None:
    results = run_future_local_identity_promotion_matrix()
    summary = summarize_future_local_identity_promotion_results(results)

    assert len(results) == len(default_future_local_identity_promotion_cases())
    assert all(item.matched for item in results)
    assert summary["mismatched"] == 0
    assert summary["asset_type_counts"]["image"] > 0
    assert summary["asset_type_counts"]["video"] > 0
    assert summary["first_behavior_counts"]["future_local_promotion"] > 0
    assert summary["third_behavior_counts"]["recent_reuse"] > 0
    assert any(
        sequence.startswith("top_level->nested_forward")
        or sequence.startswith("forward->nested_forward")
        for sequence in summary["topology_sequence_counts"]
    )


def test_simulator_coverage_manifest_reports_cross_tabs_and_no_family_topology_holes() -> None:
    summary = summarize_simulator_coverage_manifest()

    assert summary["case_family_counts"]["single_scenarios"] >= 688
    assert summary["case_family_counts"]["future_local_identity_promotion"] == len(
        default_future_local_identity_promotion_cases()
    )
    assert summary["asset_topology_matrix"]["image"]["top_level"] > 0
    assert summary["asset_topology_matrix"]["image"]["forward"] > 0
    assert summary["asset_topology_matrix"]["image"]["nested_forward"] > 0
    assert summary["asset_topology_matrix"]["video"]["top_level"] > 0
    assert summary["asset_topology_matrix"]["sticker"]["nested_forward"] > 0
    assert summary["optimization_seam_counts"]["terminal_classifier"] > 0
    assert summary["optimization_seam_counts"]["prefetch_seeded_routes"] > 0
    assert summary["optimization_seam_counts"]["partial_parent_handle_sufficient"] > 0
    assert summary["optimization_seam_counts"]["second_pass_gate"] > 0
    assert summary["optimization_seam_counts"]["future_local_identity_promotion"] > 0
    assert summary["sequence_family_counts"]["promotion:image"] > 0
    assert summary["prefetch_seed_shape_counts"]["<none>"] > 0
    assert summary["coverage_gaps"]["single_scenario_family_topology_missing"] == []
    assert summary["coverage_gaps"]["promotion_image_topology_missing"] == []


def test_simulator_evidence_dimension_manifest_reports_domains_and_gaps() -> None:
    summary = summarize_simulator_evidence_dimension_manifest()

    assert summary["dimension_count"] >= 16
    assert "asset_type" in summary["domains"]
    assert "speech" in summary["domains"]["asset_type"]
    assert "top_level_speech_terminal_evidence" not in summary["domains"]
    assert "asset_type" in summary["fully_covered_dimensions"]
    assert "topology" in summary["fully_covered_dimensions"]
    assert "prefetch_public_state" in summary["fully_covered_dimensions"]
    assert "forward_metadata_state" in summary["fully_covered_dimensions"]
    assert "prefetch_request_context_payload_state" in summary["fully_covered_dimensions"]
    assert "public_fallback_result_state" in summary["fully_covered_dimensions"]
    assert summary["uncovered_values"]["public_fallback_result_state"] == []


def test_simulator_global_evidence_registry_reports_owner_and_source_classes() -> None:
    summary = summarize_simulator_global_evidence_registry()
    dimensions = summary["dimensions"]

    assert summary["dimension_count"] >= 24
    assert dimensions["asset_type"]["owner_track"] == "coverage_reachability_surface"
    assert "exporter_logic" in dimensions["asset_type"]["source_classes"]
    assert dimensions["topology"]["owner_track"] == "forward_recursive_surface"
    assert "napcat_plugin_contract" in dimensions["topology"]["source_classes"]
    assert dimensions["chat_provenance"]["owner_track"] == "provider_history_surface"
    assert dimensions["filesystem_family"]["owner_track"] == "filesystem_materialization_surface"
    assert dimensions["speech_requested_out_format"]["owner_track"] == "speech_output_surface"
    assert "covered_values" in dimensions["public_result_state"]
    assert "uncovered_values" in dimensions["public_fallback_result_state"]


def test_simulator_value_witness_ledger_reports_covered_and_unresolved_values() -> None:
    summary = summarize_simulator_value_witness_ledger()
    dimensions = summary["dimensions"]

    assert summary["dimension_count"] >= 24
    assert dimensions["asset_type"]["image"]["status"] == "covered"
    assert dimensions["topology"]["nested_forward"]["status"] == "covered"
    assert dimensions["forward_metadata_state"]["timeout"]["status"] == "covered"
    assert dimensions["public_fallback_result_state"]["known_bad_video"]["status"] == "covered"
    assert dimensions["context_payload_state"]["remote_url"]["status"] == "route_irrelevant"
    assert dimensions["context_payload_state"]["remote_url"]["unreachable_reason"] == "ignored_by_current_route"
    assert dimensions["speech_original_format"]["amr"]["status"] == "covered"
    assert dimensions["speech_original_format"]["amr"]["unreachable_reason"] is None
    assert dimensions["chat_provenance"]["group"]["status"] == "covered"
    assert dimensions["filesystem_family"]["ntqq"]["status"] == "covered"
    assert dimensions["speech_requested_out_format"]["default"]["status"] == "covered"
    assert dimensions["speech_requested_out_format"]["mp3"]["status"] == "covered"
    assert dimensions["speech_requested_out_format"]["mp3"]["unreachable_reason"] is None
    assert "public_fallback_result_state" not in summary["unresolved_dimensions"]
    assert summary["status_counts"]["covered"] > 0
    assert summary["status_counts"]["deferred_needs_carrier"] > 0
    assert summary["status_counts"]["route_irrelevant"] > 0
    assert summary["status_counts"]["reserved_future_placeholder"] > 0
    assert "speech_original_format" in summary["adjudicated_noncovered_dimensions"]


def test_simulator_cross_track_join_schema_reports_required_groups() -> None:
    summary = summarize_simulator_cross_track_join_schema()
    groups = summary["join_groups"]

    assert summary["join_group_count"] >= 6
    assert "provider_message_provenance" in groups
    assert "forward_handle" in groups
    assert "request_key" in groups
    assert "asset_identity_key" in groups
    assert "bundle_identity" in groups
    assert "materialization_outcome" in groups
    assert "forward_handle" in groups["provider_message_provenance"]["joins_to"]
    assert "materialization_outcome" in groups["request_key"]["joins_to"]


def test_simulator_result_algebra_spec_reports_bundle_and_manifest_fields() -> None:
    summary = summarize_simulator_result_algebra_spec()
    fields = summary["fields"]

    assert summary["field_count"] >= 8
    assert "materialization_status" in fields
    assert "missing_kind" in fields
    assert "missing_bucket" in fields
    assert "bundle_behavior" in fields
    assert "requested_output_format" in fields
    assert "materialized_output_format" in fields
    assert "converted" in fields["format_relation"]["domain"]


def test_asset_resolution_result_carries_runtime_algebra_projection() -> None:
    result = {
        item.name: item for item in run_asset_resolution_matrix()
    }["top_level_speech_stale_public_not_found_fallback_terminal_recent"]

    assert result.algebra.resolution_result == "missing"
    assert result.algebra.terminality_class == "background_terminal"
    assert result.algebra.materialization_status == "missing"
    assert result.algebra.missing_kind == "qq_expired_after_napcat"
    assert result.algebra.missing_bucket == "background"


def test_future_local_promotion_result_carries_runtime_algebra_projection() -> None:
    result = {
        item.name: item for item in run_future_local_identity_promotion_matrix()
    }["image_top_level_weak_then_nested_forward_strong_then_repeat"]

    assert result.first_algebra["bundle_behavior"] == "future_local_promotion"
    assert result.first_algebra["materialization_status"] == "copied"
    assert result.second_algebra["bundle_behavior"] == "recent_reuse"
    assert result.second_algebra["materialization_status"] == "reused"
    assert result.third_algebra["bundle_behavior"] == "recent_reuse"


def test_pair_result_carries_runtime_algebra_projection() -> None:
    result = {
        item.name: item for item in run_asset_resolution_pair_matrix()
    }["top_level_image_placeholder_then_forward_remote"]

    assert result.first_algebra["resolution_result"] == "missing"
    assert result.first_algebra["terminality_class"] == "unresolved"
    assert result.first_algebra["bundle_behavior"] == "unresolved"
    assert result.second_algebra["resolution_result"] == "resolved"
    assert result.second_algebra["terminality_class"] == "recovered"
    assert result.second_algebra["materialization_status"] == "copied"
    assert result.second_algebra["bundle_behavior"] == "immediate_copy"


def test_triplet_result_carries_runtime_algebra_projection() -> None:
    result = {
        item.name: item for item in run_asset_resolution_triplet_matrix()
    }["top_level_image_weak_then_strong_then_weak_repeat"]

    assert result.first_algebra["missing_kind"] == "qq_not_downloaded_local_placeholder"
    assert result.first_algebra["missing_bucket"] == "background"
    assert result.second_algebra["resolution_result"] == "resolved"
    assert result.second_algebra["terminality_class"] == "recovered"
    assert result.third_algebra["missing_kind"] == "qq_not_downloaded_local_placeholder"


def test_cross_run_result_carries_runtime_algebra_projection() -> None:
    result = {
        item.name: item for item in run_cross_run_reset_matrix()
    }["cross_run_reset_top_level_image_placeholder_then_forward_remote"]

    assert result.first_algebra["resolution_result"] == "missing"
    assert result.second_algebra["resolution_result"] == "resolved"
    assert result.second_algebra["bundle_behavior"] == "immediate_copy"


def test_runtime_algebra_derives_missing_bucket_across_asset_families() -> None:
    results = {
        item.name: item for item in run_asset_resolution_matrix()
    }

    image = results["top_level_image_weak_gchatpic_context_no_path_recent"]
    video = results["forward_old_video_public_token_timeout"]
    speech = results["top_level_speech_stale_public_not_found_fallback_terminal_recent"]
    file_actionable = results["public_token_fallback_top_level_file_known_bad_file"]

    assert image.algebra.missing_kind == "qq_not_downloaded_local_placeholder"
    assert image.algebra.missing_bucket == "background"
    assert video.algebra.missing_kind == "qq_expired_after_napcat"
    assert video.algebra.missing_bucket == "background"
    assert speech.algebra.missing_kind == "qq_expired_after_napcat"
    assert speech.algebra.missing_bucket == "background"
    assert file_actionable.algebra.missing_kind == "napcat_file_url_unavailable"
    assert file_actionable.algebra.missing_bucket == "actionable"


def test_cross_run_reset_matrix_matches_expectations() -> None:
    results = run_cross_run_reset_matrix()
    summary = summarize_cross_run_reset_results(results)

    assert len(results) == len(default_cross_run_reset_cases())
    assert summary["mismatched"] == 0
    assert summary["resolver_counts"]["napcat_public_token_get_image_remote_url"] > 0
    assert summary["resolver_counts"]["napcat_segment_file_id_get_file_remote_url"] > 0
    assert summary["resolver_counts"]["napcat_public_token_get_record_remote_url"] > 0
    assert summary["path_kind_counts"]["remote"] == len(results)


def test_direct_file_id_scope_matrix_matches_expectations() -> None:
    results = run_direct_file_id_scope_matrix()
    summary = summarize_direct_file_id_scope_results(results)

    assert len(results) == len(default_direct_file_id_scope_cases())
    assert summary["mismatched"] == 0
    assert summary["asset_type_counts"]["video"] > 0
    assert summary["relationship_counts"]["same_parent_different_file_id"] > 0


def test_asset_resolution_exhaustive_old_forward_terminal_suite_matches_expectations() -> None:
    results = run_asset_resolution_matrix(suite="exhaustive_old_forward_terminal")

    assert len(results) == 144
    assert all(item.matched for item in results)
    assert all(item.actual_resolver == "qq_expired_after_napcat" for item in results)
    assert all(item.actual_path_kind == "missing" for item in results)


def test_asset_resolution_exhaustive_sticker_forward_parent_suite_matches_expectations() -> None:
    results = run_asset_resolution_matrix(suite="exhaustive_sticker_forward_parent")

    assert len(results) == 24
    assert all(item.matched for item in results)
    assert any(item.actual_resolver == "sticker_remote_download" for item in results)
    assert any(item.actual_resolver is None for item in results)


def test_asset_resolution_exhaustive_local_path_state_suite_matches_expectations() -> None:
    results = run_asset_resolution_matrix(suite="exhaustive_local_path_states")

    assert len(results) == 25
    assert all(item.matched for item in results)
    assert any(item.actual_resolver == "source_local_path" for item in results)
    assert any(item.actual_resolver == "hint_local_path" for item in results)


def test_asset_resolution_exhaustive_old_forward_direct_file_id_suite_matches_expectations() -> None:
    results = run_asset_resolution_matrix(suite="exhaustive_old_forward_direct_file_id")

    assert len(results) == 36
    assert all(item.matched for item in results)
    assert all(item.actual_resolver == "qq_expired_after_napcat" for item in results)
    assert all(item.actual_path_kind == "missing" for item in results)


def test_asset_resolution_public_token_shape_drift_suite_matches_expectations() -> None:
    results = run_asset_resolution_matrix(suite="public_token_shape_drift")

    assert len(results) == 36
    assert all(item.matched for item in results)
    assert any(item.actual_path_kind == "local" for item in results)
    assert any(item.actual_path_kind == "remote" for item in results)


def test_asset_resolution_old_forward_payload_file_id_suite_matches_expectations() -> None:
    results = run_asset_resolution_matrix(suite="exhaustive_old_forward_payload_file_id")

    assert len(results) == 36
    assert all(item.matched for item in results)
    assert all(item.actual_resolver == "qq_expired_after_napcat" for item in results)
    assert all(item.actual_path_kind == "missing" for item in results)


def test_asset_resolution_old_public_zero_byte_suite_matches_expectations() -> None:
    results = run_asset_resolution_matrix(suite="exhaustive_old_public_zero_byte")

    assert len(results) == 18
    assert all(item.matched for item in results)
    assert all(item.actual_resolver == "qq_expired_after_napcat" for item in results)
    assert all(item.actual_path_kind == "missing" for item in results)


def test_asset_resolution_exhaustive_forward_image_terminal_suite_matches_expectations() -> None:
    results = run_asset_resolution_matrix(suite="exhaustive_forward_image_terminal")
    by_name = {item.name: item for item in results}

    assert len(results) == 60
    assert all(item.matched for item in results)
    assert by_name["exhaustive_forward_image_recent_none_dead_remote_metadata_timeout_materialize_empty"].actual_resolver == "qq_expired_after_napcat"
    assert by_name["exhaustive_forward_image_recent_none_dead_remote_metadata_timeout_materialize_empty"].actual_path_kind == "missing"
    assert by_name["exhaustive_nested_forward_image_old_stale_missing_dead_remote_metadata_timeout_materialize_error"].actual_resolver == "qq_expired_after_napcat"
    assert by_name["exhaustive_nested_forward_image_old_stale_missing_dead_remote_metadata_timeout_materialize_error"].actual_path_kind == "missing"
    assert by_name["exhaustive_forward_image_recent_no_remote_metadata_timeout_terminal"].actual_resolver == "qq_expired_after_napcat"
    assert by_name["exhaustive_forward_image_recent_no_remote_metadata_timeout_terminal"].actual_path_kind == "missing"
    assert by_name["exhaustive_nested_forward_image_relative_http_unavailable_remote_wins"].actual_resolver == "napcat_forward_remote_url"
    assert by_name["exhaustive_nested_forward_image_relative_http_unavailable_remote_wins"].actual_path_kind == "remote"


def test_asset_resolution_partial_parent_handle_sufficient_suite_matches_expectations() -> None:
    results = run_asset_resolution_matrix(suite="partial_parent_handle_sufficient")
    by_name = {item.name: item for item in results}

    assert len(results) == 8
    assert all(item.matched for item in results)
    assert by_name["partial_parent_forward_image_hint_local_existing_recovers"].actual_resolver == "hint_local_path"
    assert by_name["partial_parent_nested_forward_file_hint_local_existing_recovers"].actual_resolver == "hint_local_path"
    assert by_name["partial_parent_forward_image_live_remote_survives"].actual_resolver == "napcat_forward_remote_url"
    assert by_name["partial_parent_nested_forward_video_direct_file_id_survives"].actual_resolver == "napcat_segment_file_id_get_file_remote_url"
    assert by_name["partial_parent_forward_speech_public_token_survives"].actual_resolver == "napcat_public_token_get_record_remote_url"
    assert by_name["partial_parent_forward_image_public_token_survives"].actual_resolver == "napcat_forward_remote_url"
    assert by_name["partial_parent_forward_image_no_surviving_handle_unresolved"].actual_resolver is None


def test_asset_resolution_top_level_context_payload_surface_suite_matches_expectations() -> None:
    results = run_asset_resolution_matrix(suite="top_level_context_payload_surface")
    by_name = {item.name: item for item in results}

    assert len(results) == 5
    assert all(item.matched for item in results)
    assert by_name["top_level_video_context_error_no_handle"].actual_resolver is None
    assert by_name["top_level_video_context_error_no_handle"].actual_path_kind == "missing"
    assert by_name["top_level_video_context_local_path_recovers"].actual_resolver == "napcat_context_hydrated"
    assert by_name["top_level_video_context_local_path_recovers"].actual_path_kind == "local"
    assert by_name["top_level_video_context_zero_local_no_handle"].actual_resolver is None
    assert by_name["top_level_video_context_zero_public_payload_terminal"].actual_resolver == "qq_expired_after_napcat"
    assert by_name["top_level_video_context_blank_payload_terminal"].actual_resolver == "qq_expired_after_napcat"


def test_asset_resolution_forward_payload_surface_suite_matches_expectations() -> None:
    results = run_asset_resolution_matrix(suite="forward_payload_surface")
    by_name = {item.name: item for item in results}

    assert len(results) == 8
    assert all(item.matched for item in results)
    assert by_name["forward_image_payload_error_terminal"].actual_resolver == "qq_expired_after_napcat"
    assert by_name["forward_image_payload_empty_terminal"].actual_resolver == "qq_expired_after_napcat"
    assert by_name["forward_image_payload_zero_local_terminal"].actual_resolver == "qq_expired_after_napcat"
    assert by_name["forward_image_payload_stale_local_terminal"].actual_resolver == "qq_expired_after_napcat"
    assert by_name["forward_image_payload_local_path_recovers"].actual_resolver == "napcat_forward_hydrated"
    assert by_name["forward_video_payload_blank_public_payload_remote"].actual_resolver == "napcat_forward_remote_url"
    assert by_name["forward_video_payload_zero_public_payload_remote"].actual_resolver == "napcat_forward_remote_url"
    assert by_name["forward_file_payload_file_id_only_remote"].actual_resolver == "napcat_segment_file_id_get_file_remote_url"


def test_asset_resolution_public_fallback_surface_suite_matches_expectations() -> None:
    results = run_asset_resolution_matrix(suite="public_fallback_surface")
    by_name = {item.name: item for item in results}

    assert len(results) == 9
    assert all(item.matched for item in results)
    assert by_name["public_token_fallback_top_level_video_none"].actual_resolver == "qq_expired_after_napcat"
    assert by_name["public_token_fallback_top_level_video_valid_zero_local"].actual_resolver == "qq_expired_after_napcat"
    assert by_name["public_token_fallback_top_level_video_expired_remote"].actual_resolver == "qq_expired_after_napcat"
    assert by_name["public_token_fallback_top_level_video_blank_payload"].actual_resolver == "qq_expired_after_napcat"
    assert by_name["public_token_fallback_top_level_video_timeout"].actual_resolver == "qq_expired_after_napcat"
    assert by_name["public_token_fallback_top_level_video_opaque_error"].actual_resolver == "qq_expired_after_napcat"
    assert by_name["public_token_fallback_top_level_video_known_bad_video"].actual_resolver == "napcat_video_url_unavailable"
    assert by_name["public_token_fallback_top_level_file_known_bad_file"].actual_resolver == "napcat_file_url_unavailable"
    assert by_name["public_token_fallback_top_level_speech_known_bad_record"].actual_resolver == "napcat_record_url_unavailable"


def test_asset_resolution_forward_metadata_surface_suite_matches_expectations() -> None:
    results = run_asset_resolution_matrix(suite="forward_metadata_surface")
    by_name = {item.name: item for item in results}

    assert len(results) == 9
    assert all(item.matched for item in results)
    assert by_name["forward_image_metadata_local_path_recovers"].actual_resolver == "napcat_forward_hydrated"
    assert by_name["forward_image_metadata_zero_local_terminal"].actual_resolver == "qq_expired_after_napcat"
    assert by_name["forward_image_metadata_empty_local_unresolved"].actual_resolver is None
    assert by_name["forward_image_metadata_stale_local_terminal"].actual_resolver == "qq_expired_after_napcat"
    assert by_name["forward_image_metadata_remote_url_recovers"].actual_resolver == "napcat_forward_remote_url"
    assert by_name["forward_image_metadata_blank_payload_unresolved"].actual_resolver is None
    assert by_name["forward_video_metadata_public_token_remote"].actual_resolver == "napcat_forward_remote_url"
    assert by_name["forward_video_metadata_blank_public_payload_remote"].actual_resolver == "napcat_forward_remote_url"
    assert by_name["forward_video_metadata_zero_public_payload_remote"].actual_resolver == "napcat_forward_remote_url"


def test_asset_resolution_forward_recursive_symbolic_suite_matches_expectations() -> None:
    results = run_asset_resolution_matrix(suite="forward_recursive_symbolic")
    by_name = {item.name: item for item in results}

    assert len(results) == 7
    assert all(item.matched for item in results)
    assert by_name["forward_leaf_local_recovery_exact_depth"].actual_resolver == "napcat_forward_hydrated"
    assert by_name["forward_chain_transition_handle_gain_remote"].actual_resolver == "napcat_forward_remote_url"
    assert by_name["forward_chain_transition_preview_only_terminal"].actual_resolver == "qq_expired_after_napcat"
    assert by_name["forward_chain_parent_partial_handle_survives"].actual_resolver == "napcat_public_token_get_record_remote_url"
    assert by_name["forward_chain_alias_repeat_terminal_lower_bound"].actual_resolver == "qq_expired_after_napcat"
    assert by_name["forward_chain_budget_cut_terminal_lower_bound"].actual_resolver == "qq_expired_after_napcat"
    assert by_name["forward_chain_terminal_proof_unavailable_lower_bound"].actual_resolver == "qq_expired_after_napcat"


def test_asset_resolution_join_schema_end_to_end_suite_matches_expectations() -> None:
    results = run_asset_resolution_matrix(suite="join_schema_end_to_end")
    by_name = {item.name: item for item in results}

    assert len(results) == 7
    assert all(item.matched for item in results)
    top = by_name["join_top_level_image_sourcepath_to_materialization"]
    assert top.actual_resolver == "source_local_path"
    assert top.algebra.resolution_result == "resolved"
    assert top.algebra.materialization_status == "copied"
    assert top.join_snapshot["provider_message_provenance"]["message_id_raw"]
    assert top.join_snapshot["request_key"] is not None
    assert top.join_snapshot["asset_identity_key"] is not None
    assert top.join_snapshot["bundle_identity"] is not None
    provider_top = by_name["join_top_level_provider_image_sourcepath_bundle_copy"]
    assert provider_top.actual_resolver == "source_local_path"
    assert provider_top.join_snapshot["provider_message_provenance"]["message_id_raw"]
    assert provider_top.join_snapshot["provider_message_provenance"]["history_fetch_source"] == "simulated_history"
    assert provider_top.join_snapshot["request_key"] is not None
    assert provider_top.join_snapshot["asset_identity_key"] is not None
    assert provider_top.join_snapshot["bundle_identity"] is not None
    top_file = by_name["join_top_level_file_direct_file_id_remote_to_materialization"]
    assert top_file.actual_resolver == "napcat_segment_file_id_get_file_remote_url"
    assert top_file.algebra.resolution_result == "resolved"
    assert top_file.algebra.materialization_status == "copied"
    recursive_leaf = by_name["join_recursive_forward_leaf_local_exact_depth_to_materialization"]
    assert recursive_leaf.actual_resolver == "napcat_forward_hydrated"
    assert recursive_leaf.algebra.resolution_result == "resolved"
    assert recursive_leaf.algebra.materialization_status == "copied"
    assert recursive_leaf.join_snapshot["forward_handle"] is not None
    assert recursive_leaf.join_snapshot["forward_handle"]["depth_semantics"] == "exact"
    assert recursive_leaf.join_snapshot["bundle_identity"] is not None
    recursive = by_name["join_recursive_forward_speech_public_remote_to_materialization"]
    assert recursive.actual_resolver == "napcat_public_token_get_record_remote_url"
    assert recursive.algebra.resolution_result == "resolved"
    assert recursive.algebra.requested_output_format == "mp3"
    assert recursive.algebra.original_input_format == "amr"
    assert recursive.algebra.materialized_output_format == "mp3"
    assert recursive.algebra.format_relation == "converted"
    assert recursive.algebra.output_name_relation == "rewritten_suffix_from_bytes"
    assert recursive.algebra.materialization_status == "copied"
    assert recursive.algebra.missing_bucket == "none"
    assert recursive.join_snapshot["provider_message_provenance"]["history_fetch_source"] == "simulated_forward_detail"
    assert recursive.join_snapshot["forward_handle"] is not None
    assert recursive.join_snapshot["request_key"] is not None
    assert recursive.join_snapshot["asset_identity_key"] is not None
    assert recursive.join_snapshot["bundle_identity"] is not None
    recursive_alias = by_name["join_recursive_forward_alias_repeat_timeout_to_materialization"]
    assert recursive_alias.actual_resolver == "qq_expired_after_napcat"
    assert recursive_alias.algebra.resolution_result == "missing"
    assert recursive_alias.algebra.terminality_class == "background_terminal"
    assert recursive_alias.algebra.materialization_status == "missing"
    assert recursive_alias.algebra.missing_bucket == "background"
    recursive_budget = by_name["join_recursive_forward_budget_cut_remote_survives_to_materialization"]
    assert recursive_budget.actual_resolver == "napcat_forward_remote_url"
    assert recursive_budget.algebra.resolution_result == "resolved"
    assert recursive_budget.algebra.materialization_status == "copied"
    assert recursive_budget.algebra.missing_bucket == "none"


def test_exact_friend_speech_current_reduction_suite_matches_expectations() -> None:
    results = run_asset_resolution_matrix(suite="exact_friend_speech_current_reduction")

    assert len(results) == 1
    assert all(item.matched for item in results)
    assert results[0].actual_resolver == "qq_expired_after_napcat"
    assert results[0].actual_path_kind == "missing"


def test_exact_friend_speech_dual_family_keeps_same_evidence_shape_but_distinct_terminal_semantics() -> None:
    current_case = exact_friend_speech_current_reduction_scenarios()[0]
    historical_case = historical_exact_friend_speech_reference_scenarios()[0]

    for field_name in (
        "asset_type",
        "topology",
        "age_days",
        "source_path_state",
        "hint_file_id_state",
        "context_payload_state",
        "public_result_state",
        "public_fallback_result_state",
    ):
        assert getattr(current_case, field_name) == getattr(historical_case, field_name)

    assert current_case.expected_resolver == "qq_expired_after_napcat"
    assert historical_case.expected_resolver == "missing_after_napcat"


def test_historical_exact_friend_speech_reference_is_not_part_of_current_matrix() -> None:
    results = run_asset_resolution_matrix()
    summary = summarize_asset_resolution_catalog()

    assert all(item.matched for item in results)
    assert "historical_exact_friend_speech_reference" not in summary["suite_counts"]
    historical_cases = historical_exact_friend_speech_reference_scenarios()
    assert len(historical_cases) == 1
    assert historical_cases[0].expected_resolver == "missing_after_napcat"


def test_triplet_nonmutation_join_snapshot_preserves_request_vs_identity_semantics() -> None:
    result = {
        item.name: item for item in run_asset_resolution_triplet_matrix()
    }["join_nonmutation_top_level_timeout_then_forward_direct_then_repeat"]

    assert result.first_join_snapshot["request_key"] != result.second_join_snapshot["request_key"]
    assert result.first_join_snapshot["request_key"] == result.third_join_snapshot["request_key"]
    assert result.first_join_snapshot["asset_identity_key"] == result.second_join_snapshot["asset_identity_key"]
    assert result.second_join_snapshot["asset_identity_key"] == result.third_join_snapshot["asset_identity_key"]
    assert result.first_algebra["terminality_class"] == "unresolved"
    assert result.second_algebra["terminality_class"] == "recovered"
    assert result.third_algebra["terminality_class"] == "recovered"
    assert result.second_algebra["terminality_class"] == "recovered"


def test_join_schema_contradiction_checker_rejects_inconsistent_scenarios() -> None:
    scenario = AssetResolutionScenario(
        name="invalid_top_level_recursive_family",
        asset_type="image",
        topology="top_level",
        forward_recursive_family="forward_leaf",
    )
    issues = validate_join_schema_scenario(scenario)

    assert "top_level_cannot_have_forward_recursive_family" in issues

    alias_budget_speech = AssetResolutionScenario(
        name="invalid_alias_exact_non_speech_mp3",
        asset_type="image",
        topology="nested_forward",
        forward_recursive_family="forward_chain_transition",
        forward_expansion_state="alias_repeat",
        depth_semantics="exact",
        speech_original_format="amr",
        speech_requested_out_format="mp3",
        segment_path_provenance="dynamicFacePath",
    )
    alias_budget_issues = validate_join_schema_scenario(alias_budget_speech)

    assert "alias_repeat_requires_alias_repeat_family" in alias_budget_issues
    assert "non_exact_expansion_requires_lower_bound_depth" in alias_budget_issues
    assert "non_speech_cannot_request_record_output_format" in alias_budget_issues
    assert "non_speech_cannot_claim_speech_original_format" in alias_budget_issues
    assert "sticker_path_provenance_requires_sticker_asset" in alias_budget_issues

    budget_family = AssetResolutionScenario(
        name="invalid_budget_family_mismatch",
        asset_type="file",
        topology="nested_forward",
        forward_recursive_family="forward_chain_transition",
        forward_expansion_state="budget_cut",
        depth_semantics="lower_bound",
    )
    budget_issues = validate_join_schema_scenario(budget_family)

    assert "budget_cut_requires_budget_cut_family" in budget_issues


def test_asset_resolution_sequence_reuses_old_forward_timeout_classification() -> None:
    scenario = {
        item.name: item
        for item in all_asset_resolution_scenarios()
    }["forward_old_video_public_token_timeout"]

    result = run_asset_resolution_sequence(scenario, repeats=3)

    assert result.matched is True
    assert result.actual_resolver == "qq_expired_after_napcat"
    assert result.actual_path_kind == "missing"
    assert result.client_call_count == 1
    assert result.fast_call_count == 1
    assert result.remote_attempt_count == 0


def test_asset_resolution_sequence_reuses_route_unavailable_fast_fail() -> None:
    scenario = {
        item.name: item
        for item in all_asset_resolution_scenarios()
    }["forward_old_video_route_unavailable"]

    result = run_asset_resolution_sequence(scenario, repeats=3)

    assert result.matched is True
    assert result.actual_resolver == "qq_expired_after_napcat"
    assert result.actual_path_kind == "missing"
    assert result.client_call_count == 0
    assert result.fast_call_count == 1
    assert result.remote_attempt_count == 0


def test_asset_resolution_sequence_reuses_public_token_shape_drift_success() -> None:
    scenario = {
        item.name: item
        for item in all_asset_resolution_scenarios()
    }["public_token_shape_drift_forward_video_valid_remote"]

    result = run_asset_resolution_sequence(scenario, repeats=3)

    assert result.matched is True
    assert result.actual_resolver == "napcat_public_token_get_file_remote_url"
    assert result.actual_path_kind == "remote"
    assert result.client_call_count == 2
    assert result.fast_call_count == 1
    assert result.remote_attempt_count == 1


def test_asset_resolution_sequence_reuses_public_token_remote_url_only_success() -> None:
    scenario = {
        item.name: item
        for item in all_asset_resolution_scenarios()
    }["public_token_shape_drift_forward_video_valid_remote_only"]

    result = run_asset_resolution_sequence(scenario, repeats=3)

    assert result.matched is True
    assert result.actual_resolver == "napcat_public_token_get_file_remote_url"
    assert result.actual_path_kind == "remote"
    assert result.client_call_count == 2
    assert result.fast_call_count == 1
    assert result.remote_attempt_count == 1


def test_asset_resolution_sequence_reuses_payload_only_direct_file_id_fast_fail() -> None:
    scenario = {
        item.name: item
        for item in all_asset_resolution_scenarios()
    }["exhaustive_forward_video_stale_missing_blank_payload_payload_file_id"]

    result = run_asset_resolution_sequence(scenario, repeats=3)

    assert result.matched is True
    assert result.actual_resolver == "qq_expired_after_napcat"
    assert result.actual_path_kind == "missing"
    assert result.client_call_count == 1
    assert result.fast_call_count == 1
    assert result.remote_attempt_count == 0


def test_asset_resolution_sequence_reuses_forward_image_dead_remote_terminal_classification() -> None:
    scenario = {
        item.name: item
        for item in all_asset_resolution_scenarios()
    }["exhaustive_forward_image_recent_none_dead_remote_metadata_timeout_materialize_empty"]

    result = run_asset_resolution_sequence(scenario, repeats=3)

    assert result.matched is True
    assert result.actual_resolver == "qq_expired_after_napcat"
    assert result.actual_path_kind == "missing"
    assert result.client_call_count == 0
    assert result.fast_call_count == 0
    assert result.remote_attempt_count == 2
