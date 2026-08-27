"""Read-only diagnostics for VectorVault CLI and MCP setup.

The doctor performs live AWS control-plane reads (STS and SSM) and an optional
S3 Vectors list probe. It never embeds, writes, archives, restores, or purges
memory. The probe functions are injectable so the default unit suite remains
hermetic.
"""
from __future__ import annotations

import importlib.metadata
import json
import os
import re
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from vectorvault.config import Config
from vectorvault.tools.memory_tools import _source_identity, refreshable_assumed_session

VALID_ROLES = frozenset({"none", "planner", "researcher", "auditor", "admin"})
_ROLE_ARN_PREFIX = "/vectorvault/role/"


@dataclass(frozen=True)
class DoctorCheck:
    """One diagnostic result."""

    name: str
    status: str
    detail: str
    remediation: str | None = None
    required: bool = True


@dataclass(frozen=True)
class DoctorReport:
    """Structured doctor output shared by human and JSON renderers."""

    region: str
    role: str
    agent_id: str
    profile: str | None
    checks: tuple[DoctorCheck, ...]

    @property
    def exit_code(self) -> int:
        return int(any(c.required and c.status == "fail" for c in self.checks))

    @property
    def healthy(self) -> bool:
        return self.exit_code == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "healthy": self.healthy,
            "exit_code": self.exit_code,
            "context": {
                "region": self.region,
                "role": self.role,
                "agent_id": self.agent_id,
                "profile": self.profile,
            },
            "checks": [asdict(check) for check in self.checks],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2)

    def render(self) -> str:
        lines = [
            f"VectorVault doctor ({self.region}, role={self.role}, agent={self.agent_id})",
        ]
        for check in self.checks:
            suffix = f" — {check.remediation}" if check.remediation else ""
            lines.append(f"[{check.status.upper()}] {check.name}: {check.detail}{suffix}")
        outcome = "healthy" if self.healthy else "failed"
        lines.append(f"Result: {outcome} (exit {self.exit_code})")
        return "\n".join(lines)


def _error(exc: Exception) -> str:
    """Keep failures actionable without dumping credentials or response bodies."""
    message = str(exc).strip().replace("\n", " ")
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _major_version(version: str) -> int | None:
    match = re.match(r"\s*(\d+)", version)
    return int(match.group(1)) if match else None


def _package_check(version: str | None = None) -> DoctorCheck:
    if version is None:
        try:
            version = importlib.metadata.version("mcp")
        except importlib.metadata.PackageNotFoundError:
            return DoctorCheck(
                "mcp_dependency",
                "warn",
                "optional package mcp is not installed",
                "install with: pip install -e '.[mcp]'",
                required=False,
            )
    major = _major_version(version)
    if major is not None and major >= 2:
        return DoctorCheck(
            "mcp_dependency",
            "warn",
            f"mcp {version} is installed, but this server requires mcp<2",
            "install a compatible version with: pip install 'mcp>=1.2,<2'",
            required=False,
        )
    return DoctorCheck("mcp_dependency", "pass", f"mcp {version} is compatible")


def run_doctor(
    *,
    region: str,
    role: str,
    agent_id: str,
    profile: str | None = None,
    probe_data_plane: bool = False,
    session: Any | None = None,
    package_version: str | None = None,
    role_session_factory: Callable[..., Any] | None = None,
) -> DoctorReport:
    """Run read-only diagnostics.

    ``session``, ``package_version``, and ``role_session_factory`` are dependency
    injection seams for unit tests. A role check uses the same
    ``refreshable_assumed_session`` implementation as production MCP/CLI clients,
    including the IAM-enforced SourceIdentity value.
    """
    effective_profile = profile or os.environ.get("AWS_PROFILE")
    checks: list[DoctorCheck] = [
        DoctorCheck(
            "runtime",
            "pass",
            f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        ),
        _package_check(package_version),
    ]

    if role not in VALID_ROLES:
        checks.append(
            DoctorCheck(
                "role",
                "fail",
                f"unsupported role {role!r}",
                f"choose one of: {', '.join(sorted(VALID_ROLES))}",
            )
        )
        return DoctorReport(region, role, agent_id, effective_profile, tuple(checks))

    try:
        if session is None:
            import boto3

            session = boto3.Session(profile_name=profile, region_name=region) if profile else boto3.Session(
                region_name=region
            )
        sts = session.client("sts", region_name=region)
        caller = sts.get_caller_identity()
        source_identity = _source_identity(caller)
        checks.append(
            DoctorCheck(
                "aws_identity",
                "pass",
                f"authenticated as {caller.get('Arn', 'unknown principal')}",
            )
        )
    except Exception as exc:
        sts = None
        caller = None
        source_identity = None
        checks.append(
            DoctorCheck(
                "aws_identity",
                "fail",
                _error(exc),
                "refresh AWS SSO credentials or check AWS_PROFILE/AWS_REGION",
            )
        )

    try:
        ssm = session.client("ssm", region_name=region)
        config = Config.from_ssm(ssm)
        checks.append(
            DoctorCheck(
                "ssm_config",
                "pass",
                "resolved required /vectorvault configuration",
            )
        )
    except Exception as exc:
        ssm = None
        config = None
        checks.append(
            DoctorCheck(
                "ssm_config",
                "fail",
                _error(exc),
                "verify the deployed SSM contract and selected region",
            )
        )

    assumed_session = None
    if role == "none":
        checks.append(DoctorCheck("role_assumption", "pass", "using ambient AWS credentials"))
    elif sts is None or ssm is None or source_identity is None:
        checks.append(
            DoctorCheck(
                "role_assumption",
                "skip",
                "cannot test role assumption until AWS identity and SSM checks pass",
                required=False,
            )
        )
    else:
        try:
            role_arn = ssm.get_parameter(Name=f"{_ROLE_ARN_PREFIX}{role}-arn")["Parameter"]["Value"]
            assume = role_session_factory or refreshable_assumed_session
            assumed_session = assume(
                role_arn,
                agent_id,
                region,
                source_identity=source_identity,
                sts_client=sts,
            )
            checks.append(
                DoctorCheck(
                    "role_assumption",
                    "pass",
                    f"{role} role assumption succeeded with SourceIdentity",
                )
            )
        except Exception as exc:
            checks.append(
                DoctorCheck(
                    "role_assumption",
                    "fail",
                    _error(exc),
                    "verify the role ARN and IAM trust policy requires the derived SourceIdentity",
                )
            )

    if probe_data_plane:
        if config is None:
            checks.append(
                DoctorCheck(
                    "data_plane",
                    "skip",
                    "cannot probe S3 Vectors until SSM config resolves",
                    required=False,
                )
            )
        elif role != "none" and assumed_session is None:
            checks.append(
                DoctorCheck(
                    "data_plane",
                    "skip",
                    "cannot probe S3 Vectors until role assumption succeeds",
                    required=False,
                )
            )
        else:
            try:
                probe_session = assumed_session or session
                s3vectors = probe_session.client("s3vectors", region_name=region)
                s3vectors.list_vectors(
                    vectorBucketName=config.vector_bucket,
                    indexName=config.shared_index,
                    maxResults=1,
                    returnData=False,
                    returnMetadata=False,
                )
                checks.append(
                    DoctorCheck(
                        "data_plane",
                        "pass",
                        "S3 Vectors shared index list succeeded (no embedding or write performed)",
                    )
                )
            except Exception as exc:
                checks.append(
                    DoctorCheck(
                        "data_plane",
                        "fail",
                        _error(exc),
                        "verify S3 Vectors index names and read permissions",
                    )
                )
    else:
        checks.append(
            DoctorCheck(
                "data_plane",
                "skip",
                "not requested; use --probe-data-plane for a read-only S3 Vectors list",
                required=False,
            )
        )

    return DoctorReport(region, role, agent_id, effective_profile, tuple(checks))
