"""Pure-function tests for scripts/vv_galaxy.py (PCA + HTML injection — no AWS)."""

from __future__ import annotations

import importlib.util
import math
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "vv_galaxy", Path(__file__).resolve().parents[2] / "scripts" / "vv_galaxy.py")
vv_galaxy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_SPEC and vv_galaxy)


def test_gram_pca_separates_two_clusters():
    # two tight clusters far apart along one direction -> PC1 splits them
    a = [[1.0 + i * 0.01, 0.0, 0.0] for i in range(5)]
    b = [[-1.0 - i * 0.01, 0.0, 0.0] for i in range(5)]
    (pc1, pc2) = vv_galaxy.gram_pca(a + b, 2)
    assert len(pc1) == 10 and len(pc2) == 10
    left, right = pc1[:5], pc1[5:]
    assert (max(left) < min(right)) or (max(right) < min(left))
    assert all(-1.0 - 1e-6 <= v <= 1.0 + 1e-6 for v in pc1 + pc2)  # normalized


def test_gram_pca_third_component_and_empty():
    pts = [[math.sin(i), math.cos(i * 2), i * 0.1, 0.0] for i in range(8)]
    comps = vv_galaxy.gram_pca(pts, 3)
    assert len(comps) == 3 and all(len(c) == 8 for c in comps)
    assert vv_galaxy.gram_pca([], 2) == [[], []]


def test_build_html_injects_data_and_escapes_script_close(tmp_path):
    tpl = tmp_path / "t.html"
    tpl.write_text("<script id=data>__DATA__</script>")
    points = [{"key": "k1", "text": "sneaky </script> content", "x": 0, "y": 0}]
    html = vv_galaxy.build_html(tpl, points, None)
    assert "k1" in html
    assert "</script> content" not in html  # guarded as <\/script>
    assert "<\\/script> content" in html


def test_active_only_filters_by_status():
    vecs = [
        {"key": "a", "metadata": {"status": "active"}},
        {"key": "b", "metadata": {"status": "superseded"}},
        {"key": "c", "metadata": {"status": "archived"}},
        {"key": "d", "metadata": {}},   # no status -> dropped
        {"key": "e"},                   # no metadata -> dropped
        {"key": "f", "metadata": {"status": "active"}},
    ]
    assert [v["key"] for v in vv_galaxy.active_only(vecs)] == ["a", "f"]


def test_build_html_requires_wasm_when_placeholder_present(tmp_path):
    tpl = tmp_path / "t3d.html"
    tpl.write_text("__DATA__ __WASM__")
    with pytest.raises(SystemExit):
        vv_galaxy.build_html(tpl, [], None)
    assert "AAAA" in vv_galaxy.build_html(tpl, [], "AAAA")


def test_build_search_backend_none_for_ambient_role():
    # role == "none" => no scoped client to attribute reads => search disabled.
    assert vv_galaxy.build_search_backend("us-west-2", "none", active_only=False) is None


def test_handle_refresh_returns_points_on_success():
    points = [{"key": "k1", "x": 0.1, "y": 0.2, "z": 0.3}]
    status, body = vv_galaxy.handle_refresh(lambda: points)
    assert status == 200
    assert body == points


def test_handle_refresh_503_on_empty():
    status, body = vv_galaxy.handle_refresh(lambda: [])
    assert status == 503
    assert "error" in body


def test_handle_refresh_500_on_exception():
    def boom():
        raise RuntimeError("aws exploded")
    status, body = vv_galaxy.handle_refresh(boom)
    assert status == 500
    assert "error" in body


def test_refresh_closure_shape_matches_to_points_output(monkeypatch):
    # Simulates main()'s refresh_fn: fetch -> optional active_only -> to_points,
    # using a fake vector so we don't need AWS. Confirms to_points' output survives
    # the round trip through handle_refresh untouched.
    fake_vectors = [{
        "key": "mem_x", "data": {"float32": [0.1, 0.2, 0.3, 0.4]},
        "metadata": {"agent_id": "a", "team_id": "t", "task_id": "tk",
                      "memory_type": "semantic", "status": "active", "version": 1,
                      "created_at": 100, "content": "hello"},
    }] * 3  # gram_pca needs >1 row to be meaningful; 3 identical rows is fine here

    def refresh_fn():
        return vv_galaxy.to_points(fake_vectors, 3)

    status, body = vv_galaxy.handle_refresh(refresh_fn)
    assert status == 200
    assert len(body) == 3
    assert body[0]["key"] == "mem_x"
    assert "z" in body[0]  # 3D projection present
