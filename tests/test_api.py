"""FastAPI 接口集成测试。"""
import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app


def _make_app(tmp_path: Path, *, allow_private: bool = True):
    root = tmp_path
    root.mkdir(parents=True, exist_ok=True)
    cfg = {
        "allow_private_network": allow_private,
        "download_dir": str(root / "downloads"),
        "global_concurrency": 4,
        "per_domain_concurrency": 4,
        "max_retries": 1,
        "timeout": 15.0,
    }
    (root / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    return create_app(root)


def test_add_and_download_completes(tmp_path, server):
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        r = client.post("/api/tasks", json={"urls": [f"{server.url}/file/100"]})
        assert r.status_code == 200
        assert r.json()["added"] == 1

        deadline = time.time() + 20
        status = None
        while time.time() < deadline:
            tasks = client.get("/api/tasks").json()["tasks"]
            if tasks:
                status = tasks[0]["status"]
                if status == "completed":
                    break
            time.sleep(0.05)
        assert status == "completed"


def test_add_rejects_invalid_url(tmp_path, server):
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        r = client.post("/api/tasks", json={"urls": ["ftp://example.com/x"]})
        assert r.status_code == 400


def test_add_rejects_private_url_by_default(tmp_path, server):
    app = _make_app(tmp_path, allow_private=False)
    with TestClient(app) as client:
        r = client.post("/api/tasks", json={"urls": ["http://127.0.0.1:1/x"]})
        assert r.status_code == 400
        assert "拦截" in r.json()["detail"]


def test_import_text_multiline(tmp_path, server):
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        text = f"{server.url}/file/1\n{server.url}/file/2\n# 注释行\nnot-a-url\n"
        r = client.post("/api/tasks", json={"text": text})
        assert r.status_code == 200
        assert r.json()["added"] == 2


def test_config_get_and_update(tmp_path, server):
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        cfg = client.get("/api/config").json()
        assert cfg["global_concurrency"] == 4
        r = client.post("/api/config", json={
            "global_concurrency": 8,
            "conflict_policy": "overwrite",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["global_concurrency"] == 8
        assert body["conflict_policy"] == "overwrite"
        # 已持久化
        cfg2 = client.get("/api/config").json()
        assert cfg2["conflict_policy"] == "overwrite"


def test_config_rejects_bad_conflict_policy(tmp_path, server):
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        r = client.post("/api/config", json={"conflict_policy": "nuke"})
        assert r.status_code == 400


def test_parse_webpage(tmp_path, server):
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        r = client.post("/api/parse", json={"urls": [f"{server.url}/html"]})
        assert r.status_code == 200
        res = r.json()["results"][0]
        assert res["is_html"] is True
        assert res["downloadable_count"] >= 3


def test_index_served(tmp_path, server):
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        r = client.get("/")
        assert r.status_code == 200
        assert "批量下载器" in r.text
