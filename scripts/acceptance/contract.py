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
INSTALLED_CASES = ("first_install", "cross_version_upgrade", "same_version_repair",
                   "cleanup_failure_retry", "complete_uninstall", "reinstall_pristine")


def complete_installed_lifecycle(report):
    cases = report.get("cases", {})
    previous = report.get("previous_installer", {})
    if not isinstance(previous, dict):
        return False
    try:
        old = tuple(map(int, previous.get("version", "").split(".")))
        new = tuple(map(int, report.get("version", "").split(".")))
    except (ValueError, AttributeError):
        return False
    digest = previous.get("sha256", "")
    return (report.get("schema_version") == 2 and report.get("production_installer_executed") is True
            and report.get("test_credentials_only") is True
            and report.get("real_user_credentials_accessed") is False
            and isinstance(digest, str) and len(digest) == 64 and set(digest) <= set("0123456789abcdef")
            and len(old) == len(new) == 3 and old < new
            and isinstance(cases, dict) and set(cases) == set(INSTALLED_CASES)
            and all(isinstance(cases[name], dict) and cases[name].get("status") == "passed" and cases[name].get("passed") is True
                    for name in INSTALLED_CASES))


def complete_milestones(phase, values):
    required = FIRST_MILESTONES if phase == "first" else RESTART_MILESTONES
    return isinstance(values, list) and len(values) == len(required) and set(values) == required
