const HTML_NAMESPACE = 'http://www.w3.org/1999/xhtml';
const ALLOWED_TAGS = new Set([
  'a', 'b', 'blockquote', 'br', 'code', 'del', 'details', 'div', 'em',
  'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr', 'i', 'img', 'kbd', 'li',
  'mark', 'ol', 'p', 'pre', 's', 'samp', 'small', 'span', 'strong',
  'sub', 'summary', 'sup', 'table', 'tbody', 'td', 'tfoot', 'th', 'thead',
  'tr', 'u', 'ul',
]);
const DROP_CONTENT = new Set([
  'base', 'button', 'embed', 'form', 'iframe', 'input', 'link', 'math',
  'meta', 'noscript', 'object', 'script', 'select', 'style', 'svg',
  'template', 'textarea',
]);

export const safeContentUrl = (value) => {
  const text = String(value ?? '').trim();
  if (!text || /[\u0000-\u001f\u007f]/.test(text)) return '';
  try {
    const url = new URL(text, window.location.href);
    if (!['http:', 'https:', 'mailto:', 'tel:'].includes(url.protocol)) return '';
    if (url.username || url.password) return '';
    return url.href;
  } catch {
    return '';
  }
};

// 重建允许的 HTML 节点，不复制事件、style、id/name 或任何未知属性。
export const sanitizeRichHtml = (value) => {
  // template 中的输入保持惰性；新节点也在无浏览上下文的文档中构造。
  const inertDocument = document.implementation.createHTMLDocument('');
  const source = inertDocument.createElement('template');
  source.innerHTML = String(value ?? '').slice(0, 1000000);
  const result = inertDocument.createElement('div');
  let remainingNodes = 20000;
  const copy = (node, parent, depth) => {
    if (remainingNodes-- <= 0 || depth > 80) return;
    if (node.nodeType === Node.TEXT_NODE) {
      parent.appendChild(inertDocument.createTextNode(node.textContent || ''));
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    const tag = node.localName.toLowerCase();
    if (node.namespaceURI !== HTML_NAMESPACE || DROP_CONTENT.has(tag)) return;
    if (!ALLOWED_TAGS.has(tag)) {
      for (const child of node.childNodes) {
        if (remainingNodes <= 0) break;
        copy(child, parent, depth + 1);
      }
      return;
    }
    const element = inertDocument.createElement(tag);
    if (node.hasAttribute('title')) element.setAttribute('title', node.getAttribute('title'));
    const classes = (node.getAttribute('class') || '').split(/\s+/).filter(
      (name) => /^(?:language-[a-z0-9_-]+|hljs(?:-[a-z0-9_-]+)?)$/i.test(name),
    );
    if (classes.length) element.className = classes.join(' ');
    if (tag === 'a') {
      const href = safeContentUrl(node.getAttribute('href'));
      if (href) element.setAttribute('href', href);
      element.setAttribute('target', '_blank');
      element.setAttribute('rel', 'noopener noreferrer');
    }
    if (tag === 'img') {
      const src = safeContentUrl(node.getAttribute('src'));
      if (!src || !/^https?:/i.test(src)) return;
      element.setAttribute('src', src);
      element.setAttribute('alt', node.getAttribute('alt') || '');
      element.setAttribute('loading', 'lazy');
      element.setAttribute('referrerpolicy', 'no-referrer');
    }
    if (tag === 'td' || tag === 'th') {
      for (const attribute of ['colspan', 'rowspan']) {
        const number = Number(node.getAttribute(attribute));
        if (Number.isInteger(number) && number > 0 && number <= 100) {
          element.setAttribute(attribute, String(number));
        }
      }
    }
    if (tag === 'ol') {
      const start = node.getAttribute('start');
      if (start && /^-?\d{1,6}$/.test(start)) element.setAttribute('start', start);
    }
    for (const child of node.childNodes) {
      if (remainingNodes <= 0) break;
      copy(child, element, depth + 1);
    }
    parent.appendChild(element);
  };
  for (const child of source.content.childNodes) {
    if (remainingNodes <= 0) break;
    copy(child, result, 0);
  }
  return result.innerHTML;
};
