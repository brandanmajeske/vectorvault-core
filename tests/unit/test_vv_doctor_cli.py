from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from vectorvault.doctor import DoctorCheck, DoctorReport

_SPEC = importlib.util.spec_from_file_location(
    "vv_doctor_cli", Path(__file__).resolve().parents[2] / "scripts" / "vv.py"
)
vv = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(vv)


def test_doctor_parser_accepts_json_and_data_plane_probe():
    args = vv.build_parser().parse_args(["doctor", "--json", "--probe-data-plane"])
    assert args.cmd == "doctor"
    assert args.json is True
    assert args.probe_data_plane is True


def test_doctor_dispatches_before_build_client(monkeypatch, capsys):
    report = DoctorReport(
        region="us-west-2",
        role="none",
        agent_id="vv-cli",
        profile=None,
        checks=(DoctorCheck("runtime", "pass", "ok"),),
    )
    monkeypatch.setattr("vectorvault.doctor.run_doctor", lambda **_kwargs: report)
    monkeypatch.setattr(vv, "build_client", lambda _args: (_ for _ in ()).throw(AssertionError()))

    assert vv.main(["doctor", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["healthy"] is True



def test_doctor_rejects_unknown_flags():
    with pytest.raises(SystemExit) as exc:
        vv.main(["doctor", "--typo"])
    assert exc.value.code == 2
