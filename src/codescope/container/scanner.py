"""Container image scanning engine for Dockerfiles and docker-compose files."""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------------

class FindingSeverity(enum.Enum):
    """Severity levels for container findings."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


# ---------------------------------------------------------------------------
# Finding / Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ContainerFinding:
    """A single finding from container scanning."""

    rule_id: str
    title: str
    description: str
    severity: FindingSeverity
    file_path: str
    line_number: int
    remediation: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize finding to a dictionary."""
        return {
            "rule_id": self.rule_id,
            "title": self.title,
            "description": self.description,
            "severity": self.severity.value,
            "file_path": self.file_path,
            "line_number": self.line_number,
            "remediation": self.remediation,
        }


@dataclass
class ContainerScanResult:
    """Aggregated results from a container scan."""

    findings: list[ContainerFinding] = field(default_factory=list)
    files_scanned: int = 0
    dockerfiles_found: list[str] = field(default_factory=list)
    compose_files_found: list[str] = field(default_factory=list)

    # -- severity counts --------------------------------------------------

    @property
    def critical_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == FindingSeverity.CRITICAL)

    @property
    def high_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == FindingSeverity.HIGH)

    @property
    def medium_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == FindingSeverity.MEDIUM)

    @property
    def low_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == FindingSeverity.LOW)

    @property
    def info_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == FindingSeverity.INFO)

    @property
    def is_clean(self) -> bool:
        return len(self.findings) == 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize scan result to a dictionary."""
        return {
            "findings": [f.to_dict() for f in self.findings],
            "files_scanned": self.files_scanned,
            "dockerfiles_found": self.dockerfiles_found,
            "compose_files_found": self.compose_files_found,
            "summary": {
                "total": len(self.findings),
                "critical": self.critical_count,
                "high": self.high_count,
                "medium": self.medium_count,
                "low": self.low_count,
                "info": self.info_count,
                "is_clean": self.is_clean,
            },
        }


# ---------------------------------------------------------------------------
# Dockerfile parser helpers
# ---------------------------------------------------------------------------

_SECRET_KEYWORDS = re.compile(
    r"(PASSWORD|SECRET|API_KEY|TOKEN|PRIVATE_KEY|AWS_SECRET)",
    re.IGNORECASE,
)

_SHELL_PIPE_RE = re.compile(
    r"(curl\b.*\|\s*sh|curl\b.*\|\s*bash|wget\b.*\|\s*sh|wget\b.*\|\s*bash"
    r"|curl\b.*\|\s*/bin/sh|curl\b.*\|\s*/bin/bash"
    r"|wget\s+-O\s*-\s*.*\|\s*sh|wget\s+-O\s*-\s*.*\|\s*bash)",
    re.IGNORECASE,
)


def _parse_dockerfile(text: str) -> list[dict[str, Any]]:
    """Parse a Dockerfile into a list of logical instructions.

    Each entry is a dict with keys:
        instruction  – the uppercase instruction keyword (FROM, RUN, ...)
        arguments    – the rest of the line (with continuations joined)
        line_number  – the *first* line number (1-based) of the instruction
    """
    instructions: list[dict[str, Any]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()

        # Skip empty lines and comments
        if not stripped or stripped.startswith("#"):
            i += 1
            continue

        # Handle line continuations (backslash at end)
        start_line = i + 1  # 1-based
        full_line = stripped
        while full_line.endswith("\\") and i + 1 < len(lines):
            full_line = full_line[:-1].rstrip()
            i += 1
            continuation = lines[i].strip()
            # skip comment-only continuation lines
            if continuation.startswith("#"):
                continue
            full_line = full_line + " " + continuation

        # Split into instruction and arguments
        parts = full_line.split(None, 1)
        if parts:
            instruction = parts[0].upper()
            arguments = parts[1] if len(parts) > 1 else ""
            instructions.append(
                {
                    "instruction": instruction,
                    "arguments": arguments,
                    "line_number": start_line,
                }
            )
        i += 1

    return instructions


# ---------------------------------------------------------------------------
# DockerfileLint
# ---------------------------------------------------------------------------

class DockerfileLint:
    """Collection of Dockerfile linting rules.

    Each ``check_*`` method receives the parsed instructions and the file
    path, and returns a (possibly empty) list of :class:`ContainerFinding`.
    """

    # -- DL0001  Running as root -------------------------------------------

    @staticmethod
    def check_running_as_root(
        instructions: list[dict[str, Any]], file_path: str,
    ) -> list[ContainerFinding]:
        """DL0001: Detect missing USER instruction or only USER root."""
        user_instructions = [
            inst for inst in instructions if inst["instruction"] == "USER"
        ]
        if not user_instructions:
            # Report on the first FROM line (or line 1 if no FROM)
            first_from = next(
                (i for i in instructions if i["instruction"] == "FROM"), None,
            )
            line = first_from["line_number"] if first_from else 1
            return [
                ContainerFinding(
                    rule_id="DL0001",
                    title="Container running as root",
                    description=(
                        "No USER instruction found. The container will run as "
                        "root by default, which is a security risk."
                    ),
                    severity=FindingSeverity.HIGH,
                    file_path=file_path,
                    line_number=line,
                    remediation=(
                        "Add a USER instruction to run the container as a "
                        "non-root user: USER appuser"
                    ),
                )
            ]

        # Check if the *last* USER instruction is 'root'
        last_user = user_instructions[-1]
        if last_user["arguments"].strip().lower() == "root":
            return [
                ContainerFinding(
                    rule_id="DL0001",
                    title="Container running as root",
                    description=(
                        "The last USER instruction is set to 'root'. The "
                        "container will run as root, which is a security risk."
                    ),
                    severity=FindingSeverity.HIGH,
                    file_path=file_path,
                    line_number=last_user["line_number"],
                    remediation=(
                        "Change the USER instruction to a non-root user: "
                        "USER appuser"
                    ),
                )
            ]
        return []

    # -- DL0002  Using latest tag ------------------------------------------

    @staticmethod
    def check_latest_tag(
        instructions: list[dict[str, Any]], file_path: str,
    ) -> list[ContainerFinding]:
        """DL0002: Detect FROM image:latest or FROM image (no tag)."""
        findings: list[ContainerFinding] = []
        for inst in instructions:
            if inst["instruction"] != "FROM":
                continue
            image = inst["arguments"].split()[0] if inst["arguments"] else ""
            # Strip --platform=... prefix
            if image.startswith("--"):
                parts = inst["arguments"].split()
                image = parts[1] if len(parts) > 1 else ""
            # Skip scratch
            if image.lower() == "scratch":
                continue
            # Skip build-stage references (FROM stage AS ...)
            # but we still want to check the image name
            # Remove AS alias
            image = image.split()[0] if image else ""
            if ":" not in image and "@" not in image:
                findings.append(
                    ContainerFinding(
                        rule_id="DL0002",
                        title="Using latest tag",
                        description=(
                            f"Image '{image}' does not have an explicit tag "
                            f"and will default to 'latest'."
                        ),
                        severity=FindingSeverity.MEDIUM,
                        file_path=file_path,
                        line_number=inst["line_number"],
                        remediation=(
                            "Pin the image to a specific version tag, e.g. "
                            f"'{image}:1.0'."
                        ),
                    )
                )
            elif image.endswith(":latest"):
                findings.append(
                    ContainerFinding(
                        rule_id="DL0002",
                        title="Using latest tag",
                        description=(
                            f"Image '{image}' uses the 'latest' tag which "
                            f"is mutable and non-reproducible."
                        ),
                        severity=FindingSeverity.MEDIUM,
                        file_path=file_path,
                        line_number=inst["line_number"],
                        remediation=(
                            "Pin the image to a specific version tag, e.g. "
                            f"'{image.rsplit(':', 1)[0]}:1.0'."
                        ),
                    )
                )
        return findings

    # -- DL0003  Secrets in ENV --------------------------------------------

    @staticmethod
    def check_secrets_in_env(
        instructions: list[dict[str, Any]], file_path: str,
    ) -> list[ContainerFinding]:
        """DL0003: Detect ENV instructions with secret-like keywords."""
        findings: list[ContainerFinding] = []
        for inst in instructions:
            if inst["instruction"] != "ENV":
                continue
            if _SECRET_KEYWORDS.search(inst["arguments"]):
                findings.append(
                    ContainerFinding(
                        rule_id="DL0003",
                        title="Secrets in ENV instruction",
                        description=(
                            "ENV instruction appears to contain a secret or "
                            "sensitive value. Environment variables are visible "
                            "in image metadata."
                        ),
                        severity=FindingSeverity.CRITICAL,
                        file_path=file_path,
                        line_number=inst["line_number"],
                        remediation=(
                            "Use Docker build secrets (--mount=type=secret) "
                            "or runtime environment variables instead of "
                            "baking secrets into the image."
                        ),
                    )
                )
        return findings

    # -- DL0004  COPY without --chown --------------------------------------

    @staticmethod
    def check_copy_without_chown(
        instructions: list[dict[str, Any]], file_path: str,
    ) -> list[ContainerFinding]:
        """DL0004: COPY/ADD without --chown when USER is set."""
        findings: list[ContainerFinding] = []
        user_set = False
        for inst in instructions:
            if inst["instruction"] == "USER":
                user_val = inst["arguments"].strip().lower()
                user_set = user_val != "root" and user_val != ""
            elif inst["instruction"] in ("COPY", "ADD") and user_set:
                if "--chown" not in inst["arguments"]:
                    findings.append(
                        ContainerFinding(
                            rule_id="DL0004",
                            title=f"{inst['instruction']} without --chown",
                            description=(
                                f"{inst['instruction']} instruction does not "
                                f"use --chown while a non-root USER is set. "
                                f"Files may be owned by root."
                            ),
                            severity=FindingSeverity.LOW,
                            file_path=file_path,
                            line_number=inst["line_number"],
                            remediation=(
                                f"Add --chown=<user>:<group> to the "
                                f"{inst['instruction']} instruction."
                            ),
                        )
                    )
        return findings

    # -- DL0005  Using ADD instead of COPY ---------------------------------

    @staticmethod
    def check_add_instead_of_copy(
        instructions: list[dict[str, Any]], file_path: str,
    ) -> list[ContainerFinding]:
        """DL0005: ADD for local files (not URLs/tarballs)."""
        findings: list[ContainerFinding] = []
        for inst in instructions:
            if inst["instruction"] != "ADD":
                continue
            args = inst["arguments"]
            # Remove flags like --chown=...
            cleaned = re.sub(r"--\S+\s*", "", args).strip()
            parts = cleaned.split()
            if not parts:
                continue
            src = parts[0]
            # ADD is fine for URLs and compressed archives
            if src.startswith("http://") or src.startswith("https://"):
                continue
            if re.search(r"\.(tar|tar\.gz|tgz|tar\.bz2|tar\.xz|zip)$", src, re.IGNORECASE):
                continue
            findings.append(
                ContainerFinding(
                    rule_id="DL0005",
                    title="Using ADD instead of COPY",
                    description=(
                        "ADD instruction used for local file copy. COPY is "
                        "preferred as it is more transparent."
                    ),
                    severity=FindingSeverity.LOW,
                    file_path=file_path,
                    line_number=inst["line_number"],
                    remediation="Use COPY instead of ADD for local files.",
                )
            )
        return findings

    # -- DL0006  apt-get without --no-install-recommends -------------------

    @staticmethod
    def check_apt_no_install_recommends(
        instructions: list[dict[str, Any]], file_path: str,
    ) -> list[ContainerFinding]:
        """DL0006: apt-get install without --no-install-recommends."""
        findings: list[ContainerFinding] = []
        for inst in instructions:
            if inst["instruction"] != "RUN":
                continue
            args = inst["arguments"]
            if "apt-get" in args and "install" in args:
                if "--no-install-recommends" not in args:
                    findings.append(
                        ContainerFinding(
                            rule_id="DL0006",
                            title="apt-get without --no-install-recommends",
                            description=(
                                "apt-get install is used without "
                                "--no-install-recommends, which may install "
                                "unnecessary packages and increase image size."
                            ),
                            severity=FindingSeverity.LOW,
                            file_path=file_path,
                            line_number=inst["line_number"],
                            remediation=(
                                "Add --no-install-recommends to apt-get "
                                "install commands."
                            ),
                        )
                    )
        return findings

    # -- DL0007  Not pinning package versions ------------------------------

    @staticmethod
    def check_unpinned_packages(
        instructions: list[dict[str, Any]], file_path: str,
    ) -> list[ContainerFinding]:
        """DL0007: apt-get install or apk add without version pinning."""
        findings: list[ContainerFinding] = []
        for inst in instructions:
            if inst["instruction"] != "RUN":
                continue
            args = inst["arguments"]

            # apt-get install check
            apt_match = re.search(
                r"apt-get\s+install\s+(.*?)(?:&&|;|\||$)",
                args,
                re.IGNORECASE | re.DOTALL,
            )
            if apt_match:
                pkg_str = apt_match.group(1)
                # Remove flags
                tokens = [
                    t for t in pkg_str.split()
                    if not t.startswith("-") and t not in ("&&", ";", "|")
                ]
                unpinned = [t for t in tokens if "=" not in t and t]
                if unpinned:
                    findings.append(
                        ContainerFinding(
                            rule_id="DL0007",
                            title="Unpinned package versions",
                            description=(
                                f"Packages installed without version pinning: "
                                f"{', '.join(unpinned[:5])}"
                                f"{'...' if len(unpinned) > 5 else ''}."
                            ),
                            severity=FindingSeverity.MEDIUM,
                            file_path=file_path,
                            line_number=inst["line_number"],
                            remediation=(
                                "Pin package versions, e.g. "
                                "'apt-get install pkg=1.0.0'."
                            ),
                        )
                    )

            # apk add check
            apk_match = re.search(
                r"apk\s+add\s+(.*?)(?:&&|;|\||$)",
                args,
                re.IGNORECASE | re.DOTALL,
            )
            if apk_match:
                pkg_str = apk_match.group(1)
                tokens = [
                    t for t in pkg_str.split()
                    if not t.startswith("-") and t not in ("&&", ";", "|")
                ]
                unpinned = [
                    t for t in tokens
                    if "=" not in t and t
                ]
                if unpinned:
                    findings.append(
                        ContainerFinding(
                            rule_id="DL0007",
                            title="Unpinned package versions",
                            description=(
                                f"Packages installed without version pinning: "
                                f"{', '.join(unpinned[:5])}"
                                f"{'...' if len(unpinned) > 5 else ''}."
                            ),
                            severity=FindingSeverity.MEDIUM,
                            file_path=file_path,
                            line_number=inst["line_number"],
                            remediation=(
                                "Pin package versions, e.g. "
                                "'apk add pkg=1.0.0'."
                            ),
                        )
                    )
        return findings

    # -- DL0008  Exposing SSH port -----------------------------------------

    @staticmethod
    def check_expose_ssh(
        instructions: list[dict[str, Any]], file_path: str,
    ) -> list[ContainerFinding]:
        """DL0008: Detect EXPOSE 22."""
        findings: list[ContainerFinding] = []
        for inst in instructions:
            if inst["instruction"] != "EXPOSE":
                continue
            ports = inst["arguments"].split()
            for port in ports:
                # port might be "22/tcp", "22"
                port_num = port.split("/")[0].strip()
                if port_num == "22":
                    findings.append(
                        ContainerFinding(
                            rule_id="DL0008",
                            title="SSH port exposed",
                            description=(
                                "Port 22 (SSH) is exposed. Running SSH in "
                                "a container is generally an anti-pattern."
                            ),
                            severity=FindingSeverity.HIGH,
                            file_path=file_path,
                            line_number=inst["line_number"],
                            remediation=(
                                "Remove EXPOSE 22. Use 'docker exec' to "
                                "access the container instead of SSH."
                            ),
                        )
                    )
        return findings

    # -- DL0009  Using sudo ------------------------------------------------

    @staticmethod
    def check_sudo_usage(
        instructions: list[dict[str, Any]], file_path: str,
    ) -> list[ContainerFinding]:
        """DL0009: Detect sudo in RUN instructions."""
        findings: list[ContainerFinding] = []
        for inst in instructions:
            if inst["instruction"] != "RUN":
                continue
            # Match 'sudo' as a standalone command (word boundary)
            if re.search(r"\bsudo\b", inst["arguments"]):
                findings.append(
                    ContainerFinding(
                        rule_id="DL0009",
                        title="Using sudo in RUN instruction",
                        description=(
                            "sudo is used inside a RUN instruction. This is "
                            "unnecessary when building as root and indicates "
                            "a potential permission misconfiguration."
                        ),
                        severity=FindingSeverity.MEDIUM,
                        file_path=file_path,
                        line_number=inst["line_number"],
                        remediation=(
                            "Remove sudo from RUN instructions. Use USER to "
                            "switch users instead."
                        ),
                    )
                )
        return findings

    # -- DL0010  Missing HEALTHCHECK ---------------------------------------

    @staticmethod
    def check_healthcheck_missing(
        instructions: list[dict[str, Any]], file_path: str,
    ) -> list[ContainerFinding]:
        """DL0010: No HEALTHCHECK instruction found."""
        has_healthcheck = any(
            i["instruction"] == "HEALTHCHECK" for i in instructions
        )
        if not has_healthcheck:
            last_line = instructions[-1]["line_number"] if instructions else 1
            return [
                ContainerFinding(
                    rule_id="DL0010",
                    title="Missing HEALTHCHECK",
                    description=(
                        "No HEALTHCHECK instruction found. A health check "
                        "helps Docker determine if the container is healthy."
                    ),
                    severity=FindingSeverity.INFO,
                    file_path=file_path,
                    line_number=last_line,
                    remediation=(
                        "Add a HEALTHCHECK instruction, e.g. "
                        "HEALTHCHECK CMD curl -f http://localhost/ || exit 1"
                    ),
                )
            ]
        return []

    # -- DL0011  Multiple FROM without AS ----------------------------------

    @staticmethod
    def check_multiple_from_no_alias(
        instructions: list[dict[str, Any]], file_path: str,
    ) -> list[ContainerFinding]:
        """DL0011: Multiple FROM without build stage aliases."""
        from_instructions = [
            i for i in instructions if i["instruction"] == "FROM"
        ]
        if len(from_instructions) <= 1:
            return []

        # Check if all FROM instructions (except possibly the last) have AS
        without_alias = []
        for inst in from_instructions:
            args_upper = inst["arguments"].upper()
            if " AS " not in args_upper:
                without_alias.append(inst)

        # If there are multiple FROMs and at least one doesn't have AS,
        # flag non-multistage usage
        if len(without_alias) > 1:
            return [
                ContainerFinding(
                    rule_id="DL0011",
                    title="Multiple FROM without build stage aliases",
                    description=(
                        f"Found {len(from_instructions)} FROM instructions "
                        f"but {len(without_alias)} lack an 'AS' alias. "
                        f"Consider using multi-stage builds."
                    ),
                    severity=FindingSeverity.INFO,
                    file_path=file_path,
                    line_number=without_alias[0]["line_number"],
                    remediation=(
                        "Use multi-stage builds with 'FROM image AS stage' "
                        "to reduce final image size."
                    ),
                )
            ]
        return []

    # -- DL0012  Curl/wget piped to shell ----------------------------------

    @staticmethod
    def check_curl_pipe_shell(
        instructions: list[dict[str, Any]], file_path: str,
    ) -> list[ContainerFinding]:
        """DL0012: Detect curl|sh, wget -O-|sh patterns."""
        findings: list[ContainerFinding] = []
        for inst in instructions:
            if inst["instruction"] != "RUN":
                continue
            if _SHELL_PIPE_RE.search(inst["arguments"]):
                findings.append(
                    ContainerFinding(
                        rule_id="DL0012",
                        title="Piping download to shell",
                        description=(
                            "Download output is piped directly to a shell. "
                            "This is dangerous as it executes arbitrary "
                            "remote code without verification."
                        ),
                        severity=FindingSeverity.CRITICAL,
                        file_path=file_path,
                        line_number=inst["line_number"],
                        remediation=(
                            "Download the script first, verify its checksum, "
                            "then execute it in a separate step."
                        ),
                    )
                )
        return findings

    # -- DL0013  Too many exposed ports ------------------------------------

    @staticmethod
    def check_too_many_ports(
        instructions: list[dict[str, Any]], file_path: str,
    ) -> list[ContainerFinding]:
        """DL0013: More than 5 ports exposed."""
        all_ports: list[str] = []
        expose_lines: list[int] = []
        for inst in instructions:
            if inst["instruction"] != "EXPOSE":
                continue
            ports = inst["arguments"].split()
            all_ports.extend(ports)
            expose_lines.append(inst["line_number"])

        if len(all_ports) > 5:
            return [
                ContainerFinding(
                    rule_id="DL0013",
                    title="Too many ports exposed",
                    description=(
                        f"{len(all_ports)} ports are exposed. Exposing too "
                        f"many ports increases the attack surface."
                    ),
                    severity=FindingSeverity.MEDIUM,
                    file_path=file_path,
                    line_number=expose_lines[0] if expose_lines else 1,
                    remediation=(
                        "Only expose the ports that are strictly required "
                        "by the application."
                    ),
                )
            ]
        return []

    # -- DL0014  WORKDIR not absolute path ---------------------------------

    @staticmethod
    def check_workdir_relative(
        instructions: list[dict[str, Any]], file_path: str,
    ) -> list[ContainerFinding]:
        """DL0014: WORKDIR not using absolute path."""
        findings: list[ContainerFinding] = []
        for inst in instructions:
            if inst["instruction"] != "WORKDIR":
                continue
            path_arg = inst["arguments"].strip()
            # Allow variable references like $HOME or ${HOME}
            if path_arg.startswith("/") or path_arg.startswith("$"):
                continue
            findings.append(
                ContainerFinding(
                    rule_id="DL0014",
                    title="WORKDIR uses relative path",
                    description=(
                        f"WORKDIR '{path_arg}' is not an absolute path. "
                        f"Relative paths can cause unexpected behaviour."
                    ),
                    severity=FindingSeverity.MEDIUM,
                    file_path=file_path,
                    line_number=inst["line_number"],
                    remediation="Use an absolute path for WORKDIR, e.g. /app.",
                )
            )
        return findings

    # -- DL0015  Not cleaning apt cache ------------------------------------

    @staticmethod
    def check_apt_cache_cleanup(
        instructions: list[dict[str, Any]], file_path: str,
    ) -> list[ContainerFinding]:
        """DL0015: apt-get install without cleaning cache in same RUN."""
        findings: list[ContainerFinding] = []
        for inst in instructions:
            if inst["instruction"] != "RUN":
                continue
            args = inst["arguments"]
            if "apt-get" in args and "install" in args:
                if "rm -rf /var/lib/apt/lists" not in args:
                    findings.append(
                        ContainerFinding(
                            rule_id="DL0015",
                            title="apt cache not cleaned",
                            description=(
                                "apt-get install is used without cleaning "
                                "the apt cache in the same RUN instruction, "
                                "resulting in a larger image."
                            ),
                            severity=FindingSeverity.LOW,
                            file_path=file_path,
                            line_number=inst["line_number"],
                            remediation=(
                                "Add '&& rm -rf /var/lib/apt/lists/*' at "
                                "the end of the RUN instruction."
                            ),
                        )
                    )
        return findings

    # -- public convenience ------------------------------------------------

    @classmethod
    def all_checks(cls) -> list:
        """Return all check methods."""
        return [
            cls.check_running_as_root,
            cls.check_latest_tag,
            cls.check_secrets_in_env,
            cls.check_copy_without_chown,
            cls.check_add_instead_of_copy,
            cls.check_apt_no_install_recommends,
            cls.check_unpinned_packages,
            cls.check_expose_ssh,
            cls.check_sudo_usage,
            cls.check_healthcheck_missing,
            cls.check_multiple_from_no_alias,
            cls.check_curl_pipe_shell,
            cls.check_too_many_ports,
            cls.check_workdir_relative,
            cls.check_apt_cache_cleanup,
        ]


# ---------------------------------------------------------------------------
# Docker-compose linting helpers
# ---------------------------------------------------------------------------

def _get_services(compose_data: dict[str, Any]) -> dict[str, Any]:
    """Extract services dict from compose data (v2/v3 compatible)."""
    if "services" in compose_data and isinstance(compose_data["services"], dict):
        return compose_data["services"]
    return {}


# ---------------------------------------------------------------------------
# ContainerScanner
# ---------------------------------------------------------------------------

class ContainerScanner:
    """Scan directories for Dockerfiles and docker-compose files."""

    def __init__(self) -> None:
        self._lint = DockerfileLint()

    # -- public API --------------------------------------------------------

    def scan(self, path: Path) -> ContainerScanResult:
        """Scan a directory for container configuration issues.

        Parameters
        ----------
        path:
            A directory (or single file) to scan.

        Returns
        -------
        ContainerScanResult
        """
        path = Path(path)
        result = ContainerScanResult()

        if path.is_file():
            name_lower = path.name.lower()
            if "dockerfile" in name_lower or name_lower.endswith(".dockerfile"):
                result.dockerfiles_found.append(str(path))
                result.findings.extend(self.scan_dockerfile(path))
                result.files_scanned = 1
            elif "compose" in name_lower or "docker-compose" in name_lower:
                result.compose_files_found.append(str(path))
                result.findings.extend(self.scan_compose(path))
                result.files_scanned = 1
            return result

        dockerfiles = self._find_dockerfiles(path)
        compose_files = self._find_compose_files(path)

        result.dockerfiles_found = [str(f) for f in dockerfiles]
        result.compose_files_found = [str(f) for f in compose_files]
        result.files_scanned = len(dockerfiles) + len(compose_files)

        for df in dockerfiles:
            result.findings.extend(self.scan_dockerfile(df))
        for cf in compose_files:
            result.findings.extend(self.scan_compose(cf))

        return result

    def scan_dockerfile(self, file_path: Path) -> list[ContainerFinding]:
        """Lint a single Dockerfile and return findings."""
        file_path = Path(file_path)
        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []

        instructions = _parse_dockerfile(text)
        if not instructions:
            return []

        findings: list[ContainerFinding] = []
        fp = str(file_path)
        for check in DockerfileLint.all_checks():
            findings.extend(check(instructions, fp))
        return findings

    def scan_compose(self, file_path: Path) -> list[ContainerFinding]:
        """Lint a single docker-compose file and return findings."""
        file_path = Path(file_path)
        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []

        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError:
            return []

        if not isinstance(data, dict):
            return []

        services = _get_services(data)
        if not services:
            return []

        findings: list[ContainerFinding] = []
        fp = str(file_path)
        # We need line numbers; do a simple mapping from service name to line
        svc_lines = self._map_service_lines(text, services)

        for svc_name, svc_cfg in services.items():
            if not isinstance(svc_cfg, dict):
                continue
            line = svc_lines.get(svc_name, 1)
            findings.extend(self._check_dc0001(svc_name, svc_cfg, fp, line))
            findings.extend(self._check_dc0002(svc_name, svc_cfg, fp, line))
            findings.extend(self._check_dc0003(svc_name, svc_cfg, fp, line))
            findings.extend(self._check_dc0004(svc_name, svc_cfg, fp, line))
            findings.extend(self._check_dc0005(svc_name, svc_cfg, fp, line))
            findings.extend(self._check_dc0006(svc_name, svc_cfg, fp, line))
            findings.extend(self._check_dc0007(svc_name, svc_cfg, fp, line))
            findings.extend(self._check_dc0008(svc_name, svc_cfg, fp, line))

        return findings

    # -- docker-compose rules ---------------------------------------------

    @staticmethod
    def _check_dc0001(
        svc: str, cfg: dict, fp: str, line: int,
    ) -> list[ContainerFinding]:
        """DC0001: Privileged container."""
        if cfg.get("privileged") is True:
            return [
                ContainerFinding(
                    rule_id="DC0001",
                    title="Privileged container",
                    description=(
                        f"Service '{svc}' runs in privileged mode, granting "
                        f"full access to the host."
                    ),
                    severity=FindingSeverity.CRITICAL,
                    file_path=fp,
                    line_number=line,
                    remediation=(
                        "Remove 'privileged: true' and use specific "
                        "capabilities instead."
                    ),
                )
            ]
        return []

    @staticmethod
    def _check_dc0002(
        svc: str, cfg: dict, fp: str, line: int,
    ) -> list[ContainerFinding]:
        """DC0002: Host network mode."""
        if cfg.get("network_mode") == "host":
            return [
                ContainerFinding(
                    rule_id="DC0002",
                    title="Host network mode",
                    description=(
                        f"Service '{svc}' uses host network mode, bypassing "
                        f"Docker network isolation."
                    ),
                    severity=FindingSeverity.HIGH,
                    file_path=fp,
                    line_number=line,
                    remediation=(
                        "Remove 'network_mode: host' and use Docker "
                        "networks instead."
                    ),
                )
            ]
        return []

    @staticmethod
    def _check_dc0003(
        svc: str, cfg: dict, fp: str, line: int,
    ) -> list[ContainerFinding]:
        """DC0003: Host PID namespace."""
        if cfg.get("pid") == "host":
            return [
                ContainerFinding(
                    rule_id="DC0003",
                    title="Host PID namespace",
                    description=(
                        f"Service '{svc}' uses the host PID namespace, "
                        f"which can expose host processes."
                    ),
                    severity=FindingSeverity.HIGH,
                    file_path=fp,
                    line_number=line,
                    remediation="Remove 'pid: host' from the service.",
                )
            ]
        return []

    @staticmethod
    def _check_dc0004(
        svc: str, cfg: dict, fp: str, line: int,
    ) -> list[ContainerFinding]:
        """DC0004: Secrets in environment variables."""
        findings: list[ContainerFinding] = []
        env = cfg.get("environment")
        if env is None:
            return []

        items: list[tuple[str, str]] = []
        if isinstance(env, dict):
            items = list(env.items())
        elif isinstance(env, list):
            for entry in env:
                entry_str = str(entry)
                if "=" in entry_str:
                    k, v = entry_str.split("=", 1)
                    items.append((k.strip(), v.strip()))

        for key, value in items:
            if not _SECRET_KEYWORDS.search(key):
                continue
            # Skip variable references like ${VAR} or $VAR
            value_str = str(value)
            if value_str.startswith("${") or value_str.startswith("$"):
                continue
            # Skip empty values
            if not value_str or value_str.lower() in ("", "null", "none"):
                continue
            findings.append(
                ContainerFinding(
                    rule_id="DC0004",
                    title="Secret in environment variable",
                    description=(
                        f"Service '{svc}' has environment variable '{key}' "
                        f"that appears to contain a hardcoded secret."
                    ),
                    severity=FindingSeverity.CRITICAL,
                    file_path=fp,
                    line_number=line,
                    remediation=(
                        "Use Docker secrets or external secret management "
                        "instead of hardcoding values. Reference environment "
                        "variables with ${VAR} syntax."
                    ),
                )
            )
        return findings

    @staticmethod
    def _check_dc0005(
        svc: str, cfg: dict, fp: str, line: int,
    ) -> list[ContainerFinding]:
        """DC0005: No resource limits."""
        has_limits = False

        # Check deploy.resources.limits (compose v3)
        deploy = cfg.get("deploy")
        if isinstance(deploy, dict):
            resources = deploy.get("resources")
            if isinstance(resources, dict) and "limits" in resources:
                has_limits = True

        # Check mem_limit / cpus (compose v2)
        if cfg.get("mem_limit") or cfg.get("cpus"):
            has_limits = True

        if not has_limits:
            return [
                ContainerFinding(
                    rule_id="DC0005",
                    title="No resource limits",
                    description=(
                        f"Service '{svc}' has no resource limits configured. "
                        f"A runaway container could consume all host resources."
                    ),
                    severity=FindingSeverity.MEDIUM,
                    file_path=fp,
                    line_number=line,
                    remediation=(
                        "Add resource limits via "
                        "'deploy.resources.limits' (v3) or "
                        "'mem_limit'/'cpus' (v2)."
                    ),
                )
            ]
        return []

    @staticmethod
    def _check_dc0006(
        svc: str, cfg: dict, fp: str, line: int,
    ) -> list[ContainerFinding]:
        """DC0006: Writable root filesystem."""
        if cfg.get("read_only") is not True:
            return [
                ContainerFinding(
                    rule_id="DC0006",
                    title="Writable root filesystem",
                    description=(
                        f"Service '{svc}' does not set 'read_only: true'. "
                        f"A writable root filesystem allows attackers to "
                        f"modify the container."
                    ),
                    severity=FindingSeverity.LOW,
                    file_path=fp,
                    line_number=line,
                    remediation=(
                        "Add 'read_only: true' to the service and use "
                        "tmpfs mounts for writable directories."
                    ),
                )
            ]
        return []

    @staticmethod
    def _check_dc0007(
        svc: str, cfg: dict, fp: str, line: int,
    ) -> list[ContainerFinding]:
        """DC0007: All capabilities added."""
        cap_add = cfg.get("cap_add")
        if isinstance(cap_add, list):
            caps_upper = [str(c).upper().strip('"').strip("'") for c in cap_add]
            if "ALL" in caps_upper:
                return [
                    ContainerFinding(
                        rule_id="DC0007",
                        title="All capabilities added",
                        description=(
                            f"Service '{svc}' adds ALL Linux capabilities, "
                            f"which is equivalent to running in privileged mode."
                        ),
                        severity=FindingSeverity.CRITICAL,
                        file_path=fp,
                        line_number=line,
                        remediation=(
                            "Only add the specific capabilities required. "
                            "Remove 'ALL' from cap_add."
                        ),
                    )
                ]
        return []

    @staticmethod
    def _check_dc0008(
        svc: str, cfg: dict, fp: str, line: int,
    ) -> list[ContainerFinding]:
        """DC0008: No security_opt or no-new-privileges."""
        sec_opts = cfg.get("security_opt")
        has_no_new_privs = False
        if isinstance(sec_opts, list):
            for opt in sec_opts:
                opt_str = str(opt).lower().replace(" ", "")
                if "no-new-privileges" in opt_str:
                    has_no_new_privs = True
                    break

        if not has_no_new_privs:
            return [
                ContainerFinding(
                    rule_id="DC0008",
                    title="No no-new-privileges security option",
                    description=(
                        f"Service '{svc}' does not set "
                        f"'security_opt: [no-new-privileges:true]'. "
                        f"Processes could escalate privileges."
                    ),
                    severity=FindingSeverity.LOW,
                    file_path=fp,
                    line_number=line,
                    remediation=(
                        "Add 'security_opt: [no-new-privileges:true]' "
                        "to the service configuration."
                    ),
                )
            ]
        return []

    # -- file discovery ----------------------------------------------------

    @staticmethod
    def _find_dockerfiles(path: Path) -> list[Path]:
        """Find Dockerfile, Dockerfile.*, *.dockerfile in a directory tree."""
        results: list[Path] = []
        try:
            for item in path.rglob("*"):
                if not item.is_file():
                    continue
                name = item.name
                name_lower = name.lower()
                if name_lower == "dockerfile" or name_lower.startswith("dockerfile."):
                    results.append(item)
                elif name_lower.endswith(".dockerfile"):
                    results.append(item)
        except OSError:
            pass
        return sorted(results)

    @staticmethod
    def _find_compose_files(path: Path) -> list[Path]:
        """Find docker-compose*.yml/yaml and compose*.yml/yaml files."""
        results: list[Path] = []
        try:
            for item in path.rglob("*"):
                if not item.is_file():
                    continue
                name_lower = item.name.lower()
                if not (name_lower.endswith(".yml") or name_lower.endswith(".yaml")):
                    continue
                if name_lower.startswith("docker-compose") or name_lower.startswith("compose"):
                    results.append(item)
        except OSError:
            pass
        return sorted(results)

    @staticmethod
    def _map_service_lines(
        text: str, services: dict[str, Any],
    ) -> dict[str, int]:
        """Map service names to approximate line numbers in YAML text."""
        mapping: dict[str, int] = {}
        lines = text.splitlines()
        # We look for lines matching "  <service_name>:" pattern
        for svc_name in services:
            pattern = re.compile(
                rf"^\s+{re.escape(svc_name)}\s*:", re.MULTILINE,
            )
            for idx, line in enumerate(lines, 1):
                if pattern.match(line):
                    mapping[svc_name] = idx
                    break
            else:
                mapping[svc_name] = 1
        return mapping
