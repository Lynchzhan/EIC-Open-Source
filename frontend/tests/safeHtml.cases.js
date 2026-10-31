// 浏览器 DOM 回归源码，当前未执行。由后续隔离浏览器 harness 导入。
import { sanitizeRichHtml, safeContentUrl } from '../src/utils/safeHtml.js';

export const cases = [
  { name: '保留代码块', html: '<pre><code class="language-js">&lt;script&gt;literal&lt;/script&gt;</code></pre>', selector: 'pre code', text: '<script>literal</script>' },
  { name: '保留正常链接和表格', html: '<table><tr><td>文本</td></tr></table><a href="https://example.invalid">链接</a>', selector: 'table td', text: '文本' },
  { name: '移除事件属性', html: '<img src="/fixture.png" onerror="window.phase1Injected=true"><p onclick="window.phase1Injected=true">正文</p>', selector: 'p', text: '正文' },
  { name: '移除脚本协议', html: '<a href="javascript:window.phase1Injected=true">文本</a>', selector: 'a', forbiddenHref: true },
  { name: '移除编码后的脚本协议', html: '<a href="jav&#x61;script:alert(1)">文本</a>', selector: 'a', forbiddenHref: true },
  { name: '移除命名空间和嵌入内容', html: '<svg onload="window.phase1Injected=true"></svg><iframe srcdoc="<script>bad</script>"></iframe><p>正文</p>', selector: 'p', text: '正文' },
  { name: '移除 clobber 属性和 style', html: '<p id="location" name="document" style="background:url(javascript:bad)">正文</p>', selector: 'p', text: '正文' },
];

export const runSafeHtmlCases = () => {
  for (const fixture of cases) {
    const root = document.createElement('div');
    root.innerHTML = sanitizeRichHtml(fixture.html);
    const selected = root.querySelector(fixture.selector);
    if (!selected || (fixture.text && selected.textContent !== fixture.text)) throw new Error(fixture.name);
    if (fixture.forbiddenHref && selected.hasAttribute('href')) throw new Error(fixture.name);
    if (root.querySelector('script,iframe,svg,math,object,embed,style,form')) throw new Error(fixture.name);
    for (const element of root.querySelectorAll('*')) {
      for (const attribute of element.attributes) {
        if (/^on/i.test(attribute.name) || ['style', 'id', 'name', 'srcdoc'].includes(attribute.name)) throw new Error(fixture.name);
      }
    }
    if (sanitizeRichHtml(root.innerHTML) !== root.innerHTML) throw new Error('净化不幂等: ' + fixture.name);
  }
  for (const url of ['javascript:bad', 'data:text/html,bad', 'file:///fixture', 'vbscript:bad']) {
    if (safeContentUrl(url)) throw new Error('危险 URL 被接受');
  }
  return { passed: cases.length, scope: 'DOM fixture only, not an application E2E result' };
};
