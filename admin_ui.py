# -*- coding: utf-8 -*-
"""cline-router 图形化配置面板（本地网页 UI，由 router.py 挂载在 /ui）。

纯静态字符串，无模板引擎依赖；数据通过 /api/config 与 /api/test 读写。
"""

HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cline 路由 · 配置</title>
<style>
:root{--bg:#0b0e11;--panel:#131820;--panel2:#0f141b;--line:#242c37;--text:#e7ecf3;--sub:#8b98a9;--accent:#f0b90b;--ok:#0ecb81;--bad:#f6465d}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:13px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",sans-serif}
.wrap{max-width:1020px;margin:0 auto;padding:20px}
header{display:flex;align-items:center;gap:14px;padding:16px;background:var(--panel);border:1px solid var(--line);border-radius:14px;margin-bottom:16px}
h1{font-size:15px;margin:0;font-weight:700;letter-spacing:.3px}
.dot{width:9px;height:9px;border-radius:50%;background:var(--ok);box-shadow:0 0 8px var(--ok)}
.meta{color:var(--sub);font-size:12px}
.spacer{flex:1}
button{font:inherit;padding:7px 14px;border-radius:10px;border:1px solid var(--line);background:var(--panel2);color:var(--text);cursor:pointer;transition:.15s}
button:hover{border-color:var(--accent);color:var(--accent)}
button.primary{background:var(--accent);border-color:var(--accent);color:#0b0e11;font-weight:700}
button.primary:hover{filter:brightness(1.08)}
button.danger:hover{border-color:var(--bad);color:var(--bad)}
button.sm{padding:4px 10px;font-size:12px}
button[disabled]{opacity:.5;cursor:default}
section{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px;margin-bottom:16px}
h2{font-size:14px;margin:0 0 12px;display:flex;align-items:center;gap:8px}
h2 .tag{color:var(--sub);font-weight:400;font-size:12px}
h2 .spacer{flex:1}
.card{background:var(--panel2);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin-bottom:10px}
.row{display:grid;grid-template-columns:110px 1fr;gap:8px 12px;align-items:center}
.row+.row{margin-top:8px}
label{color:var(--sub);font-size:12px}
input,select,textarea{width:100%;font:inherit;padding:7px 10px;border-radius:9px;border:1px solid var(--line);background:#0b0f14;color:var(--text)}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--accent)}
textarea{resize:vertical;min-height:66px;line-height:1.5}
.preview{margin-top:12px}
.preview img{max-width:100%;border-radius:12px;border:1px solid var(--line);display:block}
.preview a{color:var(--accent)}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);font-size:13px;vertical-align:top}
th{color:var(--sub);font-weight:500;font-size:12px}
tbody tr:hover{background:#0e131a}
.hint{color:var(--sub);font-size:12px;margin-top:8px}
.result{font-size:12px;margin-top:6px;color:var(--sub);word-break:break-all;max-width:340px}
.result.ok{color:var(--ok)}
.result.bad{color:var(--bad)}
#toast{position:fixed;left:50%;bottom:28px;transform:translateX(-50%);background:var(--panel);border:1px solid var(--accent);padding:10px 16px;border-radius:12px;opacity:0;transition:.25s;pointer-events:none;max-width:80vw}
#toast.show{opacity:1}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <span class="dot"></span>
    <h1>Cline 路由</h1>
    <span class="meta" id="status">加载中…</span>
    <span class="spacer"></span>
    <button class="sm" onclick="loadConfig()">刷新</button>
    <button class="primary" onclick="saveConfig()">保存并生效</button>
  </header>

  <section>
    <h2>服务设置 <span class="tag">改端口/口令后需重启服务</span></h2>
    <div class="row"><label>监听地址</label><input id="host"></div>
    <div class="row"><label>端口</label><input id="port"></div>
    <div class="row"><label>本机口令</label><input id="auth_key" placeholder="留空 = 不校验，任何 Key 都通过（自用推荐）"></div>
    <div class="row"><label>默认模型</label><select id="default_model"></select></div>
    <div class="row"><label>Cline 下拉</label><label style="display:flex;align-items:center;gap:8px;color:var(--text)"><input type="checkbox" id="only_auto" style="width:auto"> 只显示 auto —— Cline 里固定选 auto，切模型都在这里/菜单栏进行</label></div>
    <div class="hint" id="authHint"></div>
  </section>

  <section>
    <h2>上游 API <span class="tag">一个上游 = 一个 Base URL + 一把 Key</span><span class="spacer"></span><button class="sm" onclick="addUpstream()">+ 新增上游</button></h2>
    <div id="upstreams"></div>
    <div class="hint">名称用英文小写（如 volc / cline / myapi）；若某个上游需要特殊请求头（部分订阅制接口校验 UA），填在下面进阶项里。<br>
    <strong>接口类型</strong>：<code>openai</code>（默认，标准 OpenAI 兼容网关）· <code>codebuddy</code>（CodeBuddy 官方协议，自动加专用请求头、强制流式并在本地聚合成非流式；支持多 Key 轮换分摊限流）。</div>
  </section>

  <section>
    <h2>模型清单 <span class="tag">Cline 下拉里看到的就是这里的显示 ID</span><span class="spacer"></span><button class="sm" onclick="addModel()">+ 新增模型</button></h2>
    <table>
      <thead><tr><th style="width:26%">显示 ID</th><th style="width:20%">上游</th><th style="width:30%">上游真实模型名</th><th>操作</th></tr></thead>
      <tbody id="models"></tbody>
    </table>
    <div class="hint">「显示 ID」随你起名；「上游真实模型名」必须与上游文档/控制台一致。测试按钮使用的是<strong>已保存</strong>的配置，改完先保存再测。<strong>auto 是保留名</strong>：Cline 里填 auto 时自动走上面选的「默认模型」，勾选「只显示 auto」后下拉里就只有 auto 一个。</div>
  </section>

  <section>
    <h2>图片模型 <span class="tag">Seedream 等，走 /v1/images/generations</span><span class="spacer"></span><button class="sm" onclick="addImage()">+ 新增图片模型</button></h2>
    <table>
      <thead><tr><th style="width:22%">显示 ID</th><th style="width:16%">上游</th><th style="width:26%">上游真实模型名</th><th style="width:18%">默认尺寸</th><th>操作</th></tr></thead>
      <tbody id="images"></tbody>
    </table>
    <div class="hint">图片模型<strong>不会</strong>出现在 Cline 的对话模型下拉里（避免误选报错），只用于下面的绘图区和 /v1/images/generations 接口。</div>
  </section>

  <section>
    <h2>🎨 绘图 <span class="tag">Seedream · 生成的图会存到本程序目录的 images/ 下</span></h2>
    <div class="row"><label>提示词</label><textarea id="imgPrompt" placeholder="例如：一只赛博朋克风格的机械猫，霓虹灯背景，电影级光影"></textarea></div>
    <div class="row"><label>图片模型</label><select id="imgModel" onchange="syncImageSize()"></select></div>
    <div class="row"><label>尺寸</label><input id="imgSize" value="1024x1024" placeholder="如 1024x1024（lite 需 ≥1920x1920）"></div>
    <div style="margin-top:12px"><button class="primary" id="imgBtn" onclick="genImage()">生成图片</button></div>
    <div class="result" id="imgResult"></div>
  </section>
</div>
<div id="toast"></div>

<script>
var cfg = null;
function $(id){ return document.getElementById(id); }
function esc(s){ return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function toast(msg, bad){
  var t = $('toast'); t.textContent = msg;
  t.style.borderColor = bad ? 'var(--bad)' : 'var(--accent)';
  t.classList.add('show'); setTimeout(function(){ t.classList.remove('show'); }, 2800);
}
function api(path, opt){
  opt = opt || {};
  var headers = { 'X-Router-UI': '1', 'Content-Type': 'application/json' };
  opt.headers = Object.assign(headers, opt.headers || {});
  if (opt.body) opt.body = JSON.stringify(opt.body);
  return fetch(path, opt).then(function(r){ return r.json(); });
}
function loadConfig(){
  api('/api/config').then(function(d){
    cfg = d;
    $('host').value = d.host; $('port').value = d.port; $('auth_key').value = d.auth_key || '';
    $('status').textContent = '运行中 · 端口 ' + d.port + ' · 默认 ' + (d.default_model || '未设置')
      + ' · 对话模型 ' + (d.active_models||[]).length + ' 个 · 图片模型 ' + (d.active_images||[]).length + ' 个';
    $('authHint').textContent = d.auth_key
      ? '当前：开启校验 —— Cline 的 API Key 必须填「' + d.auth_key + '」'
      : '当前：不校验 —— Cline 的 API Key 填任何值都通过（自用够用；服务只监听本机，外网访问不到）';
    render();
  });
}
function render(){ renderDefaultModel(); renderUpstreams(); renderModels(); renderImages(); }
function renderDefaultModel(){
  var sel = $('default_model');
  var ids = (cfg.models || []).map(function(m){ return (m.id || '').trim(); }).filter(Boolean);
  sel.innerHTML = ids.map(function(id){
    return '<option value="' + esc(id) + '">' + esc(id) + (id === 'auto' ? '（与保留名冲突，勿用）' : '') + '</option>';
  }).join('');
  var cur = cfg.default_model && ids.indexOf(cfg.default_model) >= 0 ? cfg.default_model : (ids[0] || '');
  if (cur) sel.value = cur;
  else sel.innerHTML = '<option value="">（还没有对话模型）</option>';
  $('only_auto').checked = !!cfg.only_auto;
}
function renderImages(){
  var tb = $('images');
  var ups = Object.keys(cfg.upstreams || {});
  var list = cfg.images || [];
  if (!list.length){
    tb.innerHTML = '<tr><td colspan="5" class="hint">还没有图片模型</td></tr>';
  } else {
    tb.innerHTML = list.map(function(im, i){
      var opts = ups.map(function(n){
        return '<option value="' + esc(n) + '"' + (im.upstream === n ? ' selected' : '') + '>' + esc(n) + '</option>';
      }).join('');
      return '<tr>'
        + '<td><input data-kind="image" data-idx="' + i + '" data-field="id" value="' + esc(im.id||'') + '"></td>'
        + '<td><select data-kind="image" data-idx="' + i + '" data-field="upstream">' + opts + '</select></td>'
        + '<td><input data-kind="image" data-idx="' + i + '" data-field="model" value="' + esc(im.model||'') + '"></td>'
        + '<td><input data-kind="image" data-idx="' + i + '" data-field="size" value="' + esc(im.size||'1024x1024') + '"></td>'
        + '<td><button class="sm danger" onclick="delImage(' + i + ')">删除</button></td>'
        + '</tr>';
    }).join('');
  }
  var sel = $('imgModel');
  var ids = list.map(function(im){ return (im.id || '').trim(); }).filter(Boolean);
  var keep = sel.value;
  sel.innerHTML = ids.map(function(id){ return '<option value="' + esc(id) + '">' + esc(id) + '</option>'; }).join('');
  if (keep && ids.indexOf(keep) >= 0) sel.value = keep;
  syncImageSize();
}
function syncImageSize(){
  var id = $('imgModel').value;
  var hit = (cfg.images || []).filter(function(im){ return im.id === id; })[0];
  if (hit && hit.size) $('imgSize').value = hit.size;
}
function addImage(){
  var ups = Object.keys(cfg.upstreams || {});
  if (!ups.length){ toast('请先新增一个上游', true); return; }
  cfg.images = cfg.images || [];
  cfg.images.push({ id: 'seedream-' + (cfg.images.length + 1), upstream: ups[0],
                    model: 'doubao-seedream-5.0-pro', size: '1024x1024' });
  render();
}
function delImage(i){
  cfg.images.splice(i, 1);
  render();
}
function genImage(){
  var btn = $('imgBtn');
  var out = $('imgResult');
  var prompt = $('imgPrompt').value.trim();
  var model = $('imgModel').value;
  var size = $('imgSize').value.trim() || '1024x1024';
  if (!prompt){ toast('请先填提示词', true); return; }
  if (!model){ toast('请先配置并保存一个图片模型', true); return; }
  btn.disabled = true;
  out.className = 'result';
  out.textContent = '生成中（Seedream 一般 5～20 秒）…';
  api('/api/image', { method: 'POST', body: { model: model, prompt: prompt, size: size } }).then(function(r){
    btn.disabled = false;
    if (r.ok){
      out.className = 'result ok';
      out.innerHTML = '✅ 生成成功' + (r.saved ? ' · 已存到 ' + esc(r.saved) : '');
      if (r.saved_url) out.innerHTML += '<div class="preview"><img src="' + esc(r.saved_url) + '?t=' + Date.now() + '"></div>';
      else if (r.upstream_url) out.innerHTML += '<div class="preview"><a href="' + esc(r.upstream_url) + '" target="_blank">打开上游图片链接</a></div>';
    } else {
      out.className = 'result bad';
      out.textContent = '生成失败 · HTTP ' + r.status + ' · ' + ((r.error && r.error.message) || r.detail || '');
    }
  }).catch(function(e){
    btn.disabled = false;
    out.className = 'result bad';
    out.textContent = '请求出错：' + e;
  });
}
function renderUpstreams(){
  var box = $('upstreams');
  var names = Object.keys(cfg.upstreams || {});
  if (!names.length){ box.innerHTML = '<div class="hint">还没有上游，点右上角「+ 新增上游」</div>'; return; }
  box.innerHTML = names.map(function(n){
    var u = cfg.upstreams[n] || {};
    var hd = u.headers && Object.keys(u.headers).length ? JSON.stringify(u.headers) : '';
    var cb = (u.mode || 'openai') === 'codebuddy';
    return '<div class="card">'
      + '<div class="row"><label>名称</label><input data-kind="upname" data-key="' + esc(n) + '" value="' + esc(n) + '"></div>'
      + '<div class="row"><label>接口类型</label><select data-kind="upmode" data-key="' + esc(n) + '" onchange="setUpMode(this)">'
      +   '<option value="openai"' + (cb ? '' : ' selected') + '>openai —— 标准 OpenAI 兼容网关（默认）</option>'
      +   '<option value="codebuddy"' + (cb ? ' selected' : '') + '>codebuddy —— CodeBuddy 官方协议</option>'
      + '</select></div>'
      + '<div class="row"><label>Base URL</label><input data-kind="up" data-key="' + esc(n) + '" data-field="base_url" value="' + esc(u.base_url||'') + '" placeholder="' + (cb ? 'https://copilot.tencent.com' : 'https://example.com/v1') + '"></div>'
      + '<div class="row"><label>API Key</label><input data-kind="up" data-key="' + esc(n) + '" data-field="api_key" type="password" value="' + esc(u.api_key||'') + '" placeholder="可写 ${ENV_VAR} 从环境变量取"></div>'
      + '<div class="row"><label>对话路径</label><input data-kind="up" data-key="' + esc(n) + '" data-field="path" value="' + esc(u.path||'') + '" placeholder="' + (cb ? '/v2/chat/completions（留空即用此默认值）' : '/chat/completions（留空即用此默认值）') + '"></div>'
      + '<div class="row"><label>密钥池(轮换)</label><textarea data-kind="up" data-key="' + esc(n) + '" data-field="api_keys" placeholder="一行一把 Key；留空则只用上面那把单 Key。多把时会轮流用，分摊限流">' + esc((u.api_keys||[]).join('\n')) + '</textarea></div>'
      + '<div class="row"><label>轮换周期</label><input data-kind="up" data-key="' + esc(n) + '" data-field="rotation_count" value="' + esc(u.rotation_count||1) + '" placeholder="每 N 次请求换下一把 Key，默认 1"></div>'
      + '<div class="row"><label>超时(秒)</label><input data-kind="up" data-key="' + esc(n) + '" data-field="timeout" value="' + esc(u.timeout||900) + '"></div>'
      + '<div class="row"><label>代理(可选)</label><input data-kind="up" data-key="' + esc(n) + '" data-field="proxy" value="' + esc(u.proxy||'') + '" placeholder="如 http://127.0.0.1:7890"></div>'
      + '<div class="row"><label>额外请求头</label><input data-kind="up" data-key="' + esc(n) + '" data-field="headers" value="' + esc(hd) + '" placeholder=\\'{"User-Agent":"xxx"}\\'></div>'
      + '<div style="margin-top:10px"><button class="sm danger" onclick="delUpstream(\\'' + esc(n) + '\\')">删除该上游</button></div>'
      + '</div>';
  }).join('');
}
function renderModels(){
  var tb = $('models');
  var ups = Object.keys(cfg.upstreams || {});
  if (!(cfg.models||[]).length){ tb.innerHTML = '<tr><td colspan="4" class="hint">还没有模型，点右上角「+ 新增模型」</td></tr>'; return; }
  tb.innerHTML = cfg.models.map(function(m, i){
    var opts = ups.map(function(n){
      return '<option value="' + esc(n) + '"' + (m.upstream === n ? ' selected' : '') + '>' + esc(n) + '</option>';
    }).join('');
    return '<tr>'
      + '<td><input data-kind="model" data-idx="' + i + '" data-field="id" value="' + esc(m.id||'') + '"></td>'
      + '<td><select data-kind="model" data-idx="' + i + '" data-field="upstream">' + opts + '</select></td>'
      + '<td><input data-kind="model" data-idx="' + i + '" data-field="model" value="' + esc(m.model||'') + '"></td>'
      + '<td style="white-space:nowrap"><button class="sm" onclick="testModel(' + i + ',this)">测试</button> <button class="sm danger" onclick="delModel(' + i + ')">删除</button><div class="result" id="r' + i + '"></div></td>'
      + '</tr>';
  }).join('');
}
document.addEventListener('input', function(e){
  var el = e.target;
  if (!el.dataset || !el.dataset.kind) return;
  if (el.dataset.kind === 'up'){
    var up = cfg.upstreams[el.dataset.key];
    if (!up) return;
    var f = el.dataset.field;
    if (f === 'headers'){
      var txt = el.value.trim();
      if (!txt){ delete up.headers; return; }
      try { up.headers = JSON.parse(txt); el.style.borderColor = ''; }
      catch (err) { el.style.borderColor = 'var(--bad)'; }
    } else if (f === 'timeout'){ up.timeout = parseInt(el.value, 10) || 900; }
    else if (f === 'rotation_count'){ up.rotation_count = parseInt(el.value, 10) || 1; }
    else if (f === 'api_keys'){
      var keys = String(el.value).split(/[\n,;]+/).map(function(s){ return s.trim(); }).filter(Boolean);
      if (keys.length) up.api_keys = keys; else delete up.api_keys;
    }
    else { up[f] = el.value; }
  } else if (el.dataset.kind === 'model'){
    cfg.models[parseInt(el.dataset.idx,10)][el.dataset.field] = el.value;
  } else if (el.dataset.kind === 'image'){
    cfg.images[parseInt(el.dataset.idx,10)][el.dataset.field] = el.value;
  }
});
document.addEventListener('change', function(e){
  var el = e.target;
  if (!el.dataset || el.dataset.kind !== 'upname') return;
  var oldName = el.dataset.key, newName = (el.value || '').trim();
  if (!newName || newName === oldName) return;
  if (cfg.upstreams[newName]) { toast('上游名称已存在', true); return; }
  var rebuilt = {};
  Object.keys(cfg.upstreams).forEach(function(k){ rebuilt[k === oldName ? newName : k] = cfg.upstreams[k]; });
  cfg.upstreams = rebuilt;
  (cfg.models||[]).forEach(function(m){ if (m.upstream === oldName) m.upstream = newName; });
  (cfg.images||[]).forEach(function(m){ if (m.upstream === oldName) m.upstream = newName; });
  render();
});
function setUpMode(sel){
  var up = cfg.upstreams[sel.dataset.key];
  if (!up) return;
  up.mode = sel.value;
  // 切到 codebuddy 时把典型的官方端点/路径预填好，省得手抄
  if (up.mode === 'codebuddy'){
    if (!up.base_url) up.base_url = 'https://copilot.tencent.com';
    if (!up.path) up.path = '/v2/chat/completions';
  }
  render();
}
function addUpstream(){
  var n = 'api' + (Object.keys(cfg.upstreams||{}).length + 1);
  while (cfg.upstreams[n]) n = n + 'x';
  cfg.upstreams[n] = { base_url: '', api_key: '', timeout: 900 };
  render();
}
function delUpstream(name){
  var usedM = (cfg.models||[]).filter(function(m){ return m.upstream === name; }).length;
  var usedI = (cfg.images||[]).filter(function(m){ return m.upstream === name; }).length;
  if (usedM || usedI){
    toast('该上游下还有 ' + usedM + ' 个对话模型 / ' + usedI + ' 个图片模型，请先删除或改挂别的上游', true);
    return;
  }
  if (!confirm('删除上游「' + name + '」？')) return;
  delete cfg.upstreams[name];
  render();
}
function addModel(){
  var ups = Object.keys(cfg.upstreams || {});
  if (!ups.length){ toast('请先新增一个上游', true); return; }
  cfg.models = cfg.models || [];
  cfg.models.push({ id: 'model' + (cfg.models.length + 1), upstream: ups[0], model: '' });
  render();
}
function delModel(i){
  cfg.models.splice(i, 1);
  render();
}
function saveConfig(){
  cfg.host = $('host').value.trim() || '127.0.0.1';
  cfg.port = parseInt($('port').value, 10) || 4000;
  cfg.auth_key = $('auth_key').value;
  cfg.models = (cfg.models || []).filter(function(m){ return (m.id || '').trim(); });
  cfg.images = (cfg.images || []).filter(function(m){ return (m.id || '').trim(); });
  api('/api/config', { method: 'POST', body: {
    host: cfg.host, port: cfg.port, auth_key: cfg.auth_key,
    default_model: $('default_model').value,
    only_auto: $('only_auto').checked,
    upstreams: cfg.upstreams, models: cfg.models, images: cfg.images
  }}).then(function(r){
    if (r.ok){ toast('已保存并生效' + (r.warnings && r.warnings.length ? '（' + r.warnings.join('；') + '）' : '')); loadConfig(); }
    else { toast((r.error && r.error.message) || '保存失败', true); }
  }).catch(function(e){ toast('保存请求出错：' + e, true); });
}
function testModel(i, btn){
  var el = $('r' + i);
  var mid = cfg.models[i].id;
  if (!mid){ el.className = 'result bad'; el.textContent = '请先填显示 ID 并保存'; return; }
  el.className = 'result'; el.textContent = '测试中…'; btn.disabled = true;
  api('/api/test', { method: 'POST', body: { id: mid } }).then(function(r){
    btn.disabled = false;
    if (r.ok){ el.className = 'result ok'; el.textContent = '通过 · HTTP ' + r.status + ' · ' + r.ms + 'ms'; }
    else { el.className = 'result bad'; el.textContent = '失败 · HTTP ' + r.status + ' · ' + (r.detail || (r.error && r.error.message) || ''); }
  }).catch(function(e){ btn.disabled = false; el.className = 'result bad'; el.textContent = '请求出错：' + e; });
}
loadConfig();
</script>
</body>
</html>
"""
