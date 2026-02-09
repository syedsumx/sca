"""Issue tracker integration API endpoints (Jira, Azure Boards)."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from codescope.integrations.issue_trackers import (
    TrackerConfig,
    TrackerIssue,
    IssuePriority,
    IssueStatus,
    get_issue_tracker,
    get_configured_trackers,
)
from codescope.integrations.issue_trackers.registry import (
    create_tracker_from_config,
    get_available_providers,
    clear_cache,
)
from codescope.integrations.issue_trackers.base import CreateIssueRequest, UpdateIssueRequest

router = APIRouter()

# In-memory store for tracker configurations (persisted to DB in production)
_tracker_configs: dict[str, dict] = {}


# ── Request/Response Models ─────────────────────────────────────────────


class TrackerConfigCreate(BaseModel):
    """Request to create/update a tracker configuration."""

    provider: str = Field(..., description="Provider type: 'jira' or 'azure_boards'")
    base_url: str = Field("", description="Base URL for the tracker (required for Jira)")
    project_key: str = Field(..., description="Project key or name")
    api_token: str = Field(..., description="API token or PAT")
    username: str = Field("", description="Username (for Jira Cloud)")
    organization: str = Field("", description="Organization (for Azure DevOps)")
    default_issue_type: str = Field("Bug", description="Default issue type for new issues")
    default_labels: list[str] = Field(default_factory=list, description="Default labels/tags")


class TrackerConfigResponse(BaseModel):
    """Tracker configuration response (secrets masked)."""

    id: str
    provider: str
    base_url: str
    project_key: str
    username: str
    organization: str
    default_issue_type: str
    default_labels: list[str]
    is_connected: bool
    connection_message: str


class CreateIssueBody(BaseModel):
    """Request body for creating an issue."""

    title: str = Field(..., min_length=1, max_length=500)
    description: str = Field("", max_length=32000)
    priority: str = Field("medium", description="Priority: highest, high, medium, low, lowest")
    issue_type: str = Field("Bug", description="Issue type (e.g., Bug, Task, Story)")
    assignee: str = Field("", description="Assignee user ID")
    labels: list[str] = Field(default_factory=list)
    codescope_issue_ids: list[str] = Field(
        default_factory=list, description="CodeScope issue IDs to link"
    )


class UpdateIssueBody(BaseModel):
    """Request body for updating an issue."""

    title: Optional[str] = Field(None, min_length=1, max_length=500)
    description: Optional[str] = Field(None, max_length=32000)
    status: Optional[str] = Field(None, description="Status: open, in_progress, resolved, closed")
    priority: Optional[str] = Field(None, description="Priority: highest, high, medium, low, lowest")
    assignee: Optional[str] = None
    labels: Optional[list[str]] = None


class AddCommentBody(BaseModel):
    """Request body for adding a comment."""

    comment: str = Field(..., min_length=1, max_length=32000)


class SearchIssuesParams(BaseModel):
    """Query parameters for searching issues."""

    query: Optional[str] = None
    status: Optional[str] = None
    labels: Optional[str] = Field(None, description="Comma-separated labels")
    max_results: int = Field(50, ge=1, le=100)


class BulkCreateRequest(BaseModel):
    """Request for bulk issue creation from CodeScope issues."""

    codescope_issue_ids: list[str] = Field(..., min_length=1)
    dashboard_base_url: str = Field("", description="Base URL for dashboard links")


class IssueResponse(BaseModel):
    """Issue response model."""

    id: str
    key: str
    title: str
    description: str
    status: str
    priority: str
    issue_type: str
    assignee: str
    reporter: str
    labels: list[str]
    created_at: Optional[str]
    updated_at: Optional[str]
    url: str
    codescope_issue_ids: list[str]


# ── Provider Endpoints ─────────────────────────────────────────────────


@router.get("/issue-trackers/providers")
async def list_providers():
    """List available issue tracker provider types."""
    return {
        "providers": [
            {
                "name": "jira",
                "display_name": "Jira",
                "description": "Atlassian Jira Cloud or Server",
                "required_fields": ["base_url", "project_key", "api_token"],
                "optional_fields": ["username", "default_issue_type", "default_labels"],
            },
            {
                "name": "azure_boards",
                "display_name": "Azure Boards",
                "description": "Azure DevOps Boards",
                "required_fields": ["organization", "project_key", "api_token"],
                "optional_fields": ["base_url", "default_issue_type", "default_labels"],
            },
        ]
    }


# ── Configuration Endpoints ─────────────────────────────────────────────


@router.get("/issue-trackers/configs")
async def list_configs():
    """List all configured issue tracker connections."""
    configs = []
    for config_id, config_data in _tracker_configs.items():
        config = TrackerConfig(**config_data)
        tracker = create_tracker_from_config(config)

        is_connected = False
        connection_message = "Not tested"
        if tracker:
            is_connected, connection_message = tracker.test_connection()

        configs.append(
            TrackerConfigResponse(
                id=config_id,
                provider=config.provider,
                base_url=config.base_url,
                project_key=config.project_key,
                username=config.username,
                organization=config.organization,
                default_issue_type=config.default_issue_type,
                default_labels=config.default_labels,
                is_connected=is_connected,
                connection_message=connection_message,
            )
        )

    # Also check environment-configured providers
    env_providers = get_configured_trackers()
    for provider in env_providers:
        if provider.name not in [c.provider for c in configs]:
            is_connected, connection_message = provider.test_connection()
            configs.append(
                TrackerConfigResponse(
                    id=f"env_{provider.name}",
                    provider=provider.name,
                    base_url=provider.config.base_url,
                    project_key=provider.config.project_key,
                    username=provider.config.username,
                    organization=provider.config.organization,
                    default_issue_type=provider.config.default_issue_type,
                    default_labels=provider.config.default_labels,
                    is_connected=is_connected,
                    connection_message=connection_message,
                )
            )

    return {"configs": configs}


@router.post("/issue-trackers/configs")
async def create_config(req: TrackerConfigCreate):
    """Create a new issue tracker configuration."""
    # Validate provider
    if req.provider not in ["jira", "azure_boards"]:
        raise HTTPException(400, f"Unknown provider: {req.provider}")

    # Validate required fields
    if req.provider == "jira" and not req.base_url:
        raise HTTPException(400, "base_url is required for Jira")
    if req.provider == "azure_boards" and not req.organization:
        raise HTTPException(400, "organization is required for Azure Boards")

    # Create config
    config = TrackerConfig(
        provider=req.provider,
        base_url=req.base_url,
        project_key=req.project_key,
        api_token=req.api_token,
        username=req.username,
        organization=req.organization,
        default_issue_type=req.default_issue_type,
        default_labels=req.default_labels,
    )

    # Test connection
    tracker = create_tracker_from_config(config)
    if not tracker:
        raise HTTPException(400, "Failed to create tracker client")

    is_connected, message = tracker.test_connection()
    if not is_connected:
        raise HTTPException(400, f"Connection test failed: {message}")

    # Store config
    config_id = f"{req.provider}_{req.project_key}"
    _tracker_configs[config_id] = {
        "provider": req.provider,
        "base_url": req.base_url,
        "project_key": req.project_key,
        "api_token": req.api_token,
        "username": req.username,
        "organization": req.organization,
        "default_issue_type": req.default_issue_type,
        "default_labels": req.default_labels,
    }

    clear_cache()

    return {
        "id": config_id,
        "message": message,
        "is_connected": True,
    }


@router.delete("/issue-trackers/configs/{config_id}")
async def delete_config(config_id: str):
    """Delete an issue tracker configuration."""
    if config_id.startswith("env_"):
        raise HTTPException(400, "Cannot delete environment-configured providers")

    if config_id not in _tracker_configs:
        raise HTTPException(404, "Configuration not found")

    del _tracker_configs[config_id]
    clear_cache()

    return {"message": "Configuration deleted"}


@router.post("/issue-trackers/configs/{config_id}/test")
async def test_connection(config_id: str):
    """Test connection to an issue tracker."""
    tracker = _get_tracker(config_id)
    is_connected, message = tracker.test_connection()

    return {
        "is_connected": is_connected,
        "message": message,
    }


# ── Issue Management Endpoints ─────────────────────────────────────────


@router.get("/issue-trackers/{config_id}/issues")
async def search_issues(
    config_id: str,
    query: Optional[str] = None,
    status: Optional[str] = None,
    labels: Optional[str] = None,
    max_results: int = 50,
):
    """Search for issues in the tracker."""
    tracker = _get_tracker(config_id)

    # Parse status
    issue_status = None
    if status:
        try:
            issue_status = IssueStatus(status.lower())
        except ValueError:
            raise HTTPException(400, f"Invalid status: {status}")

    # Parse labels
    label_list = None
    if labels:
        label_list = [l.strip() for l in labels.split(",") if l.strip()]

    issues = tracker.search_issues(
        query=query,
        status=issue_status,
        labels=label_list,
        max_results=min(max_results, 100),
    )

    return {"issues": [_format_issue_response(i) for i in issues]}


@router.post("/issue-trackers/{config_id}/issues")
async def create_issue(config_id: str, req: CreateIssueBody):
    """Create a new issue in the tracker."""
    tracker = _get_tracker(config_id)

    # Parse priority
    try:
        priority = IssuePriority(req.priority.lower())
    except ValueError:
        priority = IssuePriority.MEDIUM

    create_req = CreateIssueRequest(
        title=req.title,
        description=req.description,
        priority=priority,
        issue_type=req.issue_type,
        assignee=req.assignee,
        labels=req.labels,
        codescope_issue_ids=req.codescope_issue_ids,
    )

    issue = tracker.create_issue(create_req)
    if not issue:
        raise HTTPException(500, "Failed to create issue")

    return {"issue": _format_issue_response(issue)}


@router.get("/issue-trackers/{config_id}/issues/{issue_key}")
async def get_issue(config_id: str, issue_key: str):
    """Get an issue by its key."""
    tracker = _get_tracker(config_id)

    issue = tracker.get_issue(issue_key)
    if not issue:
        raise HTTPException(404, f"Issue not found: {issue_key}")

    return {"issue": _format_issue_response(issue)}


@router.put("/issue-trackers/{config_id}/issues/{issue_key}")
async def update_issue(config_id: str, issue_key: str, req: UpdateIssueBody):
    """Update an existing issue."""
    tracker = _get_tracker(config_id)

    # Parse status
    issue_status = None
    if req.status:
        try:
            issue_status = IssueStatus(req.status.lower())
        except ValueError:
            raise HTTPException(400, f"Invalid status: {req.status}")

    # Parse priority
    priority = None
    if req.priority:
        try:
            priority = IssuePriority(req.priority.lower())
        except ValueError:
            raise HTTPException(400, f"Invalid priority: {req.priority}")

    update_req = UpdateIssueRequest(
        title=req.title,
        description=req.description,
        status=issue_status,
        priority=priority,
        assignee=req.assignee,
        labels=req.labels,
    )

    issue = tracker.update_issue(issue_key, update_req)
    if not issue:
        raise HTTPException(500, "Failed to update issue")

    return {"issue": _format_issue_response(issue)}


@router.post("/issue-trackers/{config_id}/issues/{issue_key}/comments")
async def add_comment(config_id: str, issue_key: str, req: AddCommentBody):
    """Add a comment to an issue."""
    tracker = _get_tracker(config_id)

    success = tracker.add_comment(issue_key, req.comment)
    if not success:
        raise HTTPException(500, "Failed to add comment")

    return {"message": "Comment added"}


# ── Metadata Endpoints ─────────────────────────────────────────────────


@router.get("/issue-trackers/{config_id}/issue-types")
async def get_issue_types(config_id: str):
    """Get available issue types for the project."""
    tracker = _get_tracker(config_id)
    return {"issue_types": tracker.get_available_issue_types()}


@router.get("/issue-trackers/{config_id}/priorities")
async def get_priorities(config_id: str):
    """Get available priority levels."""
    tracker = _get_tracker(config_id)
    return {"priorities": tracker.get_available_priorities()}


@router.get("/issue-trackers/{config_id}/users")
async def get_users(config_id: str):
    """Get users that can be assigned to issues."""
    tracker = _get_tracker(config_id)
    return {"users": tracker.get_project_users()}


# ── Bulk Operations ─────────────────────────────────────────────────────


@router.post("/issue-trackers/{config_id}/bulk-create")
async def bulk_create_issues(config_id: str, req: BulkCreateRequest):
    """Create issues from CodeScope analysis issues."""
    tracker = _get_tracker(config_id)

    # This would integrate with the issues store in production
    # For now, return info about what would be created
    return {
        "message": f"Would create {len(req.codescope_issue_ids)} issues",
        "codescope_issue_ids": req.codescope_issue_ids,
        "tracker": tracker.name,
        "project": tracker.config.project_key,
    }


# ── Helper Functions ───────────────────────────────────────────────────


def _get_tracker(config_id: str):
    """Get tracker instance by config ID."""
    # Check stored configs first
    if config_id in _tracker_configs:
        config = TrackerConfig(**_tracker_configs[config_id])
        tracker = create_tracker_from_config(config)
        if tracker:
            return tracker
        raise HTTPException(500, "Failed to create tracker client")

    # Check environment-configured providers
    if config_id.startswith("env_"):
        provider_name = config_id[4:]
        tracker = get_issue_tracker(provider_name)
        if tracker:
            return tracker

    raise HTTPException(404, f"Tracker configuration not found: {config_id}")


def _format_issue_response(issue: TrackerIssue) -> dict:
    """Format TrackerIssue for API response."""
    return {
        "id": issue.id,
        "key": issue.key,
        "title": issue.title,
        "description": issue.description[:1000] + "..." if len(issue.description) > 1000 else issue.description,
        "status": issue.status.value,
        "priority": issue.priority.value,
        "issue_type": issue.issue_type,
        "assignee": issue.assignee,
        "reporter": issue.reporter,
        "labels": issue.labels,
        "created_at": issue.created_at.isoformat() if issue.created_at else None,
        "updated_at": issue.updated_at.isoformat() if issue.updated_at else None,
        "url": issue.url,
        "codescope_issue_ids": issue.codescope_issue_ids,
    }
