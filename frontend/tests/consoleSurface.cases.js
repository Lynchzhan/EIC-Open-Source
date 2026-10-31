import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const read = (name) => fs.readFileSync(path.join(root, 'src', name), 'utf8');
const css = read('shared/styles/console-surface.css');
const rules = Array.from(css.matchAll(/([^{}]+)\{([^{}]*)\}/g));
const rule = (selector, declaration) => rules.some(([, selectors, body]) =>
  selectors.includes(selector) && declaration.test(body));

assert.ok(read('main.js').includes("import './shared/styles/console-surface.css'"));
assert.ok(read('app/layouts/ConsoleLayout.vue').includes('class="console-workspace"'));
assert.ok(!css.includes('.content-wrapper '), 'new layout must not depend on retired wrapper');
const headings = ['pages/DashboardPage.vue', 'components/ai/AgentWorkspace.vue',
  'pages/ai/KnowledgeBaseManagement.vue', 'pages/ai/RecallTest.vue',
  'pages/ai/MyAITasks.vue', 'pages/ai/ObservabilityDashboard.vue'];
for (const name of headings) {
  assert.match(read(name), /<header class="[^"]*eic-page-heading[^"]*"/, name);
}
assert.ok(rule('.console-workspace .eic-page-heading', /text-align:\s*center/));
assert.ok(rule('.console-workspace .eic-page-heading', /flex-direction:\s*column/));
assert.ok(rule('.console-workspace .eic-page-heading', /padding:/));
assert.ok(rule('.ant-table-thead > tr > th', /color:\s*var\(--eic-text\)\s*!important/));
assert.ok(rule('.ant-table-tbody > tr > td', /background:\s*var\(--eic-surface\)\s*!important/));
assert.ok(rule('tr.ant-table-row-selected > td', /background:\s*#e4efff/));
assert.ok(rule('body .ant-modal .ant-modal-content', /background:\s*var\(--eic-surface\)/));
assert.ok(rule('.ant-btn-primary', /color:\s*#fff\s*!important/));
assert.doesNotMatch(read('css/MyTable.css'), /color:\s*(white|aliceblue|rgba\(255)/);
assert.ok(rule('.ant-btn.ant-btn-primary:not(.ant-btn-background-ghost):not(.ant-btn-dangerous):not(:disabled)',
  /background:\s*var\(--eic-primary-dark\)\s*!important/));
assert.ok(rule('.ant-card .ant-card-head', /flex-shrink:\s*0/));
assert.ok(rule('.ant-tag:is(.ant-tag-success, .ant-tag-green)', /color:\s*var\(--eic-success-text\)/));
assert.ok(rule('.hero-kicker', /color:\s*var\(--eic-primary-dark\)/));
assert.ok(rule('.user-management-container, .user-profile-container',
  /justify-content:\s*center/));
assert.ok(rule('.ant-btn:disabled:is(.ant-btn-dangerous, .danger)',
  /color:\s*var\(--eic-text-secondary\)/));
assert.ok(rule('.ant-btn.ant-btn-primary.ant-btn-dangerous',
  /background:\s*var\(--eic-danger\)/));
assert.ok(rule('.layout-container > :is(.left-panel, .right-panel)', /flex:\s*0 0 auto/));
assert.ok(rule('.org-chart-container', /min-height:\s*0/));
assert.ok(rule('.org-node-info', /flex-direction:\s*column/));
assert.ok(rule('.org-node-actions', /opacity:\s*1/));
assert.ok(rule('.org-node-name', /white-space:\s*nowrap/));
assert.ok(rule('body .ant-modal .ant-select-selection-placeholder',
  /color:\s*var\(--eic-muted\)\s*!important/));
assert.ok(rule('body .ant-modal .ant-picker-input > input::placeholder',
  /color:\s*var\(--eic-muted\)\s*!important/));
assert.ok(rule('body .ant-drawer .ant-select-selection-placeholder', /opacity:\s*1/));

const tokens = read('shared/styles/tokens.css');
const token = (name) => tokens.match(new RegExp(`${name}:\\s*(#[0-9a-f]{6})`, 'i'))[1];
const luminance = (hex) => {
  const rgb = hex.slice(1).match(/../g).map((x) => parseInt(x, 16) / 255)
    .map((x) => x <= .04045 ? x / 12.92 : ((x + .055) / 1.055) ** 2.4);
  return rgb[0] * .2126 + rgb[1] * .7152 + rgb[2] * .0722;
};
const contrast = (a, b) => {
  const values = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (values[0] + .05) / (values[1] + .05);
};
for (const name of ['--eic-text', '--eic-text-secondary', '--eic-muted', '--eic-primary-dark']) {
  assert.ok(contrast(token(name), '#ffffff') >= 4.5, name);
  assert.ok(contrast(token(name), '#edf4fc') >= 4.5, name + ' table header');
}
assert.ok(contrast('#ffffff', token('--eic-primary-dark')) >= 4.5);
console.log(JSON.stringify({ passed: 42, scope: 'layout-css-contract-and-token-contrast',
  browserRequired: true }));
