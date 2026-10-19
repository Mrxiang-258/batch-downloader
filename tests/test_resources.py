"""网页资源分类测试：扩展名/MIME 映射、分类、去重、命名。"""
import asyncio

from app.fileutils import categorize_ext, categorize_mime, extension_from_mime
from app.resource import classify, domain_of, normalize_url

from .helpers import running_manager


def test_categorize_ext_mapping():
    assert categorize_ext("pdf") == "文档"
    assert categorize_ext("docx") == "文档"
    assert categorize_ext("txt") == "文档"
    assert categorize_ext("xlsx") == "表格"
    assert categorize_ext("csv") == "表格"
    assert categorize_ext("pptx") == "演示文稿"
    assert categorize_ext("zip") == "压缩包"
    assert categorize_ext("7z") == "压缩包"
    assert categorize_ext("jpg") == "图片"
    assert categorize_ext("svg") == "图片"
    assert categorize_ext("mp4") == "视频"
    assert categorize_ext("mkv") == "视频"
    assert categorize_ext("mp3") == "音频"
    assert categorize_ext("flac") == "音频"
    assert categorize_ext("exe") == "程序与安装包"
    assert categorize_ext("apk") == "程序与安装包"
    assert categorize_ext("json") == "数据与其他文件"
    assert categorize_ext("sql") == "数据与其他文件"
    assert categorize_ext("unknownxyz") == "未知类型"


def test_categorize_mime_mapping():
    assert categorize_mime("application/pdf") == "文档"
    assert categorize_mime("text/csv") == "表格"
    assert categorize_mime("application/vnd.ms-excel") == "表格"
    assert categorize_mime("application/vnd.ms-powerpoint") == "演示文稿"
    assert categorize_mime("application/zip") == "压缩包"
    assert categorize_mime("image/png") == "图片"
    assert categorize_mime("video/mp4") == "视频"
    assert categorize_mime("audio/mpeg") == "音频"
    assert categorize_mime("application/x-msdownload") == "程序与安装包"
    assert categorize_mime("application/json") == "数据与其他文件"
    assert categorize_mime("application/x-weird") == "未知类型"


def test_extension_from_mime():
    assert extension_from_mime("application/pdf") == "pdf"
    assert extension_from_mime("image/jpeg") == "jpg"
    assert extension_from_mime("application/x-unknown") == ""


def test_classify_by_extension():
    cat, ext, name, note = classify("https://x.com/a/b.zip", "", "")
    assert cat == "压缩包"
    assert ext == "zip"
    assert name == "b.zip"
    assert note == ""


def test_classify_by_content_disposition():
    cat, ext, name, note = classify(
        "https://x.com/download?id=1", "application/pdf", "报告.pdf"
    )
    assert cat == "文档"
    assert ext == "pdf"
    assert name == "报告.pdf"


def test_classify_no_extension_uses_mime():
    cat, ext, name, note = classify("https://x.com/download?id=1", "image/png", "")
    assert cat == "图片"
    assert ext == "png"
    assert name.endswith(".png")


def test_classify_html_response_not_file():
    cat, ext, name, note = classify("https://x.com/file.pdf", "text/html", "")
    assert cat == "未知类型"
    assert "网页" in note


def test_classify_unknown():
    cat, ext, name, note = classify("https://x.com/download", "application/x-weird", "")
    assert cat == "未知类型"
    assert note


def test_normalize_url_dedupe_keeps_tokens():
    a = normalize_url("https://Example.com:443/Path?token=abc&sig=xyz")
    b = normalize_url("https://example.com/Path?token=abc&sig=xyz")
    assert a == b
    # 查询参数（签名/令牌）原样保留，不重排、不丢弃
    assert "token=abc&sig=xyz" in a
    # 查询参数不同则不去重
    c = normalize_url("https://example.com/Path?token=other")
    assert a != c


def test_normalize_url_fragment_stripped():
    assert normalize_url("https://x.com/a.zip#frag") == "https://x.com/a.zip"


def test_domain_of():
    assert domain_of("https://sub.example.com/x") == "sub.example.com"
    assert domain_of("http://a.b.c:8080/p") == "a.b.c"


def test_parse_page_classifies_resources(tmp_path, server):
    async def main():
        async with running_manager(tmp_path) as mgr:
            result = await mgr.parse_page(f"{server.url}/html-res")
            assert result["is_html"] is True
            resources = result["resources"]
            assert result["resource_count"] >= 4
            cats = {r["category"] for r in resources}
            assert "文档" in cats       # pdf + content-disposition
            assert "图片" in cats       # jpg
            assert "表格" in cats       # xlsx
            assert "压缩包" in cats     # zip
            # 分类数量统计
            assert result["categories"]["文档"] >= 2
            # 每个资源都有 url 和合法状态
            for r in resources:
                assert r["url"]
                assert r["status"] in (
                    "available", "auth_required", "inaccessible", "unknown_type",
                )
    asyncio.run(main())
