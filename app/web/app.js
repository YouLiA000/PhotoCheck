'use strict';

const $ = (sel) => document.querySelector(sel);

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

const state = {
  samples: [],
  sourceId: null,
  title: '',
  width: 0,
  height: 0,
  detections: [],
  selected: new Set(),
  manual: [],
  result: null,
  view: 'detect',
  autoRun: false,
  manualMode: false,
  detected: false,   // 这张图跑过检测没有——决定脚注要不要出现
  analysisId: null,
  revision: 1,
  methodNote: '',
};

/* ---------------- 基础 ---------------- */

async function api(path, options = {}) {
  const res = await fetch(path, options);
  const text = await res.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { error: text }; }
  if (!res.ok) throw new Error(data.error || `请求失败（${res.status}）`);
  return data;
}

let toastTimer = null;
function toast(message, isError = false) {
  const el = $('#toast');
  el.textContent = message;
  el.className = `toast show${isError ? ' error' : ''}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = 'toast'; }, 3200);
}

function busy(on, text = '处理中…') {
  const wrap = $('#wrap');
  const old = wrap.querySelector('.loading');
  if (old) old.remove();
  if (!on) return;
  const div = document.createElement('div');
  div.className = 'loading';
  div.innerHTML = `<span class="spinner"></span><span>${text}</span>`;
  wrap.appendChild(div);
}

/* ---------------- 状态灯 ---------------- */

async function refreshStatus() {
  try {
    const st = await api('/api/status');
    const engine = $('#pillEngine');
    const tools = $('#pillTools');
    const detectors = st.detectors || {};
    if (st.warm) {
      engine.textContent = `引擎就绪 · OCR ${detectors.ocr || '?'} / 码 ${detectors.qr || '?'} / 人脸 ${detectors.face || '?'}`;
      engine.className = 'pill';
    } else {
      engine.textContent = '引擎预热中…';
      engine.className = 'pill muted';
      setTimeout(refreshStatus, 1500);
    }
    if (st.exiftool) {
      tools.textContent = 'ExifTool 就位';
      tools.className = 'pill';
    } else {
      tools.textContent = 'ExifTool 未就绪（无法处理图片）';
      tools.className = 'pill warn';
    }
  } catch (err) {
    setTimeout(refreshStatus, 2000);
  }
}

/* ---------------- 图库 ---------------- */

async function loadSamples() {
  const { samples } = await api('/api/samples');
  state.samples = samples;
  const box = $('#gallery');
  box.innerHTML = '';
  samples.forEach((s) => {
    const el = document.createElement('div');
    el.className = 'thumb';
    el.dataset.id = s.id;
    el.innerHTML = `<img src="/api/source-image?source=sample:${esc(s.id)}" alt="">
                    <span title="${esc(s.hint)}">${esc(s.title)}</span>`;
    el.addEventListener('click', () => choose(`sample:${s.id}`, s.title));
    box.appendChild(el);
  });
}

function markActive(sourceId) {
  document.querySelectorAll('.thumb').forEach((el) => {
    el.classList.toggle('active', `sample:${el.dataset.id}` === sourceId);
  });
}

/* ---------------- 选图与检测 ---------------- */

async function choose(sourceId, title) {
  state.sourceId = sourceId;
  state.title = title || '已上传的照片';
  state.result = null;
  state.manual = [];
  state.detected = false;
  state.analysisId = null;
  state.revision = 1;
  state.methodNote = '';
  setView('detect');
  $('#report').hidden = true;
  $('#tabs').hidden = true;
  markActive(sourceId);
  await detect();
}

async function detect() {
  if (!state.sourceId) return;
  busy(true, '正在检测（首次会加载模型，最长约 30 秒）…');
  try {
    const data = await api('/api/detect', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source_id: state.sourceId }),
    });
    state.width = data.width;
    state.height = data.height;
    state.analysisId = data.analysis_id;
    state.detections = data.detections.filter((d) => d.bbox);  // 元数据没有框
    state.selected = new Set(state.detections.map((d) => d.id));
    state.detected = true;

    $('#stageTitle').textContent = state.title;
    const actionable = data.actionable_count;
    $('#stageSub').textContent =
      `${data.width}×${data.height}　检出 ${actionable} 项可处置　${(data.total_ms / 1000).toFixed(1)} 秒`;
    $('#metaNote').textContent = data.meta_count
      ? `另有 ${data.meta_count} 条拍摄信息（GPS／时间／机型等），不出现在清单里——它们始终整图清除。`
      : '这张图没有拍摄信息（EXIF）。';
    $('#timing').textContent = Object.entries(data.timings_ms || {})
      .map(([k, v]) => `${k} ${Math.round(v)}ms`).join('　');

    renderStage();
    renderList();
    $('#btnProcess').disabled = false;
    updateHint();
    if (state.autoRun) {
      state.autoRun = false;
      setTimeout(process, 150);
    }
  } catch (err) {
    toast(err.message, true);
  } finally {
    busy(false);
  }
}

function renderStage() {
  const wrap = $('#wrap');
  const img = $('#stageImg');
  wrap.style.aspectRatio = `${state.width} / ${state.height}`;
  wrap.style.maxWidth = `calc(62vh * ${state.width} / ${state.height})`;
  img.src = `/api/source-image?source=${encodeURIComponent(state.sourceId)}&v=${Date.now()}`;
  img.hidden = false;
  $('#placeholder').hidden = true;
  drawOverlay();
}

function drawOverlay() {
  const svg = $('#overlay');
  svg.setAttribute('viewBox', `0 0 ${state.width} ${state.height}`);
  svg.innerHTML = '';
  const ns = 'http://www.w3.org/2000/svg';
  const scale = Math.max(state.width, state.height) / 700;

  const addBox = (bbox, color, dashed, label) => {
    const [x, y, w, h] = bbox;
    const rect = document.createElementNS(ns, 'rect');
    rect.setAttribute('x', x); rect.setAttribute('y', y);
    rect.setAttribute('width', w); rect.setAttribute('height', h);
    rect.setAttribute('fill', 'none');
    rect.setAttribute('stroke', color);
    rect.setAttribute('stroke-width', Math.max(2, 3 * scale));
    rect.setAttribute('vector-effect', 'non-scaling-stroke');
    rect.setAttribute('rx', 4 * scale);
    if (dashed) rect.setAttribute('stroke-dasharray', `${8 * scale} ${5 * scale}`);
    svg.appendChild(rect);
    if (label) {
      const t = document.createElementNS(ns, 'text');
      t.setAttribute('x', x + 2 * scale);
      t.setAttribute('y', Math.max(16 * scale, y - 10 * scale));
      t.setAttribute('fill', color);
      t.setAttribute('font-size', 13 * scale);
      t.setAttribute('font-weight', '600');
      t.setAttribute('paint-order', 'stroke');
      t.setAttribute('stroke', '#fff');
      t.setAttribute('stroke-width', 4 * scale);
      t.textContent = label;
      svg.appendChild(t);
    }
    return rect;
  };

  state.detections.forEach((d) => {
    const on = state.selected.has(d.id);
    const color = on ? riskColor(d.risk) : '#aab1bd';
    addBox(d.bbox, color, !on, on ? subtypeLabel(d) : '');
  });
  state.manual.forEach((m, i) => {
    const rect = addBox(m.bbox, '#7c3aed', true, '手动');
    // 点一下直接删掉这个补框：拖歪了不该逼用户清空重来
    rect.style.cursor = 'pointer';
    rect.addEventListener('pointerdown', (ev) => ev.stopPropagation());
    rect.addEventListener('click', (ev) => {
      ev.stopPropagation();
      state.manual.splice(i, 1);
      bumpRevision('补框已变化，旧复检报告已失效');
      drawOverlay();
      toast('已删除一个补框');
    });
  });
  updateManualTools();
}

/* 补框相关的按钮只在有补框时出现——平时不占位置 */
function updateManualTools() {
  const n = state.manual.length;
  $('#btnUndo').hidden = !n;
  $('#btnClearManual').hidden = !n;
  const btn = $('#btnManual');
  btn.textContent = n ? `手动补框 · ${n}` : '手动补框';
  btn.classList.toggle('has', !!n);
}

function riskColor(risk) {
  if (risk >= 3) return '#dc2626';
  if (risk === 2) return '#d97706';
  return '#94a3b8';
}

function subtypeLabel(d) {
  const names = {
    phone: '手机号', idcard: '身份证', face_region: '人脸',
    qr_wifi: 'Wi-Fi 码', qr_vcard: '名片码', qr_contact: '联系人码',
    qr_wechat_pay: '收款码', qr_alipay: '支付宝码', qr_link: '链接码',
    qr_unknown: '未识别码', qr_code: '条码', qr_geo: '位置码',
  };
  return names[d.subtype] || d.subtype;
}

/* ---------------- 发现项清单 ---------------- */

function renderList() {
  const list = $('#list');
  list.innerHTML = '';
  $('#countBadge').textContent = String(state.detections.length);
  // 0 检出时更要露出这句话："没检出"和"没有敏感信息"不是一回事
  $('#listNote').hidden = !state.detected;

  if (!state.detections.length) {
    list.innerHTML = '<div class="empty">这张图没有检出可处置的隐私线索</div>';
    return;
  }

  state.detections.forEach((d) => {
    const on = state.selected.has(d.id);
    const el = document.createElement('label');
    el.className = `item risk-${d.risk} ${on ? 'on' : 'off'}`;
    const detail = d.masked_preview || d.idcard_check || d.safe_domain || d.safe_ssid || '';
    const extra = d.idcard_check === 'fail' ? '　校验位未通过' : '';
    el.innerHTML = `
      <input type="checkbox" ${on ? 'checked' : ''}>
      <div>
        <div class="k"><i class="dot"></i>${esc(subtypeLabel(d))}<span style="color:var(--ink-3);font-weight:400"> · 风险 ${d.risk}</span></div>
        <div class="v">${esc(detail)}${esc(extra)}</div>
      </div>
      <div></div>`;
    el.querySelector('input').addEventListener('change', (ev) => {
      if (ev.target.checked) state.selected.add(d.id); else state.selected.delete(d.id);
      bumpRevision('处置决定已变化，旧复检报告已失效');
      renderList(); drawOverlay();
      updateHint();
    });
    list.appendChild(el);
  });
}

function updateHint() {
  const total = state.detections.length;
  const chosen = state.selected.size;
  const choice = chosen === total
    ? `全部 ${total} 项将被擦除；元数据始终整图清除。`
    : `擦除 ${chosen} 项、保留 ${total - chosen} 项；元数据始终整图清除。`;
  const note = state.methodNote ? `${state.methodNote}　` : '';
  $('#hintLine').textContent = `${note}${choice}　计划版本 ${state.revision}。`;
}

function invalidateResult(message) {
  if (!state.result) return;
  state.result = null;
  $('#report').hidden = true;
  $('#tabs').hidden = true;
  setView('detect');
  toast(message);
}

function bumpRevision(message) {
  if (!state.detected) return;
  state.revision += 1;
  invalidateResult(message);
}

/* ---------------- 手动补框 ---------------- */

function setupManual() {
  const wrap = $('#wrap');
  let start = null;
  let preview = null;

  const toNatural = (ev) => {
    const rect = wrap.getBoundingClientRect();
    const x = (ev.clientX - rect.left) / rect.width * state.width;
    const y = (ev.clientY - rect.top) / rect.height * state.height;
    return [Math.max(0, Math.min(state.width, x)), Math.max(0, Math.min(state.height, y))];
  };

  wrap.addEventListener('pointerdown', (ev) => {
    if (!state.manualMode || !state.sourceId) return;
    ev.preventDefault();
    wrap.setPointerCapture(ev.pointerId);
    start = toNatural(ev);
    preview = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    preview.setAttribute('fill', 'rgba(124,58,237,.12)');
    preview.setAttribute('stroke', '#7c3aed');
    preview.setAttribute('stroke-dasharray', '6 4');
    preview.setAttribute('vector-effect', 'non-scaling-stroke');
    $('#overlay').appendChild(preview);
  });

  wrap.addEventListener('pointermove', (ev) => {
    if (!start || !preview) return;
    const [x, y] = toNatural(ev);
    const bbox = [Math.min(start[0], x), Math.min(start[1], y), Math.abs(x - start[0]), Math.abs(y - start[1])];
    preview.setAttribute('x', bbox[0]); preview.setAttribute('y', bbox[1]);
    preview.setAttribute('width', bbox[2]); preview.setAttribute('height', bbox[3]);
  });

  wrap.addEventListener('pointerup', (ev) => {
    if (!start) return;
    const [x, y] = toNatural(ev);
    const bbox = [
      Math.round(Math.min(start[0], x)), Math.round(Math.min(start[1], y)),
      Math.round(Math.abs(x - start[0])), Math.round(Math.abs(y - start[1])),
    ];
    start = null;
    if (preview) { preview.remove(); preview = null; }
    if (bbox[2] < 8 || bbox[3] < 8) return;
    state.manual.push({ bbox, method: $('#method').value });
    bumpRevision('补框已变化，旧复检报告已失效');
    drawOverlay();
    toast(`已补一个区域（${bbox[2]}×${bbox[3]}），点「生成脱敏副本」才会真的遮住`);
  });

  $('#btnUndo').addEventListener('click', () => {
    if (!state.manual.length) return;
    state.manual.pop();
    bumpRevision('补框已变化，旧复检报告已失效');
    drawOverlay();
    toast('已撤销最后一个补框');
  });

  $('#btnClearManual').addEventListener('click', () => {
    const n = state.manual.length;
    if (!n) return;
    state.manual = [];
    bumpRevision('补框已变化，旧复检报告已失效');
    drawOverlay();
    toast(`已清空 ${n} 个补框`);
  });
}

/* ---------------- 生成与报告 ---------------- */

async function process() {
  if (!state.sourceId) return;
  $('#btnProcess').disabled = true;
  busy(true, '正在遮盖、复检并导出（约 5–10 秒）…');
  try {
    const report = await api('/api/process', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        source_id: state.sourceId,
        selected_ids: [...state.selected],
        method: $('#method').value,
        manual_boxes: state.manual,
        analysis_id: state.analysisId,
        revision: state.revision,
      }),
    });
    state.result = report;
    state.revision = Number(report.plan_revision || state.revision);
    renderReport(report);
    showAfter(report);
    showCompare(report.output_url);
    $('#tabs').hidden = false;
    // 默认看「处理后整图」：对比视图左半边是原图，容易被误认为"没盖干净"
    setView(report.undecoded_qr && report.undecoded_qr.length ? 'detect' : 'after');
    toast('已生成脱敏副本，原图未被修改');
  } catch (err) {
    toast(err.message, true);
  } finally {
    busy(false);
    $('#btnProcess').disabled = false;
  }
}

function renderReport(rep) {
  const box = $('#report');
  box.hidden = false;

  const badge = $('#stateBadge');
  const names = {
    passed: '本次计划中待处理区域未发现残留',
    pending: '部分待确认',
    failed: '未通过',
    not_applicable: '无需处置',
  };
  badge.textContent = names[rep.disposal_state] || rep.disposal_state;
  badge.className = `state ${rep.disposal_state}`;

  const fr = rep.full_recheck || {};
  const rows = [
    ['计划区域', `${rep.plan_items} 个（擦 ${rep.decisions.redact} / 留 ${rep.decisions.retain}）`],
    ['计划版本', `revision ${rep.plan_revision}`],
    ['逐区域复检', `${rep.checks.length} 项`],
    ['残留', `${(rep.residuals || []).length} 项`],
    ['全图重扫新增', `${(fr.detections || []).length} 项`],
    ['计划外残留', `${fr.unplanned_count ?? '—'} 项`],
    ['处理耗时', `${(rep.elapsed_ms / 1000).toFixed(1)} 秒`],
  ];
  $('#reportKv').innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('');

  $('#reportChecks').innerHTML = rep.checks.map((c) => {
    const cls = c.status === 'passed' ? 'ok' : (c.status === 'pending' ? 'pend' : 'bad');
    const label = { passed: '本区域未发现残留', pending: '待确认', failed: '未通过' }[c.status] || c.status;
    const method = {
      a_qr_probe: '二维码复检',
      a_ocr_probe: '文字复检',
      a_face_probe: '人脸复检',
      solid_black_region: '整块复核',
      semantic_verifier_pending: '语义待确认',
    }[c.method] || c.method;
    const region = esc((c.region_id || '').replace('redact:', ''));
    return `<div class="check"><span>${region}　<span style="color:var(--ink-3)">${method}</span></span><span class="${cls}">${label}</span></div>`;
  }).join('');

  // 没有检出二维码时明确说一句：拍屏幕的码解不出，不会进清单，只能靠手动补框
  const hasQr = state.detections.some((d) => d.kind === 'qr');
  if (!hasQr) {
    $('#reportChecks').innerHTML +=
      '<div class="check" style="border-color:#f3ddb7;background:#fdf9f0">'
      + '<span>未检出二维码</span><span class="pend">需人工确认</span></div>'
      + '<div class="hint-line" style="padding:6px 2px 0">'
      + '拍屏幕的照片里，二维码常因摩尔纹解不出内容，因此不会出现在清单里。'
      + '如果图上确实有码，请用「手动补框」盖住它。</div>';
  }

  // 报告与图片都落在 work/outputs/run-xxxx/ 下：给出下载与定位，别让用户去猜路径
  const reports = rep.reports || {};
  const actions = $('#reportActions');
  if (reports.txt) {
    actions.hidden = false;
    $('#dlTxt').href = reports.txt;
    $('#dlJson').href = reports.json || '#';
    $('#btnReveal').onclick = async () => {
      try {
        const res = await api('/api/reveal', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ analysis_id: rep.analysis_id }),
        });
        toast(`已打开：${res.folder}`);
      } catch (err) { toast(err.message, true); }
    };
  } else {
    actions.hidden = true;
  }

  $('#reportExport').innerHTML = `
    导出文件 <code>${esc(rep.export.name.slice(0, 18))}…</code><br>
    原图 SHA-256 未变：<code>${esc((rep.export.source_sha256 || '').slice(0, 10))}…</code><br>
    ${rep.exiftool ? '已用 ExifTool 独立复核元数据' : 'ExifTool 缺失，本次结果不得交付'}`;
}

function showAfter(rep) {
  const wrap = $('#afterWrap');
  wrap.style.aspectRatio = `${state.width} / ${state.height}`;
  wrap.style.maxWidth = `calc(66vh * ${state.width} / ${state.height})`;
  $('#afterImg').src = rep.output_url;

  const redact = rep.decisions.redact;
  const missQr = (rep.undecoded_qr || []).length;
  const hasQr = state.detections.some((d) => d.kind === 'qr');
  const cap = $('#afterCap');
  if (missQr) {
    cap.className = 'after-cap warn';
    cap.innerHTML = `<b>注意</b> 有 ${missQr} 处二维码形状的区域没有被盖住`
      + `（内容解不出，可能是拍屏幕）。请切到「检测」用「手动补框」补上。`;
  } else if (!hasQr) {
    // 没有检出码 ≠ 图上没有码。拍屏幕的码解不出，检测器不会报，
    // 这时写"已遮盖 N 处"会给人"全干净了"的错觉，必须把这句话补上。
    cap.className = 'after-cap warn';
    cap.innerHTML = `<b>已遮盖 ${redact} 处</b>　·　本图未检出二维码——`
      + `若图上其实有码（拍屏幕很常见），它不会被自动处理，请用「手动补框」补上`;
  } else {
    cap.className = 'after-cap';
    cap.innerHTML = `<b>已遮盖 ${redact} 处</b>　·　原图未被修改，这是另存的副本`;
  }
}

function showCompare(outputUrl) {
  const stage = $('#cmpStage');
  stage.style.aspectRatio = `${state.width} / ${state.height}`;
  stage.style.maxWidth = `calc(68vh * ${state.width} / ${state.height})`;
  $('#cmpBefore').src = `/api/source-image?source=${encodeURIComponent(state.sourceId)}`;
  $('#cmpAfter').src = `${outputUrl}`;
  $('#cmpRange').value = 50;
  applyClip(50);
}

function applyClip(percent) {
  $('#cmpClip').style.clipPath = `inset(0 ${100 - percent}% 0 0)`;
  $('#cmpHandle').style.left = `${percent}%`;
}

/* ---------------- 视图切换 ---------------- */

function setView(view) {
  if (!state.result && view !== 'detect') view = 'detect';
  state.view = view;
  document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.dataset.view === view));
  $('#wrap').hidden = view !== 'detect';
  $('#afterView').hidden = view !== 'after';
  $('#compare').hidden = view !== 'compare';
}

/* ---------------- 上传 ---------------- */

async function uploadFile(file) {
  if (!file) return;
  if (file.size > 40 * 1024 * 1024) { toast('图片超过 40 MB', true); return; }
  busy(true, '正在读取图片…');
  try {
    const res = await fetch('/api/upload', {
      method: 'POST',
      headers: { 'X-Filename': encodeURIComponent(file.name) },
      body: file,
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || '上传失败');
    busy(false);
    await choose(data.source_id, file.name);
  } catch (err) {
    busy(false);
    toast(err.message, true);
  }
}

/* ---------------- 初始化 ---------------- */

function init() {
  loadSamples()
    .then(() => {
      // 深链：?s=demo-03 直接打开某个案例。**必须在图库加载完之后**，
      // 否则 state.samples 还是空的，找谁都找不到（第一版就踩在这）。
      const params = new URLSearchParams(location.search);
      state.autoRun = params.get('run') === '1';
      const auto = params.get('s');
      if (!auto) return;
      const hit = state.samples.find((x) => x.id === auto);
      if (hit) choose(`sample:${hit.id}`, hit.title);
    })
    .catch((e) => toast(e.message, true));
  refreshStatus();

  const fileInput = $('#file');
  fileInput.addEventListener('change', (e) => uploadFile(e.target.files[0]));

  const drop = $('#drop');
  ['dragenter', 'dragover'].forEach((ev) => drop.addEventListener(ev, (e) => {
    e.preventDefault(); drop.classList.add('over');
  }));
  ['dragleave', 'drop'].forEach((ev) => drop.addEventListener(ev, (e) => {
    e.preventDefault(); drop.classList.remove('over');
  }));
  drop.addEventListener('drop', (e) => {
    const f = e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) uploadFile(f);
  });

  $('#method').addEventListener('change', (e) => {
    const notes = {
      solid: '纯色块：整块覆盖，复检必过。演示首选。',
      mosaic: '马赛克：用于文字区域；人脸区域会自动改用实心遮盖。',
      blur: '模糊：用于文字区域；人脸区域会自动改用实心遮盖。',
    };
    state.methodNote = notes[e.target.value] || '';
    bumpRevision('遮盖方式已变化，旧复检报告已失效');
    updateHint();
  });
  $('#btnProcess').addEventListener('click', process);
  $('#cmpRange').addEventListener('input', (e) => applyClip(Number(e.target.value)));

  document.querySelectorAll('.tab').forEach((t) => {
    t.addEventListener('click', () => setView(t.dataset.view));
  });

  const btn = $('#btnManual');
  btn.addEventListener('click', () => {
    if (!state.sourceId) { toast('先选一张图'); return; }
    state.manualMode = !state.manualMode;
    btn.classList.toggle('active', state.manualMode);
    $('#wrap').classList.toggle('adding', state.manualMode);
    setView('detect');
    toast(state.manualMode ? '已开启：在图上拖出要遮盖的区域' : '已关闭手动补框');
  });

  setupManual();
  updateHint();

}

document.addEventListener('DOMContentLoaded', init);
