"""Issue tracker provider registry."""

from __future__ import annotations

import logging
import os
from typing import Optional, Type

from codescope.integrations.issue_trackers.base import (
    IssueTrackerProvider,
    TrackerConfig,
)

logger = logging.getLogger(__name__)

# Registry of available providers
_PROVIDER_CLASSES: dict[str, Type[IssueTrackerProvider]] = {}

# Configured provider instances (cached)
_CONFIGURED_PROVIDERS: dict[str, IssueTrackerProvider] = {}


def register_tracker(name: str, provider_class: Type[IssueTrackerProvider]) -> None:
    """Register an issue tracker provider class.

    Args:
        name: Provider name (e.g., 'jira', 'azure_boards')
        provider_class: Provider class to register
    """
    _PROVIDER_CLASSES[name] = provider_class
    logger.debug("Registered issue tracker provider: %s", name)


def get_issue_tracker(name: str) -> Optional[IssueTrackerProvider]:
    """Get a configured issue tracker provider by name.

    The provider is configured from environment variables:
    - CODESCOPE_<NAME>_URL: Base URL
    - CODESCOPE_<NAME>_TOKEN: API token
    - CODESCOPE_<NAME>_PROJECT: Project key
    - CODESCOPE_<NAME>_USERNAME: Username (for Jira)
    - CODESCOPE_<NAME>_ORGANIZATION: Organization (for Azure DevOps)

    Args:
        name: Provider name

    Returns:
        Configured provider instance or None
    """
    if name in _CONFIGURED_PROVIDERS:
        return _CONFIGURED_PROVIDERS[name]

    if name not in _PROVIDER_CLASSES:
        logger.warning("Unknown issue tracker provider: %s", name)
        return None

    # Build config from environment
    env_prefix = f"CODESCOPE_{name.upper()}"
    config = TrackerConfig(
        provider=name,
        base_url=os.environ.get(f"{env_prefix}_URL", ""),
        project_key=os.environ.get(f"{env_prefix}_PROJECT", ""),
        api_token=os.environ.get(f"{env_prefix}_TOKEN", ""),
        username=os.environ.get(f"{env_prefix}_USERNAME", ""),
        organization=os.environ.get(f"{env_prefix}_ORGANIZATION", ""),
        default_issue_type=os.environ.get(f"{env_prefix}_ISSUE_TYPE", "Bug"),
        default_labels=os.environ.get(f"{env_prefix}_LABELS", "").split(",")
        if os.environ.get(f"{env_prefix}_LABELS")
        else [],
    )

    # Validate required fields
    if not config.api_token:
        logger.debug("Issue tracker %s not configured: missing API token", name)
        return None

    if name == "jira" and not config.base_url:
        logger.debug("Jira not configured: missing base URL")
        return None

    if name == "azure_boards" and not config.organization:
        logger.debug("Azure Boards not configured: missing organization")
        return None

    if name == "github" and not config.project_key:
        # GitHub uses project_key as "owner/repo"
        logger.debug("GitHub not configured: missing repository (owner/repo)")
        return None

    if not config.project_key:
        logger.debug("Issue tracker %s not configured: missing project key", name)
        return None

    # Create and cache provider
    provider_class = _PROVIDER_CLASSES[name]
    provider = provider_class(config)
    _CONFIGURED_PROVIDERS[name] = provider

    return provider


def get_configured_trackers() -> list[IssueTrackerProvider]:
    """Get all configured issue tracker providers.

    Returns:
        List of configured providers
    """
    providers = []
    for name in _PROVIDER_CLASSES:
        provider = get_issue_tracker(name)
        if provider:
            providers.append(provider)
    return providers


def create_tracker_from_config(config: TrackerConfig) -> Optional[IssueTrackerProvider]:
    """Create an issue tracker provider from a config object.

    Args:
        config: Tracker configuration

    Returns:
        Provider instance or None if provider type unknown
    """
    if config.provider not in _PROVIDER_CLASSES:
        logger.error("Unknown issue tracker provider: %s", config.provider)
        return None

    provider_class = _PROVIDER_CLASSES[config.provider]
    return provider_class(config)


def get_available_providers() -> list[dict[str, str]]:
    """Get list of available provider types.

    Returns:
        List of provider info dicts
    """
    return [
        {"name": name, "display_name": cls.__name__.replace("Client", "")}
        for name, cls in _PROVIDER_CLASSES.items()
    ]


def clear_cache() -> None:
    """Clear the cached provider instances."""
    _CONFIGURED_PROVIDERS.clear()


# Auto-register built-in providers
def _register_builtin_providers() -> None:
    """Register built-in issue tracker providers."""
    from codescope.integrations.issue_trackers.jira import JiraClient
    from codescope.integrations.issue_trackers.azure_boards import AzureBoardsClient
    from codescope.integrations.issue_trackers.github import GitHubIssuesClient

    register_tracker("jira", JiraClient)
    register_tracker("azure_boards", AzureBoardsClient)
    register_tracker("github", GitHubIssuesClient)


_register_builtin_providers()
