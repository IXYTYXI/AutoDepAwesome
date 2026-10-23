import importlib.util
from pathlib import Path
from xml.etree import ElementTree


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


def test_feishu_snapshot_renderer_preserves_navigation_lists_and_safe_links():
    render = renderer()
    output = render('''<title>目录</title><h1>当前可用</h1><ul><li>
      <a href="https://guanghe.feishu.cn/docx/current">当前手册</a>
    </li></ul>''')
    assert '<ul><li>' in output
    assert 'href="https://guanghe.feishu.cn/docx/current"' in output
    assert '</li></ul>' in output


def test_protocol_examples_preserve_yaml_whitespace_and_local_section_links():
    path = Path(__file__).resolve().parents[1] / 'scripts' / 'render_feishu_protocol.py'
    spec = importlib.util.spec_from_file_location('render_feishu_protocol', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = ElementTree.fromstring('''<root>
      <p><a href="https://guanghe.feishu.cn/docx/DSUwdxpXhot6fQx2YticNCtWnHe#doxcnExample">完整 YAML</a></p>
      <h2 id="doxcnExample">清单示例</h2>
      <pre lang="yaml" caption="awesome.yaml"><code>services:<br/>  web:<br/>    env: {LABEL: "a &amp; b"}</code></pre>
    </root>''')
    output = ''.join(module.render(block) for block in root)
    assert '<a href="#doxcnExample">完整 YAML</a>' in output
    assert '<h3 id="doxcnExample">清单示例</h3>' in output
    assert 'services:\n  web:\n    env: {LABEL: &quot;a &amp; b&quot;}' in output
    assert 'class="manual-code-caption">awesome.yaml' in output


def test_handbook_code_preserves_copyable_newlines_and_keyboard_scroll():
    output = renderer()('<pre lang="json"><code>{<br/>  "x": 1<br/>}</code></pre>')
    assert '{\n  &quot;x&quot;: 1\n}' in output
    assert 'tabindex="0"' in output
