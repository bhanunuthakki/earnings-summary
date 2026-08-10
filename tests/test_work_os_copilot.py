"""Structural contract for the read-only Work OS Copilot workspace."""

from __future__ import annotations

import re

from pipeline.work_os_copilot import render_work_os_copilot


def test_copilot_is_one_overlay_workspace_not_an_application_destination() -> None:
    html = render_work_os_copilot()

    assert html.count('id="workOsCopilot"') == 1
    assert 'class="screen-view' not in html
    assert 'role="dialog"' in html
    assert 'aria-modal="true"' in html
    assert 'data-mode="canvas"' in html
    assert 'id="workOsCopilotHistory"' in html
    assert 'id="workOsCopilotThread"' in html
    assert 'id="workOsCopilotComposer"' in html
    assert 'id="workOsCopilotEvidence"' in html
    assert 'data-copilot-mode="fullscreen"' in html


def test_copilot_history_and_context_controls_are_dense_real_filters() -> None:
    html = render_work_os_copilot()

    assert 'id="workOsCopilotNewChat"' in html
    assert 'id="workOsCopilotHistorySearch"' in html
    assert 'id="workOsCopilotCompany"' in html
    assert 'id="workOsCopilotCategory"' in html
    assert 'class="work-os-copilot-filter-row work-os-copilot-filter-context"' in html
    assert '<label class="k-label" for="workOsCopilotCompany">Co.</label>' in html
    assert '<label class="k-label" for="workOsCopilotCategory">Type</label>' in html
    assert '<option value="">All</option>' in html
    assert '<option value="research">Research</option>' in html
    assert '<option value="thesis">Decision</option>' in html
    assert '<option value="governed_fact">Metrics</option>' in html
    assert '<option value="decision">' not in html
    assert '<option value="metrics">' not in html
    assert "grid-template-columns: auto minmax(0, 1fr) auto minmax(0, 1fr)" in html
    assert 'data-copilot-coverage="portfolio"' in html
    assert 'data-copilot-coverage="evaluation"' in html
    assert (
        'aria-label="Evaluation" data-copilot-coverage="evaluation" '
        'type="button">Eval</button>' in html
    )
    assert "filterCopilotSessions" in html
    assert "populateCopilotCompanies" in html
    assert "No conversations match these filters." in html
    assert "No governed conversation history yet." in html
    assert "function formatSessionUpdatedAt(value)" in html
    assert "timeZone: 'UTC'" in html
    assert "[company, coverage, category, thesisVersion, updated]" in html
    assert "context.thesis_version" in html


def test_copilot_reuses_ask_session_crud_and_stream_contract() -> None:
    html = render_work_os_copilot()

    assert "fetch('/api/ask/sessions?limit=200'" in html
    assert "fetch('/api/ask/sessions/' + encodeURIComponent" in html
    assert "method: 'PATCH'" in html
    assert "method: 'DELETE'" in html
    assert "fetch('/api/ask/stream'" in html
    assert "request_id: requestId" in html
    assert "session_context:" in html
    assert "coverage_role_at_creation" in html
    assert "lifecycle_at_creation" in html
    assert "research_context:" in html
    assert "category_at_creation" not in html
    assert "var categories = ['general', 'research', 'governed_fact', 'thesis', 'kpi']" in html
    assert "category: category" in html
    assert "activeCoverage === 'all' ? 'unknown' : activeCoverage" in html
    assert "categorySelect.value || 'general'" in html
    assert "lifecycle_at_creation: pendingContext.lifecycle_at_creation || 'unknown'" in html
    for event_type in (
        "session",
        "stage",
        "delta",
        "fragment",
        "final",
        "citations",
        "diff_proposal",
        "artifacts",
        "proposal_ref",
        "proposal_error",
        "error",
    ):
        assert f"case '{event_type}':" in html


def test_copilot_emits_ascii_safe_text_for_the_shell_transport() -> None:
    html = render_work_os_copilot()

    assert html.isascii()
    assert "Â·" not in html
    assert "â€¦" not in html


def test_copilot_proposals_require_explicit_typed_approval() -> None:
    html = render_work_os_copilot()

    assert "action.textContent = 'Approve change'" in html
    assert "reject.textContent = 'Keep current'" in html
    assert "decideCopilotProposal(card, ref, 'approve')" in html
    assert "decideCopilotProposal(card, ref, 'reject')" in html
    assert "ref.allowed_actions.includes('reject')" in html
    assert "openCopilotEvidence" in html
    assert "citation.href || ''" in html
    assert "citation.source_url || ''" in html
    assert "citation.doc_type" in html
    assert "citation.period" in html
    assert "citation.ticker" in html
    assert "Open original source" in html
    assert "report_source_tangent" not in html
    assert "tangent_url" not in html
    assert "screen-analytics-playground" in html
    assert "data-fact-ref" in html
    assert "/apply" not in html


def test_copilot_internal_citation_action_has_contextual_source_copy() -> None:
    html = render_work_os_copilot()

    assert "function sourceContextLabel(citation)" in html
    assert "return 'Open ' + parts.join(' ')" in html
    assert "if (!parts.length) return 'Open cited source'" in html
    assert "humanizeCitationPart(citation.doc_type)" in html
    assert "original !== internal" in html


def test_existing_session_context_is_an_immutable_snapshot() -> None:
    html = render_work_os_copilot()

    assert "var currentSessionContext = null" in html
    assert "Object.assign({}, session.session_context)" in html
    assert "currentSessionContext = null" in html
    assert "function buildNewSessionContext()" in html
    assert "if (!currentSessionContext) currentSessionContext = buildNewSessionContext()" in html
    assert "session_context: currentSessionContext" in html
    assert "if (event.session_context)" in html
    assert "renderCopilotContext()" in html
    assert "snapshot.thesis_version" in html
    assert "['portfolio', 'evaluation', 'unknown'].includes" in html
    assert "['active', 'archived', 'unknown'].includes" in html
    assert "var currentSessionRevision = 0" in html
    assert "Number.isInteger(session.session_revision)" in html
    assert "Number.isInteger(event.session_revision)" in html
    assert "expected_revision: currentSessionRevision" in html
    assert "currentSessionRevision +=" not in html


def test_session_switch_resets_context_and_rehydrates_typed_exchange_artifacts() -> None:
    html = render_work_os_copilot()

    load_start = html.index("function loadCopilotSession(sessionId)")
    fetch_start = html.index("fetch('/api/ask/sessions/' + encodeURIComponent(sessionId)", load_start)
    assert html.index("lastSpec = null", load_start, fetch_start) < fetch_start
    assert html.index("currentSessionContext = null", load_start, fetch_start) < fetch_start
    assert "var exchanges = Array.isArray(session.exchange_artifacts) ? session.exchange_artifacts : []" in html
    assert "exchangeArtifact.schema_version !== 'session_exchange_artifact.v1'" in html
    assert "typeof exchangeArtifact.exchange_id !== 'string'" in html
    assert "exchangeArtifact.exchange_id !== exchangeArtifact.request_id" in html
    assert "!Number.isInteger(exchangeArtifact.assistant_turn_id)" in html
    assert "artifact.schema_version !== 'exchange_artifacts.v1'" in html
    assert "exchanges.forEach(function (exchangeArtifact)" in html
    assert "lastSpec = artifact.view_spec" in html
    assert "fetch('/api/viewspec/run'" in html
    assert "normalizeProposalRef(artifact.proposal_ref)" in html
    assert "renderCopilotProposal(host, {ref: ref, diff: null})" in html
    assert "sessionLoadToken += 1" in html


def test_session_artifact_proposal_error_reuses_the_live_safe_renderer() -> None:
    html = render_work_os_copilot()

    assert "artifact.proposal_error.schema_version === 'proposal_error.v1'" in html
    assert "normalizeProposalEventError({error: artifact.proposal_error})" in html
    assert "renderCopilotProposalError(host, persistedError)" in html
    assert "typeof event.code === 'string' ? event.code" in html
    assert "typeof event.message === 'string' ? event.message" in html


def test_generic_new_chat_does_not_inherit_the_active_company() -> None:
    html = render_work_os_copilot()

    assert "var company = pendingContext.company_ticker || companySelect.value;" in html
    assert "String(window.workOsActiveTicker" not in html
    assert "company_ticker: company || null" in html
    assert "if (pendingContext.company_ticker) companySelect.value" in html


def test_thesis_hash_is_compact_only_at_the_display_boundary() -> None:
    html = render_work_os_copilot()

    assert "function formatThesisVersion(value)" in html
    assert "/^[a-f0-9]{64}$/i.test(raw)" in html
    assert "'Thesis ' + raw.slice(0, 8)" in html
    assert "values.push(formatThesisVersion(snapshot.thesis_version))" in html
    assert "snapshot.thesis_version = pendingContext.thesis_version" in html
    assert "session_context: currentSessionContext" in html


def test_replay_artifacts_and_proposal_refs_remain_governed() -> None:
    html = render_work_os_copilot()

    assert "var artifacts = event.artifacts || event" in html
    assert "lastSpec = artifacts.view_spec || lastSpec" in html
    assert "normalizeProposalRef(event.proposal_ref || event.ref)" in html
    assert "state.proposals.push({ref: ref, diff: null})" in html
    assert "lastSpec = artifacts.view_spec || lastSpec" in html


def test_proposals_render_compact_comparisons_not_raw_json() -> None:
    html = render_work_os_copilot()

    assert "JSON.stringify(proposal" not in html
    assert "proposal.target_path" in html
    assert "proposal.old_value" in html
    assert "proposal.new_value" in html
    assert "work-os-copilot-proposal-grid" in html
    assert ".work-os-copilot-proposal-action { margin-inline-start: auto; }" in html
    assert "proposalDisplayValue" in html


def test_kpi_proposals_render_every_changed_field_as_structured_values() -> None:
    html = render_work_os_copilot()

    assert "function renderKpiProposalComparison(oldEntries, newEntries)" in html
    assert "var kpiFields = [" in html
    for field in (
        "name",
        "current",
        "prior",
        "yoy",
        "status",
        "break_condition",
        "source",
        "frequency",
        "as_of",
        "note",
        "notes",
    ):
        assert f"'{field}'" in html
    assert "if (oldValue === newValue) return" in html
    assert "appendKpiChangeRow(rows, entryLabel, field, oldValue, newValue)" in html
    assert "work-os-copilot-kpi-row" in html
    assert "String(value.length) + ' items'" not in html
    assert "Object.keys(value).slice(0, 3).join" not in html


def test_proposal_contract_uses_only_backend_owned_links() -> None:
    html = render_work_os_copilot()

    assert "function normalizeProposalRef(value)" in html
    assert "typeof rawId === 'string' || Number.isInteger(rawId)" in html
    assert "var proposalId = Number(rawId)" in html
    assert "proposal_id: proposalId" in html
    assert "function sameOriginActionUrl(value)" in html
    assert "fetch(ref.detail_url" in html
    assert "fetch(ref.decision_url" in html
    assert "schema_version: 'ask_proposal_decision.v1'" in html
    assert "proposal_id: ref.proposal_id" in html
    assert "expected_proposal_revision: ref.proposal_revision" in html
    assert "decision_request_id: decisionRequestId" in html
    assert "var authoritativeRef = normalizeProposalRef(detail)" in html
    assert "authoritativeRef.proposal_id !== ref.proposal_id" in html
    assert '"/api/ask/proposal' not in html


def test_proposal_actions_have_idempotent_visible_state_handling() -> None:
    html = render_work_os_copilot()

    assert "card.dataset[requestKey] || buildRequestId()" in html
    assert "card.dataset[requestKey] = decisionRequestId" in html
    assert "response.status === 409" in html
    assert "response.status === 412" in html
    for label in (
        "Approval pending...",
        "Change approved",
        "Proposal changed; review the latest version.",
        "Target changed since this proposal.",
        "Approval failed. Retry is safe.",
        "Kept current",
    ):
        assert label in html
    assert "updateProposalCardState" in html
    assert "card.dataset.proposalDetailReady = 'false'" in html
    assert "terminal || !detailReady" in html
    assert ".work-os-copilot-proposal-actions { margin-inline-start: auto;" in html


def test_proposal_errors_have_typed_recovery_paths() -> None:
    html = render_work_os_copilot()

    assert "function normalizeProposalError(result, ref)" in html
    assert "result.schema_version !== 'ask_proposal_error.v1'" in html
    assert "error.code === 'mutation_busy'" in html
    assert "Retry approval" in html
    assert "error.code === 'revision_conflict' || error.code === 'status_conflict'" in html
    assert "Review latest" in html
    assert "loadCopilotProposalDetail(card, ref, true)" in html
    assert "error.code === 'idempotency_conflict'" in html
    assert "Decision request conflicts with a different prior action." in html
    assert "error.code === 'target_drift'" in html
    assert "Target changed since this proposal." in html
    assert "card.dataset[requestKey] = decisionRequestId" in html


def test_proposal_detail_failure_has_a_functional_retry() -> None:
    html = render_work_os_copilot()

    assert "Retry details" in html
    assert "loadCopilotProposalDetail(card, ref, true)" in html
    assert "offerProposalRecoveryAction" in html
    assert "Proposal details could not be loaded." in html


def test_proposal_error_events_remain_visible_in_the_completed_turn() -> None:
    html = render_work_os_copilot()

    assert "case 'proposal_error':" in html
    assert "state.proposalErrors.push(normalizeProposalEventError(event))" in html
    assert "proposalErrors: []" in html
    assert "state.proposalErrors.forEach(function (proposalError)" in html
    assert "renderCopilotProposalError(state.turn, proposalError)" in html
    assert "role', 'alert'" in html


def test_proposal_state_transitions_restore_a_safe_focus_target() -> None:
    html = render_work_os_copilot()

    assert "card.tabIndex = -1" in html
    assert "proposalStatus.tabIndex = -1" in html
    assert "function focusProposalTarget(card, target)" in html
    assert "target.focus({preventScroll: true})" in html
    assert "focusProposalTarget(card, recovery)" in html
    assert "focusProposalTarget(card, proposalStatus)" in html


def test_new_chat_suggestions_route_through_the_same_composer() -> None:
    html = render_work_os_copilot()

    assert "var suggestions = [" in html
    assert "button.className = 'k-chip k-chip-btn'" in html
    assert "button.dataset.copilotSuggestion = prompt" in html
    assert "What changed since the last review?" in html
    assert "Show the latest governed KPIs." in html
    assert "Stress-test the current thesis." in html
    assert "function renderNewChatEmptyState()" in html
    assert "form.requestSubmit()" in html


def test_copilot_has_resilient_states_and_keyboard_mobile_contracts() -> None:
    html = render_work_os_copilot()

    assert 'role="status"' in html
    assert 'role="alert"' in html
    assert "Loading conversations" in html
    assert "Copilot is temporarily unavailable" in html
    assert "ev.key === 'Escape'" in html
    assert "ev.key.toLowerCase() === 'k'" in html
    assert "restoreFocus" in html
    assert "trapCopilotFocus" in html
    assert "@media (max-width:" in html
    assert "min-block-size: var(--touch-target-size)" in html
    assert "font-size: var(--mobile-control-font-size)" in html
    assert "overflow-x: hidden" in html
    assert "prefers-reduced-motion: reduce" in html
    assert "#workOsCopilotFullscreen { display: none; }" in html
    assert not re.search(r"(?<![-\w])\d+(?:\.\d+)?px", html)
