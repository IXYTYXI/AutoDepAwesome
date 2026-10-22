import importlib.util
from pathlib import Path


def renderer():
    path = Path(__file__).resolve().parents[1] / "scripts" / "render_feishu_manual.py"
    spec = importlib.util.spec_from_file_location("render_feishu_manual", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.render_document


def test_feishu_snapshot_renderer_keeps_readable_structure_and_rejects_active_markup():
    render = renderer()
    output = render('''<title id="x">手册</title><p onclick="alert(1)">正文
      <a href="javascript:alert(1)">危险链接</a>
      <a href="https://example.com/path?x=1&amp;y=2" onclick="alert(1)">安全链接</a>
      <script>alert(1)</script>
    </p><callout><p>提示</p></callout>''')
    assert "<h1>手册</h1>" in output
    assert "<aside class=\"manual-callout\"><p>提示</p></aside>" in output
    assert 'href="https://example.com/path?x=1&amp;y=2"' in output
    assert "<script" not in output and "onclick=" not in output and "javascript:" not in output
    assert "危险链接" in output
