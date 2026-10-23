"""Required frozen acceptance milestones, shared by driver and release gates."""
FIRST_MILESTONES = frozenset((
    "download_failure_and_automatic_retries", "bank_download_extract_import_tag_concepts",
    "bank_case_distinct_paths_preserved", "invalid_config_and_model_404_recovery",
    "model_explicit_test_and_secure_save", "project_selection", "watcher_baseline",
    "seven_embedded_pages_icons_and_navigation_keep_baselines", "rejected_review_blocks_popup_then_explicit_retry",
    "new_quiz_updates_pending_without_stealing_page", "real_file_change_debounce_embedded_practice",
    "grade_analysis_and_sqlite_persistence", "settings_edit_retest_save_and_updated_receipt",
    "history_snapshot_repractice_and_idempotent_second_answer"))
RESTART_MILESTONES = frozenset(("restart_direct_main_and_history_restore", "updated_model_receipt_restored_without_network"))
REQUESTS = {"downloads": 4, "probes": 2, "quizzes": 2, "reviews": 2, "rejected": 1}


def complete_milestones(phase, values):
    required = FIRST_MILESTONES if phase == "first" else RESTART_MILESTONES
    return isinstance(values, list) and len(values) == len(required) and set(values) == required
