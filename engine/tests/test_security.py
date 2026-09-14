"""S1-4 安全边界测试：引擎访问控制 + 上传路径校验。

对应调研报告 P0-4：「本地 HTTP 无鉴权 + /api/distill/init 任意文件读取」。
这些用例是回归防线——它们失败意味着引擎重新暴露给本机任意进程。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.web.auth import TOKEN_ENV, install_auth
from src.web.server import UPLOAD_ROOTS_ENV, _resolve_upload_path, create_app

TOKEN = "test-token-0123456789abcdef"


@pytest.fixture()
def authed_client(sandbox, monkeypatch):
    """带 token 鉴权的客户端（模拟桌面壳注入环境变量的场景）。"""
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    from src.web import server as server_mod

    app = server_mod.create_app("demo-web")
    with TestClient(app) as client:
        yield client


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


class TestEngineAuth:
    def test_no_token_configured_means_open(self, app_client):
        """未注入 token（CLI / 测试场景）时不启用鉴权，保持向后兼容。"""
        client, _ = app_client
        assert client.get("/api/ping").status_code == 200

    def test_ping_reports_auth_required(self, authed_client):
        res = authed_client.get("/api/ping", headers=_auth())
        assert res.status_code == 200
        assert res.json()["auth_required"] is True

    @pytest.mark.parametrize(
        "path",
        ["/api/ping", "/api/books", "/api/chapters", "/api/settings/tree", "/api/history"],
    )
    def test_all_api_endpoints_require_token(self, authed_client, path):
        """无 token 访问任何 /api/* 一律 401（覆盖调研报告中的「全端点无鉴权」）。"""
        assert authed_client.get(path).status_code == 401

    def test_wrong_token_rejected(self, authed_client):
        res = authed_client.get("/api/books", headers={"Authorization": "Bearer wrong-token"})
        assert res.status_code == 401

    def test_malformed_authorization_rejected(self, authed_client):
        res = authed_client.get("/api/books", headers={"Authorization": TOKEN})
        assert res.status_code == 401

    def test_correct_token_allowed(self, authed_client):
        assert authed_client.get("/api/books", headers=_auth()).status_code == 200

    def test_write_endpoints_require_token(self, authed_client):
        """破坏性端点同样受保护：删书、改模型配置。"""
        assert authed_client.delete("/api/books/whatever").status_code == 401
        assert (
            authed_client.put(
                "/api/model-config/roles/writer",
                json={"provider": "evil", "model": "x", "temperature": 0.5},
            ).status_code
            == 401
        )

    def test_origin_header_rejected(self, authed_client):
        """纵深防御：即使携带正确 token，浏览器上下文发起的请求也被拒。"""
        res = authed_client.get(
            "/api/books", headers={**_auth(), "Origin": "http://evil.example"}
        )
        assert res.status_code == 403
        assert "Origin" in res.json()["detail"]

    def test_root_page_still_public(self, authed_client):
        """旧内置单页仍可加载（其 API 调用会被 401 挡住，符合预期）。"""
        assert authed_client.get("/").status_code == 200

    def test_install_auth_returns_false_without_token(self, sandbox, monkeypatch):
        monkeypatch.delenv(TOKEN_ENV, raising=False)
        app = create_app("demo-web")
        assert install_auth(app) is False

    def test_install_auth_returns_true_with_token(self, sandbox, monkeypatch):
        monkeypatch.setenv(TOKEN_ENV, TOKEN)
        app = create_app("demo-web")
        assert install_auth(app) is True


class TestUploadPathGuard:
    """蒸馏上传路径：绝对路径 / 存在性 / 后缀 / 白名单包含性。"""

    def test_empty_path_rejected(self, sandbox):
        with pytest.raises(Exception) as exc:
            _resolve_upload_path("")
        assert getattr(exc.value, "status_code", None) == 400

    def test_relative_path_rejected(self, sandbox, tmp_path):
        sample = tmp_path / "a.txt"
        sample.write_text("x", encoding="utf-8")
        with pytest.raises(Exception) as exc:
            _resolve_upload_path("a.txt")
        assert getattr(exc.value, "status_code", None) == 400

    def test_missing_file_rejected(self, sandbox, tmp_path):
        with pytest.raises(Exception) as exc:
            _resolve_upload_path(str(tmp_path / "nope.txt"))
        assert getattr(exc.value, "status_code", None) == 400

    def test_directory_rejected(self, sandbox, tmp_path):
        with pytest.raises(Exception) as exc:
            _resolve_upload_path(str(tmp_path))
        assert getattr(exc.value, "status_code", None) == 400

    @pytest.mark.parametrize("name", ["secret.exe", "id_rsa", "config.yaml", "book.pdf"])
    def test_disallowed_suffix_rejected(self, sandbox, tmp_path, name):
        """调研报告中「任意文件读取」的核心利用点：读非书籍文件。"""
        target = tmp_path / name
        target.write_text("sensitive", encoding="utf-8")
        with pytest.raises(Exception) as exc:
            _resolve_upload_path(str(target))
        assert getattr(exc.value, "status_code", None) == 400

    @pytest.mark.parametrize("name", ["book.txt", "book.epub", "book.md", "book.TXT"])
    def test_allowed_suffix_accepted(self, sandbox, tmp_path, name):
        target = tmp_path / name
        target.write_text("正文", encoding="utf-8")
        assert _resolve_upload_path(str(target)) == target.resolve()

    def test_root_whitelist_blocks_outside_path(self, sandbox, tmp_path, monkeypatch):
        allowed = tmp_path / "allowed"
        outside = tmp_path / "outside"
        allowed.mkdir()
        outside.mkdir()
        (outside / "book.txt").write_text("x", encoding="utf-8")
        monkeypatch.setenv(UPLOAD_ROOTS_ENV, str(allowed))
        with pytest.raises(Exception) as exc:
            _resolve_upload_path(str(outside / "book.txt"))
        assert getattr(exc.value, "status_code", None) == 403

    def test_root_whitelist_allows_inside_path(self, sandbox, tmp_path, monkeypatch):
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        target = allowed / "book.txt"
        target.write_text("x", encoding="utf-8")
        monkeypatch.setenv(UPLOAD_ROOTS_ENV, str(allowed))
        assert _resolve_upload_path(str(target)) == target.resolve()

    def test_multiple_roots_accepts_second(self, sandbox, tmp_path, monkeypatch):
        import os

        first, second = tmp_path / "r1", tmp_path / "r2"
        first.mkdir()
        second.mkdir()
        target = second / "book.txt"
        target.write_text("x", encoding="utf-8")
        monkeypatch.setenv(UPLOAD_ROOTS_ENV, os.pathsep.join([str(first), str(second)]))
        assert _resolve_upload_path(str(target)) == target.resolve()

    def test_api_rejects_traversal_shaped_read(self, authed_client, tmp_path):
        """经 API 层验证：读取不可读文件被拒（非 200）。"""
        target = tmp_path / "credentials"
        target.write_text("secret", encoding="utf-8")
        res = authed_client.post(
            "/api/distill/init",
            json={"file_path": str(target)},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert res.status_code in (400, 403)
