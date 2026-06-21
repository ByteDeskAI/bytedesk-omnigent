"""Read API for high-value third-party integration capability blueprints."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from bytedesk_omnigent.integration_capabilities import (
    CapabilityCategory,
    compile_integration_marketplace_listing,
    compile_integration_staffing_plan,
    get_integration_capability,
    integration_capability_categories,
    list_integration_capabilities,
)
from bytedesk_omnigent.integration_consent_manifest import (
    compile_integration_consent_manifest,
)
from bytedesk_omnigent.integration_verification_matrix import (
    compile_integration_verification_matrix,
)
from bytedesk_omnigent.integration_demo_scenarios import (
    compile_integration_demo_scenario,
)
from bytedesk_omnigent.integration_readiness_assessment import (
    compile_integration_readiness_assessment,
)
from bytedesk_omnigent.integration_dependency_graph import (
    compile_integration_dependency_graph,
)
from bytedesk_omnigent.integration_risk_register import (
    compile_integration_risk_register,
)
from bytedesk_omnigent.integration_cutover_checklist import (
    compile_integration_cutover_checklist,
)
from bytedesk_omnigent.integration_sandbox_fixtures import (
    compile_integration_sandbox_fixtures,
)
from bytedesk_omnigent.integration_verification_assessment import (
    assess_integration_verification_evidence,
)
from bytedesk_omnigent.integration_autonomy_policy import (
    compile_integration_autonomy_policy,
)
from bytedesk_omnigent.integration_incident_drills import (
    compile_integration_incident_drill,
)
from bytedesk_omnigent.integration_recommendations import (
    recommend_integration_capabilities,
)
from bytedesk_omnigent.integration_evidence_packet import (
    compile_integration_evidence_packet,
)
from bytedesk_omnigent.integration_tenant_routing import (
    compile_integration_tenant_routing_manifest,
)
from bytedesk_omnigent.integration_gap_analysis import (
    IntegrationImplementationSignal,
    analyze_integration_capability_gaps,
)
from bytedesk_omnigent.integration_pilot_plans import compile_integration_pilot_plan
from bytedesk_omnigent.integration_acceptance_suite import (
    compile_integration_acceptance_suite,
)
from bytedesk_omnigent.integration_redaction_profile import (
    compile_integration_redaction_profile,
)
from bytedesk_omnigent.integration_value_scorecards import (
    compile_integration_value_scorecard,
)
from bytedesk_omnigent.integration_telemetry_contract import (
    compile_integration_telemetry_contract,
)
from bytedesk_omnigent.integration_tool_contracts import (
    compile_integration_tool_contract,
)
from bytedesk_omnigent.integration_coordination_topology import (
    compile_integration_coordination_topology,
)
from bytedesk_omnigent.integration_remediation_playbook import (
    compile_integration_remediation_playbook,
)
from bytedesk_omnigent.integration_evidence_assessment import (
    IntegrationEvidenceItem,
    assess_integration_evidence,
)
from bytedesk_omnigent.integration_data_boundary import (
    compile_integration_data_boundary,
)
from bytedesk_omnigent.integration_ownership_matrix import (
    compile_integration_ownership_matrix,
)
from bytedesk_omnigent.integration_deprecation_plan import (
    compile_integration_deprecation_plan,
)
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user


def create_integration_capabilities_router(
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the integration capability catalog router.

    The catalog is read-only product metadata. It is still authenticated in
    multi-user mode because entries expose platform roadmap intent and business
    prioritization; single-user/local mode keeps it open like sibling ByteDesk
    extension read routes.
    """

    router = APIRouter()

    @router.get("/integration-capabilities")
    async def list_capabilities(
        request: Request,
        category: CapabilityCategory | None = None,
        limit: int = Query(default=50, ge=1, le=100),
    ) -> JSONResponse:
        """List integration blueprints ordered by product priority."""

        require_user(request, auth_provider)
        entries = list_integration_capabilities(category=category, limit=limit)
        return JSONResponse(
            {
                "object": "list",
                "data": [entry.to_dict() for entry in entries],
                "categories": integration_capability_categories(),
            }
        )

    @router.get("/integration-capabilities/{slug}")
    async def get_capability(request: Request, slug: str) -> JSONResponse:
        """Read one integration blueprint by slug."""

        require_user(request, auth_provider)
        entry = get_integration_capability(slug)
        if entry is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"unknown integration capability: {slug}"},
                status_code=404,
            )
        return JSONResponse(entry.to_dict())

    @router.get("/integration-capabilities/{slug}/consent-manifest")
    async def get_capability_consent_manifest(
        request: Request, slug: str
    ) -> JSONResponse:
        """Compile consent copy and scope rationales for one integration."""

        require_user(request, auth_provider)
        manifest = compile_integration_consent_manifest(slug)
        if manifest is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"unknown integration capability: {slug}"},
                status_code=404,
            )
        return JSONResponse(manifest)

    @router.get("/integration-capabilities/{slug}/verification-matrix")
    async def get_capability_verification_matrix(
        request: Request, slug: str
    ) -> JSONResponse:
        """Compile rollout verification gates for one integration blueprint."""

        require_user(request, auth_provider)
        matrix = compile_integration_verification_matrix(slug)
        if matrix is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"unknown integration capability: {slug}"},
                status_code=404,
            )
        return JSONResponse(matrix)

    @router.get("/integration-capabilities/{slug}/marketplace-listing")
    async def get_capability_marketplace_listing(request: Request, slug: str) -> JSONResponse:
        """Compile one integration blueprint into ByteDesk marketplace metadata."""

        require_user(request, auth_provider)
        listing = compile_integration_marketplace_listing(slug)
        if listing is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"unknown integration capability: {slug}"},
                status_code=404,
            )
        return JSONResponse(listing.to_dict())

    @router.get("/integration-capabilities/{slug}/staffing-plan")
    async def get_staffing_plan(request: Request, slug: str) -> JSONResponse:
        """Read the deterministic agent staffing plan for one blueprint."""

        require_user(request, auth_provider)
        plan = compile_integration_staffing_plan(slug)
        if plan is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"unknown integration capability: {slug}"},
                status_code=404,
            )
        return JSONResponse(plan.to_dict())


    @router.get("/integration-capabilities/{slug}/demo-scenario")
    async def get_capability_demo_scenario(request: Request, slug: str) -> JSONResponse:
        """Read a deterministic demo scenario for one integration blueprint."""

        require_user(request, auth_provider)
        scenario = compile_integration_demo_scenario(slug)
        if scenario is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"unknown integration capability: {slug}"},
                status_code=404,
            )
        return JSONResponse(scenario.to_dict())

    @router.get("/integration-capabilities/{slug}/dependency-graph")
    async def get_capability_dependency_graph(
        request: Request, slug: str
    ) -> JSONResponse:
        """Compile delivery dependency milestones for one integration blueprint."""

        require_user(request, auth_provider)
        graph = compile_integration_dependency_graph(slug)
        if graph is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"unknown integration capability: {slug}"},
                status_code=404,
            )
        return JSONResponse(graph)

    @router.get("/integration-capabilities/{slug}/risk-register")
    async def get_capability_risk_register(request: Request, slug: str) -> JSONResponse:
        """Compile rollout risks and controls for one integration blueprint."""

        require_user(request, auth_provider)
        register = compile_integration_risk_register(slug)
        if register is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"unknown integration capability: {slug}"},
                status_code=404,
            )
        return JSONResponse(register)

    @router.get("/integration-capabilities/{slug}/cutover-checklist")
    async def get_capability_cutover_checklist(
        request: Request, slug: str
    ) -> JSONResponse:
        """Compile the activation cutover checklist for one integration."""

        require_user(request, auth_provider)
        checklist = compile_integration_cutover_checklist(slug)
        if checklist is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"unknown integration capability: {slug}"},
                status_code=404,
            )
        return JSONResponse(checklist)

    @router.get("/integration-capabilities/{slug}/sandbox-fixtures")
    async def get_capability_sandbox_fixtures(
        request: Request, slug: str
    ) -> JSONResponse:
        """Compile credentialless sandbox fixtures for one integration blueprint."""

        require_user(request, auth_provider)
        fixtures = compile_integration_sandbox_fixtures(slug)
        if fixtures is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"unknown integration capability: {slug}"},
                status_code=404,
            )
        return JSONResponse(fixtures)

    @router.get("/integration-capabilities/{slug}/autonomy-policy")
    async def get_capability_autonomy_policy(
        request: Request, slug: str
    ) -> JSONResponse:
        """Compile safe default autonomy boundaries for one integration blueprint."""

        require_user(request, auth_provider)
        policy = compile_integration_autonomy_policy(slug)
        if policy is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"unknown integration capability: {slug}"},
                status_code=404,
            )
        return JSONResponse(policy)

    @router.get("/integration-capabilities/{slug}/incident-drill")
    async def get_capability_incident_drill(request: Request, slug: str) -> JSONResponse:
        """Compile operator incident drills for one integration blueprint."""

        require_user(request, auth_provider)
        drill = compile_integration_incident_drill(slug)
        if drill is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"unknown integration capability: {slug}"},
                status_code=404,
            )
        return JSONResponse(drill)

    @router.get("/integration-capabilities/recommendations")
    async def recommend_capabilities(
        request: Request,
        goal: str = Query(..., description="Natural-language integration goal"),
        category: CapabilityCategory | None = None,
        limit: int = Query(default=3, ge=1, le=10),
    ) -> JSONResponse:
        """Rank catalog entries for a natural-language integration goal."""

        require_user(request, auth_provider)
        if not goal.strip():
            raise HTTPException(status_code=422, detail="goal must not be blank")
        report = recommend_integration_capabilities(goal, category=category, limit=limit)
        return JSONResponse(report.to_dict())

    @router.get("/integration-capabilities/{slug}/evidence-packet")
    async def get_capability_evidence_packet(
        request: Request, slug: str
    ) -> JSONResponse:
        """Compile an operator evidence packet for one integration blueprint."""

        require_user(request, auth_provider)
        packet = compile_integration_evidence_packet(slug)
        if packet is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"unknown integration capability: {slug}"},
                status_code=404,
            )
        return JSONResponse(packet)

    @router.get("/integration-capabilities/{slug}/tenant-routing-manifest")
    async def get_capability_tenant_routing_manifest(
        request: Request, slug: str
    ) -> JSONResponse:
        """Compile tenant/workspace routing rules for one integration blueprint."""

        require_user(request, auth_provider)
        manifest = compile_integration_tenant_routing_manifest(slug)
        if manifest is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"unknown integration capability: {slug}"},
                status_code=404,
            )
        return JSONResponse(manifest)

    @router.get("/integration-capabilities/{slug}/pilot-plan")
    async def get_capability_pilot_plan(request: Request, slug: str) -> JSONResponse:
        """Compile the tenant-safe first pilot plan for one integration."""

        require_user(request, auth_provider)
        plan = compile_integration_pilot_plan(slug)
        if plan is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"unknown integration capability: {slug}"},
                status_code=404,
            )
        return JSONResponse(plan.to_dict())

    @router.get("/integration-capabilities/{slug}/acceptance-suite")
    async def get_capability_acceptance_suite(
        request: Request, slug: str
    ) -> JSONResponse:
        """Compile deterministic acceptance scenarios for one integration blueprint."""

        require_user(request, auth_provider)
        suite = compile_integration_acceptance_suite(slug)
        if suite is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"unknown integration capability: {slug}"},
                status_code=404,
            )
        return JSONResponse(suite)

    @router.get("/integration-capabilities/{slug}/redaction-profile")
    async def get_capability_redaction_profile(
        request: Request, slug: str
    ) -> JSONResponse:
        """Compile secret-safe logging rules for one integration blueprint."""

        require_user(request, auth_provider)
        profile = compile_integration_redaction_profile(slug)
        if profile is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"unknown integration capability: {slug}"},
                status_code=404,
            )
        return JSONResponse(profile)

    @router.get("/integration-capabilities/{slug}/value-scorecard")
    async def get_capability_value_scorecard(
        request: Request, slug: str
    ) -> JSONResponse:
        """Compile product and sales value scoring for one integration blueprint."""

        require_user(request, auth_provider)
        scorecard = compile_integration_value_scorecard(slug)
        if scorecard is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"unknown integration capability: {slug}"},
                status_code=404,
            )
        return JSONResponse(scorecard)

    @router.get("/integration-capabilities/{slug}/telemetry-contract")
    async def get_capability_telemetry_contract(
        request: Request, slug: str
    ) -> JSONResponse:
        """Compile observability events and metrics for one integration blueprint."""

        require_user(request, auth_provider)
        contract = compile_integration_telemetry_contract(slug)
        if contract is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"unknown integration capability: {slug}"},
                status_code=404,
            )
        return JSONResponse(contract)

    @router.get("/integration-capabilities/{slug}/tool-contract")
    async def get_capability_tool_contract(request: Request, slug: str) -> JSONResponse:
        """Compile the least-privilege tool surface for one integration blueprint."""

        require_user(request, auth_provider)
        contract = compile_integration_tool_contract(slug)
        if contract is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"unknown integration capability: {slug}"},
                status_code=404,
            )
        return JSONResponse(contract)

    @router.get("/integration-capabilities/{slug}/coordination-topology")
    async def get_capability_coordination_topology(
        request: Request, slug: str
    ) -> JSONResponse:
        """Compile managed-agent roles and handoffs for one integration."""

        require_user(request, auth_provider)
        topology = compile_integration_coordination_topology(slug)
        if topology is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"unknown integration capability: {slug}"},
                status_code=404,
            )
        return JSONResponse(topology)

    @router.get("/integration-capabilities/{slug}/remediation-playbook")
    async def get_capability_remediation_playbook(
        request: Request,
        slug: str,
        failed_gate_id: list[str] = _FAILED_GATE_ID_QUERY,
    ) -> JSONResponse:
        """Compile repair steps for failed rollout verification gates."""

        require_user(request, auth_provider)
        playbook = compile_integration_remediation_playbook(
            slug, failed_gate_ids=tuple(failed_gate_id)
        )
        if playbook is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"unknown integration capability: {slug}"},
                status_code=404,
            )
        return JSONResponse(playbook)

    @router.get("/integration-capabilities/{slug}/data-boundary")
    async def get_capability_data_boundary(request: Request, slug: str) -> JSONResponse:
        """Compile provider data/mutation boundaries for one integration blueprint."""

        require_user(request, auth_provider)
        manifest = compile_integration_data_boundary(slug)
        if manifest is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"unknown integration capability: {slug}"},
                status_code=404,
            )
        return JSONResponse(manifest)

    @router.get("/integration-capabilities/{slug}/ownership-matrix")
    async def get_capability_ownership_matrix(
        request: Request, slug: str
    ) -> JSONResponse:
        """Compile launch owners and approvers for one integration blueprint."""

        require_user(request, auth_provider)
        matrix = compile_integration_ownership_matrix(slug)
        if matrix is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"unknown integration capability: {slug}"},
                status_code=404,
            )
        return JSONResponse(matrix)

    @router.get("/integration-capabilities/{slug}/deprecation-plan")
    async def get_capability_deprecation_plan(
        request: Request, slug: str
    ) -> JSONResponse:
        """Compile safe retirement phases for one integration blueprint."""

        require_user(request, auth_provider)
        plan = compile_integration_deprecation_plan(slug)
        if plan is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"unknown integration capability: {slug}"},
                status_code=404,
            )
        return JSONResponse(plan)

    return router
