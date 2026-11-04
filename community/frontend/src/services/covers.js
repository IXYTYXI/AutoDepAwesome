const escape = (s) =>
  String(s ?? "").replace(
    /[&<>\"']/g,
    (c) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '\"': "&quot;",
        "'": "&#39;",
      })[c],
  );
const previewKinds = {
  research: "research",
  content: "content",
  meeting: "meeting",
  code: "code",
  sheet: "sheet",
  review: "review",
};
function preview(c) {
  let validImage = /^data:image\/(png|jpeg|webp);base64,/.test(c.image || "");
  if (!validImage && c.image) {
    try {
      validImage = ["http:", "https:"].includes(new URL(c.image).protocol);
    } catch {}
  }
  if (validImage)
    return `<div class="work-cover uploaded"><img src="${escape(c.image)}" alt="${escape(c.title)}的成果截图" loading="lazy"></div>`;
  if (c.remote) {
    const type = c.product_type || "CASE",
      kind =
        {
          "Web App": "app",
          "AI Agent": "agent",
          Skill: "skill",
          工作区: "workspace",
        }[type] || "case";
    return `<div class="work-cover case-cover case-cover-${kind}" aria-hidden="true"><div class="case-cover-top"><span>OWS / COLLECTION</span><span>${escape(c.number)}</span></div><div class="case-art"><i></i><i></i><i></i><b>${kind === "skill" ? "⌘" : kind === "agent" ? "✳" : ""}</b></div><strong class="case-cover-type">${escape(type)}</strong><div class="case-cover-bottom"><span>${escape(c.category)}</span></div></div>`;
  }
  const kind = previewKinds[c.id] || "placeholder";
  const screens = {
    research: `<div class="screen-toolbar"><i></i><i></i><i></i><span>访谈主题分析</span></div><div class="research-board"><div class="board-sidebar"><b>研究项目</b><span class="on">主题归纳</span><span>访谈记录</span><span>研究结论</span></div><div class="board-main"><small>用户研究 / 主题归纳</small><strong>从访谈到洞察</strong><div class="note-grid"><div><b>使用场景</b><span>原始记录 / 主题</span><i></i><i></i></div><div><b>主要问题</b><span>观点 / 对应证据</span><i></i><i></i></div><div><b>待验证</b><span>假设 / 下一步</span><i></i><i></i></div></div></div></div>`,
    content: `<div class="screen-toolbar"><i></i><i></i><i></i><span>内容编辑器</span></div><div class="editor-preview"><div class="outline"><small>文档大纲</small><span>01 主题</span><span>02 素材</span><span>03 初稿</span></div><div class="paper-preview"><small>内容策划 / 初稿</small><strong>一份内容大纲</strong><i></i><i></i><h4>01 / 核心观点</h4><i></i><i></i><div class="paper-highlight">补充来源与案例</div></div></div>`,
    meeting: `<div class="screen-toolbar"><i></i><i></i><i></i><span>会议行动项</span></div><div class="meeting-preview"><small>会议纪要 / 行动清单</small><strong>本周待办</strong><div><b class="check-box">✓</b><span>整理讨论结论<small>会议记录</small></span><em>已确认</em></div><div><b class="check-box"></b><span>补充需求边界<small>产品团队</small></span><em>待跟进</em></div><div><b class="check-box"></b><span>同步下一步计划<small>项目协作</small></span><em>待跟进</em></div></div>`,
    code: `<div class="screen-toolbar"><i></i><i></i><i></i><span>review.ts</span></div><div class="code-preview"><small>CHANGE REVIEW</small><strong>提交前检查</strong><div><em>01</em><span>async function <b>review</b>(changes) {</span></div><div><em>02</em><span>  const result = <b>await</b> check(changes);</span></div><div><em>03</em><span>  return result;</span></div><div><em>04</em><span>}</span></div><p>边界条件 <span>·</span> 异常处理 <span>·</span> 测试覆盖</p></div>`,
    sheet: `<div class="screen-toolbar"><i></i><i></i><i></i><span>数据整理</span></div><div class="sheet-preview"><small>数据表 / 字段映射</small><strong>跨表数据核对</strong><div class="mini-sheet"><span></span><span>A</span><span>B</span><span>C</span><span>1</span><b>原始字段</b><b>目标字段</b><b>校验</b><span>2</span><span>name</span><span>姓名</span><span class="sheet-ok">匹配</span><span>3</span><span>team</span><span>部门</span><span class="sheet-ok">匹配</span><span>4</span><span>date</span><span>日期</span><span class="sheet-check">待确认</span></div></div>`,
    review: `<div class="screen-toolbar"><i></i><i></i><i></i><span>需求评审</span></div><div class="review-preview"><small>PRODUCT / REVIEW</small><strong>评审检查清单</strong><div class="review-step"><span>场景</span><i></i><span>边界</span><i></i><span>验收</span></div><div class="review-note"><b>待确认问题</b><i></i><i></i><i></i></div></div>`,
    placeholder: `<div class="placeholder-preview"><span><svg class="ui-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><use href="assets/icons/lucide.svg#image"></use></svg></span><strong>${escape(c.category)}</strong><small>暂无成果截图</small></div>`,
  };
  return `<div class="work-cover cover-${kind}"><div class="preview-screen" aria-hidden="true">${screens[kind]}</div><span class="preview-label">${kind === "placeholder" ? "待上传封面" : "示意预览"}</span></div>`;
}
export { preview };
