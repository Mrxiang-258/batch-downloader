"""网页解析与文本导入测试。"""
from app.parser import extract_links, extract_urls_from_text


HTML = """
<html><body>
<a href="/file/1000.pdf">document</a>
<a href="/file/2000.jpg">photo</a>
<a href="/page.html">another page</a>
<a href="/file/3000" download>download-attr</a>
<a href="https://example.com/external.zip">external</a>
<a href="#top">anchor</a>
<a href="javascript:void(0)">js</a>
</body></html>
"""

BASE = "http://127.0.0.1:8000"


def test_extract_links_marks_downloadable():
    links = extract_links(HTML, BASE + "/index")
    by_url = {l["url"]: l for l in links}
    assert by_url[BASE + "/file/1000.pdf"]["downloadable"] is True
    assert by_url[BASE + "/file/2000.jpg"]["downloadable"] is True
    assert by_url[BASE + "/page.html"]["downloadable"] is False
    assert by_url[BASE + "/file/3000"]["downloadable"] is True  # download 属性
    assert by_url["https://example.com/external.zip"]["downloadable"] is True


def test_extract_links_skips_js_and_anchor():
    links = extract_links(HTML, BASE + "/index")
    urls = [l["url"] for l in links]
    assert not any("javascript:" in u for u in urls)
    assert not any(u.endswith("#top") for u in urls)


def test_extract_links_dedupes():
    html = '<a href="/a.zip">1</a><a href="/a.zip">2</a>'
    links = extract_links(html, "http://127.0.0.1:8000/")
    assert len(links) == 1


def test_extract_links_resolves_relative():
    links = extract_links('<a href="files/b.zip">b</a>', "http://127.0.0.1:8000/dir/page.html")
    assert links[0]["url"] == "http://127.0.0.1:8000/dir/files/b.zip"


def test_extract_urls_from_text():
    text = """
    # 这是注释
    https://example.com/a.zip
    https://example.com/b.pdf,some,name
    https://example.com/c.mp4\tsomething
    not-a-url line
    """
    urls = extract_urls_from_text(text)
    assert urls == [
        "https://example.com/a.zip",
        "https://example.com/b.pdf",
        "https://example.com/c.mp4",
    ]


def test_extract_urls_from_text_ignores_non_http():
    text = "ftp://example.com/x\nhttp://example.com/y.zip\nwww.example.com/z.zip"
    assert extract_urls_from_text(text) == ["http://example.com/y.zip"]
