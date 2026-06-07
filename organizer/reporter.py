"""분석 결과 → 자체 완결형 HTML 리포트 생성.

서버 없이 브라우저에서 바로 열린다. 사용자가 체크박스로 정리할 파일을
고른 뒤 "결정 내보내기" 버튼으로 decisions.json 을 내려받는다.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import config

_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>파일 정리 리포트</title>
<style>
  body { font-family: "Malgun Gothic","맑은 고딕",system-ui,sans-serif; margin:0; background:#f4f5f7; color:#222; }
  header { background:#1f2937; color:#fff; padding:16px 24px; position:sticky; top:0; z-index:10; }
  header h1 { margin:0; font-size:20px; }
  .meta { font-size:13px; color:#cbd5e1; margin-top:4px; }
  .wrap { max-width:1100px; margin:0 auto; padding:24px; }
  .cards { display:flex; gap:12px; flex-wrap:wrap; margin-bottom:20px; }
  .card { background:#fff; border-radius:10px; padding:14px 18px; box-shadow:0 1px 3px rgba(0,0,0,.08); flex:1; min-width:150px; }
  .card .n { font-size:24px; font-weight:700; }
  .card .l { font-size:13px; color:#6b7280; }
  section { background:#fff; border-radius:10px; margin-bottom:20px; box-shadow:0 1px 3px rgba(0,0,0,.08); }
  section > h2 { margin:0; padding:14px 18px; border-bottom:1px solid #eee; font-size:16px; cursor:pointer; }
  section > h2 .badge { background:#e5e7eb; border-radius:10px; padding:2px 8px; font-size:12px; margin-left:8px; color:#374151;}
  .group { padding:12px 18px; border-bottom:1px solid #f0f0f0; }
  .group:last-child { border-bottom:none; }
  .grp-title { font-size:13px; color:#6b7280; margin-bottom:6px; }
  .file { display:flex; align-items:center; gap:10px; padding:5px 0; font-size:13px; }
  .file input { width:16px; height:16px; }
  .file .path { font-family:Consolas,monospace; word-break:break-all; flex:1; }
  .file .size, .file .date { color:#6b7280; white-space:nowrap; font-size:12px; }
  .keep { color:#059669; font-weight:600; }
  .tag { background:#fef3c7; color:#92400e; border-radius:6px; padding:1px 6px; font-size:11px; margin-left:6px; }
  .tag.late { background:#dbeafe; color:#1e40af; }
  .hint { font-size:12px; color:#6b7280; padding:0 18px 12px; }
  .bar { position:sticky; bottom:0; background:#fff; border-top:1px solid #ddd; padding:14px 24px; display:flex; gap:14px; align-items:center; box-shadow:0 -1px 4px rgba(0,0,0,.06);}
  button { background:#2563eb; color:#fff; border:none; border-radius:8px; padding:10px 18px; font-size:14px; cursor:pointer; }
  button:hover { background:#1d4ed8; }
  .count { font-size:14px; color:#374151; }
  .copybtn { background:#6b7280; padding:2px 8px; font-size:11px; border-radius:6px; }
  details summary { cursor:pointer; }
  .empty { padding:14px 18px; color:#9ca3af; font-size:13px; }
</style>
</head>
<body>
<header>
  <h1>📁 파일 정리 리포트</h1>
  <div class="meta">생성: __GENERATED__ · 모든 분석은 로컬에서 수행됨 (외부 전송 없음)</div>
</header>
<div class="wrap">
  <div class="cards" id="cards"></div>
  <div id="sections"></div>
</div>
<div class="bar">
  <button onclick="exportDecisions()">✅ 선택 항목 결정 내보내기 (decisions.json)</button>
  <span class="count" id="selcount">선택됨: 0개</span>
  <span class="count" style="color:#6b7280">체크한 파일만 격리폴더로 이동됩니다. 다운로드한 decisions.json 을 data 폴더에 두고 적용하세요.</span>
</div>
<script>
const DATA = __DATA__;

function fmtSize(b){ if(b==null)return''; const u=['B','KB','MB','GB','TB']; let i=0,n=b; while(n>=1024&&i<u.length-1){n/=1024;i++;} return n.toFixed(i?1:0)+' '+u[i]; }
function fmtDate(t){ if(!t)return''; const d=new Date(t*1000); const p=x=>String(x).padStart(2,'0'); return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+' '+p(d.getHours())+':'+p(d.getMinutes()); }
function esc(s){ return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

function fileRow(f, opts={}){
  const d=document.createElement('div'); d.className='file';
  if(opts.keepLabel){
    d.innerHTML = `<span style="width:16px"></span><span class="path keep">${esc(f.path)} <span class="keep">✔ 보관</span></span>`+
      `<span class="size">${fmtSize(f.size)}</span><span class="date">${fmtDate(f.mtime)}</span>`;
  } else {
    const checked = f.recommend==='move' ? 'checked' : '';
    let tags='';
    (f.tags||[]).forEach(t=>{ tags += t==='실제_최근수정' ? ` <span class="tag late">실제 최근수정</span>` : ` <span class="tag">${esc(t)}</span>`; });
    d.innerHTML = `<input type="checkbox" class="sel" data-path="${esc(f.path)}" ${checked}>`+
      `<span class="path">${esc(f.path)}${tags}</span>`+
      `<span class="size">${fmtSize(f.size)}</span><span class="date">${fmtDate(f.mtime)}</span>`+
      `<button class="copybtn" onclick="navigator.clipboard.writeText('${esc(f.path).replace(/\\\\/g,'\\\\\\\\')}')">경로복사</button>`;
  }
  return d;
}

function section(title, count, builder){
  const s=document.createElement('section');
  const h=document.createElement('h2');
  h.innerHTML = `${title} <span class="badge">${count}건</span>`;
  const body=document.createElement('div');
  h.onclick=()=>{ body.style.display = body.style.display==='none'?'':'none'; };
  s.appendChild(h); s.appendChild(body);
  builder(body);
  return s;
}

function render(){
  // 카드
  const st=DATA.stats;
  document.getElementById('cards').innerHTML = `
    <div class="card"><div class="n">${st.total_files.toLocaleString()}</div><div class="l">스캔된 파일</div></div>
    <div class="card"><div class="n">${st.exact_dup_groups}</div><div class="l">완전중복 그룹</div></div>
    <div class="card"><div class="n">${st.name_conflict_groups}</div><div class="l">이름충돌 그룹</div></div>
    <div class="card"><div class="n">${st.version_anomaly_groups}</div><div class="l">버전이상 그룹</div></div>
    <div class="card"><div class="n">${fmtSize(st.reclaimable_bytes)}</div><div class="l">회수 가능 용량</div></div>`;

  const root=document.getElementById('sections');

  // (A) 완전중복
  root.appendChild(section('🟥 완전 중복 (내용 동일) — 1개만 보관, 나머지 이동 추천', DATA.exact_duplicates.length, body=>{
    body.insertAdjacentHTML('beforeend', `<div class="hint">내용이 완전히 같은 파일들입니다. 보관 1개를 제외한 사본이 자동 체크되어 있습니다.</div>`);
    if(!DATA.exact_duplicates.length){ body.insertAdjacentHTML('beforeend','<div class="empty">없음</div>'); return; }
    DATA.exact_duplicates.forEach(g=>{
      const gd=document.createElement('div'); gd.className='group';
      gd.insertAdjacentHTML('beforeend', `<div class="grp-title">${g.count}개 · 각 ${fmtSize(g.size)}</div>`);
      g.files.forEach(f=> gd.appendChild(fileRow(f, {keepLabel: f.recommend==='keep'})));
      body.appendChild(gd);
    });
  }));

  // (B) 이름충돌
  root.appendChild(section('🟨 이름 같음 · 내용 다름 — 직접 확인 필요', DATA.name_conflicts.length, body=>{
    body.insertAdjacentHTML('beforeend', `<div class="hint">같은 이름인데 내용이 다릅니다. 크기·수정시간을 보고 최신만 남기세요. 기본 미체크.</div>`);
    if(!DATA.name_conflicts.length){ body.insertAdjacentHTML('beforeend','<div class="empty">없음</div>'); return; }
    DATA.name_conflicts.forEach(g=>{
      const gd=document.createElement('div'); gd.className='group';
      gd.insertAdjacentHTML('beforeend', `<div class="grp-title">${esc(g.name)} · ${g.count}개 (서로 다른 내용 ${g.distinct}종)</div>`);
      g.files.forEach(f=> gd.appendChild(fileRow(f)));
      body.appendChild(gd);
    });
  }));

  // (C) 버전이상
  root.appendChild(section('🟦 버전 이상 — 이름은 최신인데 수정일이 더 옛날', DATA.version_anomalies.length, body=>{
    body.insertAdjacentHTML('beforeend', `<div class="hint">파일명은 "최종/v2" 등 최신을 주장하지만 실제 수정일이 더 오래된 경우입니다. 잘못 저장된 옛 버전일 수 있습니다. 기본 미체크.</div>`);
    if(!DATA.version_anomalies.length){ body.insertAdjacentHTML('beforeend','<div class="empty">없음</div>'); return; }
    DATA.version_anomalies.forEach(g=>{
      const gd=document.createElement('div'); gd.className='group';
      gd.insertAdjacentHTML('beforeend', `<div class="grp-title">가족: ${esc(g.family)}${esc(g.ext)} · ${g.count}개</div>`);
      g.files.forEach(f=> gd.appendChild(fileRow(f)));
      body.appendChild(gd);
    });
  }));

  // (D~) 키퍼형 추가 카테고리 일반 렌더
  const keeperCats = [
    ['image_dups','🟪 비슷한 이미지 — 리사이즈·재압축·메타만 다른 사진'],
    ['video_dups','🎬 비슷한 영상 — 재인코딩·해상도만 다른 영상'],
    ['audio_dups','🎵 비슷한 오디오 (실험적)'],
    ['doc_dups','🟩 같은 내용 문서'],
    ['doc_near','🟫 비슷한 문서 — 약간 수정된 버전'],
    ['zip_dups','📦 같은 내용 압축'],
  ];
  keeperCats.forEach(([key,title])=>{
    const groups = DATA[key]||[];
    root.appendChild(section(title, groups.length, body=>{
      if(!groups.length){ body.insertAdjacentHTML('beforeend','<div class="empty">없음</div>'); return; }
      groups.forEach(g=>{
        const gd=document.createElement('div'); gd.className='group';
        gd.insertAdjacentHTML('beforeend', `<div class="grp-title">${g.count}개</div>`);
        g.files.forEach(f=> gd.appendChild(fileRow(f, {keepLabel: f.recommend==='keep'})));
        body.appendChild(gd);
      });
    }));
  });
  // 평면 목록 카테고리(파일/폴더 단위)
  const flatCats = [
    ['junk_files','🧹 시스템 찌꺼기 — 삭제 안전'],
    ['archive_loose','🗂 풀린 압축 — 내용이 이미 디스크에 있음'],
    ['empty_dirs','📂 빈 폴더'],
  ];
  flatCats.forEach(([key,title])=>{
    const items = DATA[key]||[];
    root.appendChild(section(title, items.length, body=>{
      if(!items.length){ body.insertAdjacentHTML('beforeend','<div class="empty">없음</div>'); return; }
      const gd=document.createElement('div'); gd.className='group';
      items.forEach(f=> gd.appendChild(fileRow(f)));
      body.appendChild(gd);
    }));
  });

  // 오류
  if(DATA.errors && DATA.errors.length){
    root.appendChild(section('⚠️ 스캔 중 건너뛴 항목', DATA.errors.length, body=>{
      DATA.errors.forEach(e=>{
        body.insertAdjacentHTML('beforeend', `<div class="group" style="font-size:12px;color:#6b7280">${esc(e.path)} — ${esc(e.error)}</div>`);
      });
    }));
  }

  document.body.addEventListener('change', e=>{ if(e.target.classList.contains('sel')) updateCount(); });
  updateCount();
}

function updateCount(){
  const n=document.querySelectorAll('.sel:checked').length;
  document.getElementById('selcount').textContent = '선택됨: '+n+'개';
}

function exportDecisions(){
  const move=[...document.querySelectorAll('.sel:checked')].map(c=>c.getAttribute('data-path'));
  if(!move.length){ alert('선택된 파일이 없습니다.'); return; }
  const payload={ version:1, generated_at:new Date().toISOString(), move };
  const blob=new Blob([JSON.stringify(payload,null,2)],{type:'application/json'});
  const a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download='decisions.json'; a.click();
}

render();
</script>
</body>
</html>
"""


def build_report(analysis: dict, out_path: Path = config.REPORT_HTML) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    html = _TEMPLATE.replace("__DATA__", json.dumps(analysis, ensure_ascii=False))
    html = html.replace("__GENERATED__", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path
