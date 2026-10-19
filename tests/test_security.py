"""安全校验单元测试：URL/SSRF、文件名、路径。"""
import pytest

from app.security import (
    SecurityError,
    safe_join,
    sanitize_filename,
    validate_url,
)


def test_validate_url_allows_public_https():
    assert validate_url("https://example.com/file.zip") == "https://example.com/file.zip"
    assert validate_url("http://example.org/a/b.pdf?x=1") == "http://example.org/a/b.pdf?x=1"


def test_validate_url_rejects_non_http_scheme():
    for bad in ["ftp://example.com/f", "file:///etc/passwd", "javascript:alert(1)", ""]:
        with pytest.raises(SecurityError):
            validate_url(bad)


def test_validate_url_rejects_localhost():
    for bad in [
        "http://localhost/admin",
        "http://localhost:8080/x",
        "http://127.0.0.1/x",
        "http://[::1]/x",
    ]:
        with pytest.raises(SecurityError):
            validate_url(bad)


def test_validate_url_rejects_private_ip():
    for bad in [
        "http://10.0.0.1/x",
        "http://192.168.1.1/x",
        "http://172.16.0.1/x",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.64.0.1/x",
    ]:
        with pytest.raises(SecurityError):
            validate_url(bad)


def test_validate_url_rejects_metadata_hostname():
    with pytest.raises(SecurityError):
        validate_url("http://metadata.google.internal/computeMetadata/v1/")


def test_validate_url_rejects_credentials():
    with pytest.raises(SecurityError):
        validate_url("http://user:pass@example.com/x")


def test_validate_url_allow_private_flag():
    assert validate_url("http://127.0.0.1:8000/x", allow_private=True)


def test_sanitize_filename_removes_traversal():
    assert ".." not in sanitize_filename("..\\..\\windows\\system32")
    assert sanitize_filename("a/b/c.txt") == "a_b_c.txt"
    assert sanitize_filename("a\\b.txt") == "a_b.txt"


def test_sanitize_filename_illegal_chars():
    assert sanitize_filename('a<b>c:d"e|f?g*h.txt') == "a_b_c_d_e_f_g_h.txt"


def test_sanitize_filename_reserved_name():
    assert sanitize_filename("CON") == "_CON"
    assert sanitize_filename("nul.txt") == "_nul.txt"


def test_sanitize_filename_empty_and_dots():
    assert sanitize_filename("") == "download"
    assert sanitize_filename("   ") == "download"
    assert sanitize_filename("...") == "download"
    assert sanitize_filename("file.") == "file"


def test_safe_join_prevents_escape(tmp_path):
    base = tmp_path / "dl"
    base.mkdir()
    p = safe_join(base, "normal.txt")
    assert p.parent == base.resolve()

    with pytest.raises(SecurityError):
        safe_join(base, "..\\..\\evil.txt")
    with pytest.raises(SecurityError):
        safe_join(base, "../evil.txt")
