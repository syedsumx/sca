"""Team and organization management endpoints."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

# In-memory stores
_organizations: list[dict] = []
_teams: list[dict] = []
_memberships: list[dict] = []  # {team_id, user_id, role}
_project_assignments: list[dict] = []  # {project_id, team_id}


class OrgCreate(BaseModel):
    name: str
    description: str = ""
    plan: str = "free"  # free, pro, enterprise


class OrgUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    plan: Optional[str] = None


class TeamCreate(BaseModel):
    name: str
    org_id: int
    description: str = ""


class TeamUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class MembershipCreate(BaseModel):
    user_id: str
    role: str = "member"  # owner, admin, member, viewer


class ProjectAssign(BaseModel):
    project_id: str


# ── Organizations ────────────────────────────────────────────────────

@router.get("/orgs")
async def list_organizations():
    """List all organizations."""
    return [{"id": i, **o} for i, o in enumerate(_organizations)]


@router.post("/orgs")
async def create_organization(req: OrgCreate):
    """Create a new organization."""
    org = req.dict()
    _organizations.append(org)
    return {"id": len(_organizations) - 1, **org}


@router.get("/orgs/{org_id}")
async def get_organization(org_id: int):
    """Get organization details."""
    if org_id < 0 or org_id >= len(_organizations):
        raise HTTPException(404, "Organization not found")
    org = _organizations[org_id]
    teams = [{"id": i, **t} for i, t in enumerate(_teams) if t.get("org_id") == org_id]
    return {"id": org_id, **org, "teams": teams}


@router.put("/orgs/{org_id}")
async def update_organization(org_id: int, req: OrgUpdate):
    """Update organization details."""
    if org_id < 0 or org_id >= len(_organizations):
        raise HTTPException(404, "Organization not found")
    for k, v in req.dict(exclude_none=True).items():
        _organizations[org_id][k] = v
    return {"id": org_id, **_organizations[org_id]}


@router.delete("/orgs/{org_id}")
async def delete_organization(org_id: int):
    """Delete an organization."""
    if org_id < 0 or org_id >= len(_organizations):
        raise HTTPException(404, "Organization not found")
    _organizations[org_id] = {"_deleted": True}
    return {"message": "Organization deleted"}


# ── Teams ────────────────────────────────────────────────────────────

@router.get("/orgs/{org_id}/teams")
async def list_teams(org_id: int):
    """List teams in an organization."""
    return [{"id": i, **t} for i, t in enumerate(_teams) if t.get("org_id") == org_id]


@router.post("/orgs/{org_id}/teams")
async def create_team(org_id: int, req: TeamCreate):
    """Create a new team."""
    if org_id < 0 or org_id >= len(_organizations):
        raise HTTPException(404, "Organization not found")
    team = {"name": req.name, "org_id": org_id, "description": req.description}
    _teams.append(team)
    return {"id": len(_teams) - 1, **team}


@router.get("/teams/{team_id}")
async def get_team(team_id: int):
    """Get team details with members and assigned projects."""
    if team_id < 0 or team_id >= len(_teams):
        raise HTTPException(404, "Team not found")
    team = _teams[team_id]
    members = [m for m in _memberships if m.get("team_id") == team_id]
    projects = [a["project_id"] for a in _project_assignments if a.get("team_id") == team_id]
    return {"id": team_id, **team, "members": members, "projects": projects}


@router.put("/teams/{team_id}")
async def update_team(team_id: int, req: TeamUpdate):
    """Update team details."""
    if team_id < 0 or team_id >= len(_teams):
        raise HTTPException(404, "Team not found")
    for k, v in req.dict(exclude_none=True).items():
        _teams[team_id][k] = v
    return {"id": team_id, **_teams[team_id]}


@router.delete("/teams/{team_id}")
async def delete_team(team_id: int):
    """Delete a team."""
    if team_id < 0 or team_id >= len(_teams):
        raise HTTPException(404, "Team not found")
    _teams[team_id] = {"_deleted": True}
    return {"message": "Team deleted"}


# ── Memberships ──────────────────────────────────────────────────────

@router.post("/teams/{team_id}/members")
async def add_team_member(team_id: int, req: MembershipCreate):
    """Add a user to a team."""
    if team_id < 0 or team_id >= len(_teams):
        raise HTTPException(404, "Team not found")
    for m in _memberships:
        if m["team_id"] == team_id and m["user_id"] == req.user_id:
            raise HTTPException(409, "User is already a member of this team")
    membership = {"team_id": team_id, "user_id": req.user_id, "role": req.role}
    _memberships.append(membership)
    return membership


@router.get("/teams/{team_id}/members")
async def list_team_members(team_id: int):
    """List members of a team."""
    return [m for m in _memberships if m.get("team_id") == team_id]


@router.delete("/teams/{team_id}/members/{user_id}")
async def remove_team_member(team_id: int, user_id: str):
    """Remove a user from a team."""
    for i, m in enumerate(_memberships):
        if m["team_id"] == team_id and m["user_id"] == user_id:
            _memberships.pop(i)
            return {"message": "Member removed"}
    raise HTTPException(404, "Membership not found")


# ── Project assignments ──────────────────────────────────────────────

@router.post("/teams/{team_id}/projects")
async def assign_project(team_id: int, req: ProjectAssign):
    """Assign a project to a team."""
    if team_id < 0 or team_id >= len(_teams):
        raise HTTPException(404, "Team not found")
    for a in _project_assignments:
        if a["team_id"] == team_id and a["project_id"] == req.project_id:
            raise HTTPException(409, "Project already assigned to this team")
    assignment = {"team_id": team_id, "project_id": req.project_id}
    _project_assignments.append(assignment)
    return assignment


@router.get("/teams/{team_id}/projects")
async def list_team_projects(team_id: int):
    """List projects assigned to a team."""
    return [a["project_id"] for a in _project_assignments if a.get("team_id") == team_id]


@router.delete("/teams/{team_id}/projects/{project_id}")
async def unassign_project(team_id: int, project_id: str):
    """Remove a project from a team."""
    for i, a in enumerate(_project_assignments):
        if a["team_id"] == team_id and a["project_id"] == project_id:
            _project_assignments.pop(i)
            return {"message": "Project unassigned"}
    raise HTTPException(404, "Assignment not found")
