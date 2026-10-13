/* 统一图标系统（内联 SVG，无外部依赖、无构建步骤）
 *
 * 设计约定（所有图标一致，这是「看起来像同一个产品」的关键）：
 *   - 24×24 viewBox，stroke 用 currentColor（自动跟随文字颜色）
 *   - 线宽 1.7，圆头圆角；只用描边不填充，跟文字同一套视觉重量
 *   - 尺寸用 CSS 控制（.ic 默认 16px），不要写死 width/height
 *
 * 用法：
 *   1) 静态标记：<i class="ic" data-icon="chat"></i>（load 时 paintIcons() 自动填充）
 *   2) 动态拼接：`<span class="ic-wrap">${icon('check')}</span>`
 *   3) 图标按钮：<button class="icon-btn">${icon('refresh')}</button>
 *   4) 图标+文字：${icon('play')}<span>开始</span>
 */
(function () {
  // 每个图标只写 <path>/<circle> 这些子元素，外层 svg 统一由 icon() 包
  const P = {
    // —— 导航 / 产品概念
    chat: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
    stethoscope:
      '<path d="M6 3v7a4 4 0 0 0 8 0V3"/><path d="M10 14v2.2a3 3 0 0 0 6 0V14"/>' +
      '<circle cx="16" cy="11.4" r="2.6"/><path d="M6 3h1.6M12.4 3H14"/>',
    users:
      '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/>' +
      '<path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    chart: '<path d="M3 3v18h18"/><path d="M18 17V9M13 17V5M8 17v-3"/>',
    // 山区问诊场景：两座山峰 + 山脊（与 stethoscope 同一套描边风格）
    mountain:
      '<path d="M2.5 19.5 9 8.5l4 6.4"/><path d="M9 19.5 14.6 9.8l6.9 9.7H2.5"/>' +
      '<path d="M19.4 6.2h2.1M20.45 5.15v2.1"/>',
    sliders:
      '<path d="M4 6h8M18 6h2M4 12h4M14 12h6M4 18h8M18 18h2"/>' +
      '<circle cx="14" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="14" cy="18" r="2"/>',
    globe: '<circle cx="12" cy="12" r="9"/><path d="M3.4 9h17.2M3.4 15h17.2"/>' +
      '<path d="M12 3a14 14 0 0 0 0 18 14 14 0 0 0 0-18Z"/>',
    sparkles:
      '<path d="M12 3.2l1.75 4.55L18.3 9.5l-4.55 1.75L12 15.8l-1.75-4.55L5.7 9.5l4.55-1.75L12 3.2Z"/>' +
      '<path d="M18.6 15l.7 1.85 1.85.7-1.85.7-.7 1.85-.7-1.85-1.85-.7 1.85-.7.7-1.85Z"/>',
    fileText:
      '<path d="M15 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7l-4-4Z"/>' +
      '<path d="M15 3v4h4M9.5 12.5h5M9.5 16h3"/>',
    history:
      '<path d="M3.2 12a8.8 8.8 0 1 0 2.6-6.2L3.2 8.4"/><path d="M3.2 4.4v4h4"/>' +
      '<path d="M12 8.3V12l2.9 1.7"/>',
    messageSquare: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>' +
      '<path d="M8.5 9.5h7M8.5 12.5h4"/>',
    gauge: '<path d="M12 14.5 15.6 9"/><circle cx="12" cy="15" r="1.4"/>' +
      '<path d="M4.6 17.5a9 9 0 1 1 14.8 0"/>',
    database: '<ellipse cx="12" cy="6" rx="7.5" ry="3.2"/><path d="M4.5 6v12c0 1.8 3.4 3.2 7.5 3.2s7.5-1.4 7.5-3.2V6"/>' +
      '<path d="M4.5 12c0 1.8 3.4 3.2 7.5 3.2s7.5-1.4 7.5-3.2"/>',

    // —— 动作
    plus: '<path d="M12 5v14M5 12h14"/>',
    x: '<path d="M6 6l12 12M18 6L6 18"/>',
    check: '<path d="M4.5 12.5l5 5 10-11"/>',
    checkSquare: '<path d="M9.5 4H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3.5"/>' +
      '<path d="M8.5 11.5l3.2 3.2L21.5 4.9"/>',
    square: '<rect x="4.5" y="4.5" width="15" height="15" rx="3"/>',
    refresh: '<path d="M20.8 12a8.8 8.8 0 1 1-2.6-6.2l2.6 2.6"/><path d="M20.8 4.4v4h-4"/>',
    'refresh-cw': '<path d="M20.8 12a8.8 8.8 0 1 1-2.6-6.2l2.6 2.6"/><path d="M20.8 4.4v4h-4"/>',
    trash: '<path d="M4 7h16M9.5 7V5.2A1.2 1.2 0 0 1 10.7 4h2.6a1.2 1.2 0 0 1 1.2 1.2V7"/>' +
      '<path d="M6.5 7l.8 12.1A1.9 1.9 0 0 0 9.2 21h5.6a1.9 1.9 0 0 0 1.9-1.9L17.5 7"/>' +
      '<path d="M10.5 11v6M13.5 11v6"/>',
    play: '<path d="M7.5 5.2 18.5 12 7.5 18.8V5.2Z"/>',
    stop: '<rect x="6" y="6" width="12" height="12" rx="2.4"/>',
    search: '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.2 4.2"/>',
    external: '<path d="M14 4h6v6"/><path d="M20 4l-8.2 8.2"/>' +
      '<path d="M18.5 14.5V18a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V7.5a2 2 0 0 1 2-2h3.5"/>',
    copy: '<rect x="9" y="9" width="11" height="11" rx="2.2"/>' +
      '<path d="M15 9V5.8A1.8 1.8 0 0 0 13.2 4H5.8A1.8 1.8 0 0 0 4 5.8v7.4A1.8 1.8 0 0 0 5.8 15H9"/>',
    send: '<path d="M20.5 3.5 3.8 10.2a.6.6 0 0 0 0 1.1l6.4 2.5 2.5 6.4a.6.6 0 0 0 1.1 0L20.5 3.5Z"/>' +
      '<path d="M20.5 3.5 10.2 13.8"/>',
    arrowUp: '<path d="M12 19.5V5"/><path d="M6 11l6-6 6 6"/>',

    // —— 状态 / 反馈
    checkCircle: '<circle cx="12" cy="12" r="8.7"/><path d="M8.2 12.2l2.6 2.6 5-5.4"/>',
    xCircle: '<circle cx="12" cy="12" r="8.7"/><path d="M9.2 9.2l5.6 5.6M14.8 9.2l-5.6 5.6"/>',
    alert: '<path d="M12 4.6 3.3 19.2h17.4L12 4.6Z"/><path d="M12 10v4M12 16.8v.2"/>',
    info: '<circle cx="12" cy="12" r="8.7"/><path d="M12 11v5.2M12 7.8v.2"/>',
    loader: '<path d="M12 3.5v4M12 16.5v4M3.5 12h4M16.5 12h4M6 6l2.8 2.8M15.2 15.2 18 18M18 6l-2.8 2.8M8.8 15.2 6 18"/>',
    clock: '<circle cx="12" cy="12" r="8.7"/><path d="M12 7.2V12l3.2 1.9"/>',
    calendar: '<rect x="3.8" y="5" width="16.4" height="15.2" rx="2.6"/><path d="M3.8 9.6h16.4M8.6 3.4v3.2M15.4 3.4v3.2"/>',
    thumbsUp:
      '<path d="M7 21V10.5l4.2-7a1.6 1.6 0 0 1 2.9 1.3L13 10h4.6a2 2 0 0 1 2 2.4l-1.3 6.4a2 2 0 0 1-2 1.6H7Z"/>' +
      '<path d="M7 10.5H4.6A1.6 1.6 0 0 0 3 12.1v7.3A1.6 1.6 0 0 0 4.6 21H7"/>',
    thumbsDown:
      '<path d="M17 3v10.5l-4.2 7a1.6 1.6 0 0 1-2.9-1.3L11 14H6.4a2 2 0 0 1-2-2.4l1.3-6.4a2 2 0 0 1 2-1.6H17Z"/>' +
      '<path d="M17 13.5h2.4A1.6 1.6 0 0 0 21 11.9V4.6A1.6 1.6 0 0 0 19.4 3H17"/>',

    // —— 内容类型
    user: '<circle cx="12" cy="8.2" r="3.9"/><path d="M4.8 20.2a7.2 7.2 0 0 1 14.4 0"/>',
    bot: '<rect x="4" y="8" width="16" height="11.4" rx="4"/><path d="M12 4.6V8"/>' +
      '<circle cx="12" cy="3.4" r="1.3"/><path d="M9 12.6v1.4M15 12.6v1.4"/>',
    staff: '<circle cx="12" cy="8" r="3.8"/><path d="M5 20.2a7 7 0 0 1 14 0"/><path d="M4 4.6l16 0"/>',
    doctor: '<circle cx="12" cy="7.6" r="3.6"/><path d="M5.4 20.4a6.8 6.8 0 0 1 13.2 0"/>' +
      '<path d="M12 12.6v4M10 14.6h4"/>',
    family: '<circle cx="9" cy="8" r="3.4"/><path d="M3.4 19.6a5.8 5.8 0 0 1 11.2 0"/>' +
      '<circle cx="17.6" cy="10.4" r="2.6"/><path d="M14.6 19.6a4.6 4.6 0 0 1 6.4-3.5"/>',
    paperclip: '<path d="M20 11.5 12.6 19a4.9 4.9 0 0 1-7-7l7.5-7.4a3.4 3.4 0 0 1 4.8 4.8l-7.4 7.4a1.9 1.9 0 0 1-2.7-2.7l6.6-6.6"/>',
    image: '<rect x="3.5" y="4.5" width="17" height="15" rx="2.6"/><circle cx="9" cy="10" r="1.8"/>' +
      '<path d="M4.2 17.4 9.6 12.4l3.5 3.2 3-2.6 4.1 3.7"/>',
    file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5Z"/><path d="M14 3v5h5"/>',
    bell: '<path d="M18 9a6 6 0 1 0-12 0c0 5.2-2 6.6-2 6.6h16S18 14.2 18 9Z"/>' +
      '<path d="M10.3 19a1.9 1.9 0 0 0 3.4 0"/>',
    bulb: '<path d="M9.4 17.6h5.2"/><path d="M10 21h4"/>' +
      '<path d="M12 3a5.6 5.6 0 0 1 3.6 9.9c-.7.6-1 1.2-1.1 2H9.5c0-.8-.4-1.4-1.1-2A5.6 5.6 0 0 1 12 3Z"/>',
    eye: '<path d="M2.5 12S6 5.8 12 5.8 21.5 12 21.5 12 18 18.2 12 18.2 2.5 12 2.5 12Z"/>' +
      '<circle cx="12" cy="12" r="2.9"/>',
    target: '<circle cx="12" cy="12" r="8.6"/><circle cx="12" cy="12" r="4.4"/><circle cx="12" cy="12" r="1"/>',
    shield: '<path d="M12 3.4 5 6v6c0 4.2 2.9 7.4 7 8.6 4.1-1.2 7-4.4 7-8.6V6l-7-2.6Z"/>' +
      '<path d="M9 12.2l2.2 2.2 4.1-4.4"/>',
    activity: '<path d="M3 12.5h4l2.6-6 4 12 2.5-6H21"/>',
    zap: '<path d="M13.4 3 5.6 13.4h5.4L10.2 21l8-10.6h-5.5L13.4 3Z"/>',
    brain: '<path d="M12 5.4a3 3 0 0 0-5.6-1A2.9 2.9 0 0 0 4 7.2a3 3 0 0 0-.4 5.4A3 3 0 0 0 6 17.6a3 3 0 0 0 6 .6V5.4Z"/>' +
      '<path d="M12 5.4a3 3 0 0 1 5.6-1A2.9 2.9 0 0 1 20 7.2a3 3 0 0 1 .4 5.4A3 3 0 0 1 18 17.6a3 3 0 0 1-6 .6V5.4Z"/>',
    listChecks: '<path d="M4 6.6l1.6 1.6L8.4 5"/><path d="M4 13.4l1.6 1.6L8.4 12"/>' +
      '<path d="M4 20l1.4-1.4L7 20"/>' + '<path d="M11.5 6.8h9M11.5 13.6h9M11.5 20.4h9"/>',
    layers: '<path d="M12 3.4 3.4 8 12 12.6 20.6 8 12 3.4Z"/><path d="M3.4 12.6 12 17.2l8.6-4.6"/>' +
      '<path d="M3.4 16.8 12 21.4l8.6-4.6"/>',
    branches: '<circle cx="6.5" cy="6" r="2.6"/><circle cx="6.5" cy="18" r="2.6"/><circle cx="17.5" cy="12" r="2.6"/>' +
      '<path d="M9.1 6h3.2a2.6 2.6 0 0 1 2.6 2.6v.8M9.1 18h3.2a2.6 2.6 0 0 0 2.6-2.6v-.8"/>',
    chevronDown: '<path d="M6 9.5l6 6 6-6"/>',
    chevronRight: '<path d="M9.5 6l6 6-6 6"/>',
    chevronLeft: '<path d="M14.5 6l-6 6 6 6"/>',
    arrowRight: '<path d="M4.5 12h15"/><path d="M13.5 6l6 6-6 6"/>',
    filter: '<path d="M4 5.5h16l-6.2 7.2v5.6l-3.6 1.8v-7.4L4 5.5Z"/>',
    download: '<path d="M12 4v11"/><path d="M7.5 10.5 12 15l4.5-4.5"/><path d="M4.5 19.5h15"/>',
    cpu: '<rect x="6.5" y="6.5" width="11" height="11" rx="2.4"/>' +
      '<path d="M10 3.5v3M14 3.5v3M10 17.5v3M14 17.5v3M3.5 10h3M3.5 14h3M17.5 10h3M17.5 14h3"/>',
    scissors: '<circle cx="6" cy="6" r="2.6"/><circle cx="6" cy="18" r="2.6"/>' +
      '<path d="M19.5 4.5 8.6 15.4M14 14.2l5.5 5.3M8.6 8.6 12 12"/>',
  };

  function icon(name, opts) {
    const o = typeof opts === 'number' ? { size: opts } : (opts || {});
    const body = P[name] || P.info;
    const cls = 'ic' + (o.cls ? ' ' + o.cls : '');
    const size = o.size ? ` style="--ic:${o.size}px"` : '';
    const extra = o.title ? ` role="img" aria-label="${o.title}"` : ' aria-hidden="true"';
    return `<svg class="${cls}"${size} viewBox="0 0 24 24" fill="none" stroke="currentColor"` +
      ` stroke-width="${o.stroke || 1.7}" stroke-linecap="round" stroke-linejoin="round"${extra}>${body}</svg>`;
  }

  // 把 <i data-icon="xxx"> 换成真图标（静态 HTML 用，保持可读性）
  function paintIcons(root) {
    (root || document).querySelectorAll('[data-icon]').forEach((el) => {
      const name = el.dataset.icon;
      el.outerHTML = icon(name, { size: el.dataset.size || 0, cls: el.className.replace(/\bic\b/, '').trim() });
    });
  }

  window.ICONS = icon;
  window.icon = icon;
  window.paintIcons = paintIcons;
  document.addEventListener('DOMContentLoaded', () => paintIcons(document));
})();
