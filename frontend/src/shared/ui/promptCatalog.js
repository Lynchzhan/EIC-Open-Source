export function promptImportPayload(preview, selectedIds) {
  const selected = [...new Set(selectedIds)];
  if (!preview?.previewToken || !selected.length) throw new Error('请先预览并选择模板');
  const available = new Set(preview.entries.filter((item) => item.selectable).map((item) => item.templateId));
  if (selected.some((id) => !available.has(id))) throw new Error('所选模板已编辑或不可更新');
  return { catalogVersion: preview.catalogVersion, previewToken: preview.previewToken, templateIds: selected };
}

export function promptTextDiff(before = '', after = '') {
  const oldLines = before.split('\n');
  const newLines = after.split('\n');
  const scores = Array.from({ length: oldLines.length + 1 }, () => Array(newLines.length + 1).fill(0));
  for (let i = oldLines.length - 1; i >= 0; i -= 1) {
    for (let j = newLines.length - 1; j >= 0; j -= 1) {
      scores[i][j] = oldLines[i] === newLines[j] ? scores[i + 1][j + 1] + 1 : Math.max(scores[i + 1][j], scores[i][j + 1]);
    }
  }
  const output = [];
  let i = 0; let j = 0;
  while (i < oldLines.length || j < newLines.length) {
    if (i < oldLines.length && j < newLines.length && oldLines[i] === newLines[j]) {
      output.push({ type: 'same', text: `  ${oldLines[i++]}` }); j += 1;
    } else if (i < oldLines.length && (j === newLines.length || scores[i + 1][j] >= scores[i][j + 1])) {
      output.push({ type: 'removed', text: `- ${oldLines[i++]}` });
    } else {
      output.push({ type: 'added', text: `+ ${newLines[j++]}` });
    }
  }
  return output;
}
