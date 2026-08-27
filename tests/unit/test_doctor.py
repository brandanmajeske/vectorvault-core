from __future__ import annotations

import json

from vectorvault.doctor import DoctorCheck, DoctorReport, run_doctor

PARAMETERS = {
    "/vectorvault/region": "us-west-2",
    "/vectorvault/vector-bucket-name": "vector-bucket",
    "/vectorvault/content-bucket-name": "content-bucket",
    "/vectorvault/index/shared-team-memory-name": "shared-index",
    "/vectorvault/index/private-planner-name": "planner-index",
    "/vectorvault/index/private-researcher-name": "researcher-index",
    "/vectorvault/table/memory-embed-cache": "embed-cache",
    "/vectorvault/table/memory-index": "memory-index",
    "/vectorvault/table/memory-index-task-gsi": "task-index",
}


class _Paginator:
    def paginate(self, **_kwargs):
        return [{"Parameters": [{"Name": name, "Value": value} for name, value in PARAMETERS.items()]}]


class _SSM:
    def __init__(self, fail=False):
        self.fail = fail
        self.parameters = []

    def get_paginator(self, _name):
        if self.fail:
            raise RuntimeError("SSM unavailable")
        return _Paginator()

    def get_parameter(self, **kwargs):
        self.parameters.append(kwargs)
        return {"Parameter": {"Value": "arn:aws:iam::123456789012:role/vectorvault-planner"}}


class _STS:
    def __init__(self, fail=False):
        self.fail = fail
        self.assume_calls = []

    def get_caller_identity(self):
        if self.fail:
            raise RuntimeError("credentials unavailable")
        return {"Arn": "arn:aws:sts::123456789012:assumed-role/Dev/user@example.com"}

    def assume_role(self, **kwargs):
        self.assume_calls.append(kwargs)
        return {"Credentials": {}}


class _S3Vectors:
    def __init__(self):
        self.list_calls = []

    def list_vectors(self, **kwargs):
        self.list_calls.append(kwargs)
        return {"vectors": []}


class _Session:
    def __init__(self, sts=None, ssm=None, s3vectors=None):
        self.clients = {"sts": sts or _STS(), "ssm": ssm or _SSM(), "s3vectors": s3vectors or _S3Vectors()}

    def client(self, name, **_kwargs):
        return self.clients[name]


def _role_session(role_arn, agent_id, region, *, source_identity, sts_client):
    sts_client.assume_role(
        RoleArn=role_arn,
        RoleSessionName=agent_id,
        SourceIdentity=source_identity,
    )
    return _Session(s3vectors=_S3Vectors())


def _statuses(report):
    return {check.name: check.status for check in report.checks}


def test_doctor_passes_ambient_identity_and_config():
    report = run_doctor(
        region="us-west-2",
        role="none",
        agent_id="doctor-test",
        session=_Session(),
        package_version="1.29.1",
    )

    assert report.exit_code == 0
    assert _statuses(report)["aws_identity"] == "pass"
    assert _statuses(report)["ssm_config"] == "pass"
    assert _statuses(report)["role_assumption"] == "pass"
    assert _statuses(report)["data_plane"] == "skip"


def test_doctor_role_check_passes_with_source_identity():
    sts = _STS()
    report = run_doctor(
        region="us-west-2",
        role="planner",
        agent_id="doctor-test",
        session=_Session(sts=sts),
        package_version="1.29.1",
        role_session_factory=_role_session,
    )

    assert report.exit_code == 0
    assert _statuses(report)["role_assumption"] == "pass"
    assert sts.assume_calls[0]["SourceIdentity"] == "user@example.com"
    assert sts.assume_calls[0]["RoleSessionName"] == "doctor-test"


def test_doctor_data_plane_probe_is_read_only():
    s3vectors = _S3Vectors()
    report = run_doctor(
        region="us-west-2",
        role="none",
        agent_id="doctor-test",
        session=_Session(s3vectors=s3vectors),
        package_version="1.29.1",
        probe_data_plane=True,
    )

    assert report.exit_code == 0
    assert _statuses(report)["data_plane"] == "pass"
    assert s3vectors.list_calls[0]["indexName"] == "shared-index"
    assert s3vectors.list_calls[0]["returnData"] is False


def test_doctor_continues_after_identity_and_config_failures():
    report = run_doctor(
        region="us-west-2",
        role="planner",
        agent_id="doctor-test",
        session=_Session(sts=_STS(fail=True), ssm=_SSM(fail=True)),
        package_version="2.0.0",
        probe_data_plane=True,
    )

    statuses = _statuses(report)
    assert report.exit_code == 1
    assert statuses["aws_identity"] == "fail"
    assert statuses["ssm_config"] == "fail"
    assert statuses["role_assumption"] == "skip"
    assert statuses["data_plane"] == "skip"
    assert statuses["mcp_dependency"] == "warn"


def test_doctor_rejects_invalid_role_without_aws_calls():
    session = _Session()
    report = run_doctor(
        region="us-west-2",
        role="typo",
        agent_id="doctor-test",
        session=session,
        package_version="1.29.1",
    )

    assert report.exit_code == 1
    assert _statuses(report)["role"] == "fail"
    assert session.clients["sts"].assume_calls == []


def test_report_json_is_machine_readable():
    report = DoctorReport(
        region="us-west-2",
        role="none",
        agent_id="doctor-test",
        profile=None,
        checks=(DoctorCheck("runtime", "pass", "ok"),),
    )

    payload = json.loads(report.to_json())
    assert payload["healthy"] is True
    assert payload["checks"][0]["name"] == "runtime"
