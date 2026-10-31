import http from './http';

export const PREVIEW_MAX_BYTES = 16 * 1024 * 1024;

export function fileIdFromReference(value) {
  const raw = value && typeof value === 'object' ? (value.id ?? value.fileId ?? value.url) : value;
  const text = String(raw ?? '');
  if (/^[1-9]\d{0,18}$/.test(text)) return text;
  try {
    const url = new URL(text, window.location.origin);
    if (url.origin !== window.location.origin || url.hash) return '';
    if (!['/api/File/GetFile', '/api/File/GetFilePath', '/api/File/GetImage'].includes(url.pathname)) return '';
    const key = url.pathname === '/api/File/GetImage' ? 'fileId' : 'fileid';
    if ([...url.searchParams.keys()].some((name) => name !== key) || url.searchParams.getAll(key).length !== 1) return '';
    const id = url.searchParams.get(key);
    return /^[1-9]\d{0,18}$/.test(id ?? '') ? id : '';
  } catch { return ''; }
}

export async function loadPrivateFile(reference, options = {}) {
  const id = fileIdFromReference(reference);
  if (!id) throw new Error('文件引用无效，请重新选择受控文件');
  const maxBytes = options.maxBytes ?? PREVIEW_MAX_BYTES;
  const config = { signal: options.signal, timeout: 30000 };
  const response = await http.get('/api/File/GetById', { ...config, params: { Id: id } });
  const metadata = response?.data;
  if (response?.code !== 200 || String(metadata?.id) !== id) throw new Error('文件信息不可用');
  if (!Number.isFinite(metadata.size) || metadata.size < 0 || metadata.size > maxBytes) {
    throw new Error('文件超过当前预览大小限制，请使用受控下载');
  }
  const blob = await http.get('/api/File/GetFile', { ...config, params: { fileid: id }, responseType: 'blob' });
  if (!(blob instanceof Blob) || blob.size > maxBytes) throw new Error('文件内容无效或超过大小限制');
  return { blob, metadata };
}

export async function inertPreviewBlob(blob, name) {
  const bytes = new Uint8Array(await blob.slice(0, 12).arrayBuffer());
  const ext = String(name).split('.').pop().toLowerCase();
  let type = '';
  if (ext === 'png' && bytes.slice(0, 8).join(',') === '137,80,78,71,13,10,26,10') type = 'image/png';
  if (['jpg', 'jpeg'].includes(ext) && bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255) type = 'image/jpeg';
  if (ext === 'gif' && new TextDecoder().decode(bytes.slice(0, 6)).match(/^GIF8[79]a$/)) type = 'image/gif';
  if (ext === 'webp' && new TextDecoder().decode(bytes.slice(0, 4)) === 'RIFF' && new TextDecoder().decode(bytes.slice(8, 12)) === 'WEBP') type = 'image/webp';
  if (ext === 'bmp' && bytes[0] === 66 && bytes[1] === 77) type = 'image/bmp';
  if (ext === 'pdf' && new TextDecoder().decode(bytes.slice(0, 5)) === '%PDF-') type = 'application/pdf';
  if (!type) throw new Error('文件格式不符合受控预览要求，请下载后查看');
  return new Blob([blob], { type });
}

export function savePrivateBlob(blob, filename) {
  if (!(blob instanceof Blob)) throw new Error('下载内容无效');
  const url = URL.createObjectURL(new Blob([blob], { type: 'application/octet-stream' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = String(filename || 'download').replace(/[\\/\u0000-\u001f]/g, '_');
  try {
    document.body.appendChild(link);
    link.click();
  } finally {
    link.remove();
    // 给浏览器一次消费下载链接的机会。
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}
