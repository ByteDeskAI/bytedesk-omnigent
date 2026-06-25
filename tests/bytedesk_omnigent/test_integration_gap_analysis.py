"""Integration capability gap analysis tests."""

from __future__ import annotations

from bytedesk_omnigent.integration_gap_analysis import (
    IntegrationImplementationSignal,
    analyze_integration_capability_gaps,
)


def test_gap_analysis_prioritizes_uncovered_catalog_entries_and_notes_open_work():
    report = analyze_integration_capability_gaps(
        implemented_slugs={"slack-command-center"},
        open_signals=(
            IntegrationImplementationSignal(
                slug="archon-style-workflow-blueprints",
                source="pr#118",
                title="feat: add integration workflow harness compiler",
                url="https://github.com/ByteDeskAI/bytedesk-omnigent/pull/118",
            ),
        ),
    )

    assert report.total_catalog_entries >= 10
    assert report.implemented_count == 1
    assert report.open_work_count == 1
    assert report.next_recommended_slug == "linear-jira-work-intake"
    assert [gap.slug for gap in report.gaps[:3]] == [
        "linear-jira-work-intake",
        "github-engineering-copilot",
        "google-workspace-operator",
    ]
    assert report.open_work[0].slug == "archon-style-workflow-blueprints"
    assert report.open_work[0].source == "pr#118"


def test_gap_analysis_matches_signals_from_titles_without_exact_slug():
    report = analyze_integration_capability_gaps(
        open_signals=(
            IntegrationImplementationSignal(
                slug=None,
                source="pr#152",
                title="feat: add Notion backfill importer for knowledge operator",
            ),
        ),
    )

    slugs = {entry.slug for entry in report.gaps}

    assert "notion-knowledge-operator" not in slugs
    assert report.open_work[0].slug == "notion-knowledge-operator"
    assert report.open_work[0].title == "feat: add Notion backfill importer for knowledge operator"


def test_gap_analysis_drops_signals_with_unresolvable_titles():
    report = analyze_integration_capability_gaps(
        open_signals=(
            IntegrationImplementationSignal(
                slug=None,
                source="pr#999",
                title="totally unrelated random feature title",
            ),
        ),
    )
    assert report.open_work_count == 0
    assert report.open_work == ()


def test_gap_analysis_output_is_json_ready_and_secret_free():
    report = analyze_integration_capability_gaps(
        implemented_slugs={"slack-command-center"},
        open_signals=(
            IntegrationImplementationSignal(
                slug="github-engineering-copilot",
                source="branch",
                title="feature/loop/iteration_x",
                url="https://example.test/pr",
            ),
        ),
    )

    data = report.to_dict()

    assert data["object"] == "integration_capability_gap_report"
    assert data["covered_slugs"] == ["github-engineering-copilot", "slack-command-center"]
    assert data["next_recommended_slug"] == "archon-style-workflow-blueprints"
    assert data["gaps"][0]["slug"] == "archon-style-workflow-blueprints"
    assert set(data) == {
        "object",
        "total_catalog_entries",
        "implemented_count",
        "open_work_count",
        "covered_slugs",
        "next_recommended_slug",
        "gaps",
        "open_work",
    }
