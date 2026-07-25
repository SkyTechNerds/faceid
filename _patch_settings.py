#!/usr/bin/env python3
import re
p = "static/index.html"
s = open(p, encoding="utf-8").read()

# 1) Backup-Block aus Persons entfernen
block = '''+body+`
    <div class="row" style="margin-top:20px;border-top:1px solid var(--line);padding-top:14px">
      <span class="stat">BACKUP</span>
      <a class="act" href="api/backup" title="download all persons + ignore anchors as a .tar.gz" style="text-decoration:none">DOWNLOAD BACKUP</a>
      <label class="ghost" style="cursor:pointer" title="restore a backup — REPLACES all current persons + ignore anchors">Restore (replace)
        <input type="file" accept=".gz,.tar.gz,application/gzip" style="display:none" onchange="restore(this,false)">
      </label>
      <label class="ghost" style="cursor:pointer" title="restore a backup — only ADDS persons/anchors missing locally, keeps existing">Restore (merge)
        <input type="file" accept=".gz,.tar.gz,application/gzip" style="display:none" onchange="restore(this,true)">
      </label>
    </div>`;'''
assert block in s, "backup-block nicht gefunden"
s = s.replace(block, "+body;", 1)

# 2) SETTINGS-Nav-Button nach IGNORED
ign = '>IGNORED</button>'
assert ign in s
s = s.replace(ign, ign + '\n  <button data-t="settings" title="matching thresholds, backup and restore">SETTINGS</button>', 1)

# 3) Routing
s = s.replace(
    "  if(tab==='ignored')return renderIgnored();\n  return renderPersons();\n}",
    "  if(tab==='ignored')return renderIgnored();\n  if(tab==='settings')return renderSettings();\n  return renderPersons();\n}", 1)

# 4) renderSettings + restore + saveSetting Funktionen vor renderPersons einfuegen
anchor = "function personCard(slug,p){"
assert anchor in s
settings_js = r'''async function renderSettings(){
  if(tab!=='settings')return;
  const cfg=await j('api/settings');
  if(tab!=='settings')return;
  const labels={match_threshold:'Recognized at / above',unknown_threshold:'Definitely unknown below',
    suggest_threshold:'Group as "looks like" from',cluster_eps:'Cluster grouping distance',
    ignore_threshold:'Counts as ignored from'};
  const hints={match_threshold:'higher = fewer false matches, but more real people land in review',
    unknown_threshold:'below this a face is treated as a clear stranger',
    suggest_threshold:'keep below the recognition threshold so borderline faces get pre-sorted',
    cluster_eps:'higher merges more faces per cluster; lower splits them apart',
    ignore_threshold:'similarity to an ignore anchor needed to stay silent'};
  const rows=Object.entries(cfg.thresholds).map(([k,v])=>{
    const [lo,hi]=cfg.ranges[k];
    return `<div style="margin:10px 0">
      <label style="display:flex;gap:10px;align-items:center">
        <b style="min-width:230px;font:600 13px var(--sans)">${labels[k]||k}</b>
        <input type="range" id="set-${k}" min="${lo}" max="${hi}" step="0.01" value="${v}"
          oninput="document.getElementById('setv-${k}').textContent=(+this.value).toFixed(2)" style="flex:1">
        <span id="setv-${k}" style="font:13px var(--mono);color:var(--acc);min-width:38px">${v.toFixed(2)}</span>
      </label>
      <div class="stat" style="margin-left:240px">${hints[k]||''}</div>
    </div>`;}).join('');
  const b=cfg.backup;
  main.innerHTML=`
    <h2>Matching thresholds</h2>
    ${rows}
    <div class="row"><button class="act" onclick="saveThresholds()">SAVE THRESHOLDS</button>
      <span class="stat">applies live; changes here override the config / add-on options and persist</span></div>

    <h2>Automatic backup</h2>
    <div class="row">
      <label style="display:flex;gap:8px;align-items:center"><input type="checkbox" id="bkEnabled" ${b.enabled?'checked':''}> daily backup</label>
      <label style="display:flex;gap:6px;align-items:center">at hour <input id="bkHour" type="number" min="0" max="23" value="${b.hour}" size="2" style="width:52px"></label>
      <label style="display:flex;gap:6px;align-items:center">keep <input id="bkKeep" type="number" min="1" max="90" value="${b.keep}" size="2" style="width:60px"></label>
      <label style="display:flex;gap:6px;align-items:center">folder <input id="bkDir" value="${esc(b.dir)}" placeholder="(data/backups)" size="20"></label>
      <button class="act" onclick="saveBackupCfg()">SAVE</button>
    </div>
    <div class="stat" style="margin-bottom:16px">runs inside FaceID — no external cron needed. Empty folder = <code>data/backups</code> (survives add-on updates).</div>

    <h2>Manual backup &amp; restore</h2>
    <div class="row">
      <a class="act" href="api/backup" style="text-decoration:none" title="download persons + ignore anchors as .tar.gz">DOWNLOAD BACKUP</a>
      <button class="ghost" onclick="backupNow()" title="write a backup into the auto-backup folder now">save to folder now</button>
      <label class="ghost" style="cursor:pointer" title="REPLACES all current persons + ignore anchors">Restore (replace)
        <input type="file" accept=".gz,.tar.gz,application/gzip" style="display:none" onchange="restore(this,false)"></label>
      <label class="ghost" style="cursor:pointer" title="only ADDS what is missing locally, keeps existing">Restore (merge)
        <input type="file" accept=".gz,.tar.gz,application/gzip" style="display:none" onchange="restore(this,true)"></label>
    </div>
    <div class="stat">Backup contains only your gallery (persons + ignore anchors) — not the unknown queue.</div>`;
}
async function saveThresholds(){
  const body={};
  ['match_threshold','unknown_threshold','suggest_threshold','cluster_eps','ignore_threshold'].forEach(k=>{
    const el=document.getElementById('set-'+k); if(el)body[k]=+el.value;});
  await j('api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  toast('Thresholds saved');
}
async function saveBackupCfg(){
  const body={backup_enabled:document.getElementById('bkEnabled').checked,
    backup_hour:+document.getElementById('bkHour').value,
    backup_keep:+document.getElementById('bkKeep').value,
    backup_dir:document.getElementById('bkDir').value.trim()};
  await j('api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  toast('Backup settings saved');
}
async function backupNow(){
  try{const r=await j('api/backup/now',{method:'POST'}); toast('Saved: '+r.file.split('/').pop());}
  catch(e){toast('Backup failed');}
}
async function restore(inp,merge){
  const f=inp.files[0]; if(!f)return;
  if(!merge&&!confirm('Replace ALL current persons and ignore anchors with this backup? This cannot be undone.')){inp.value='';return;}
  const fd=new FormData(); fd.append('file',f);
  toast('Restoring...');
  try{
    const r=await j('api/restore?merge='+(merge?'true':'false'),{method:'POST',body:fd});
    toast('Restored '+r.restored_files+' file(s) ('+r.mode+') - '+r.persons+' persons');
  }catch(e){toast('Restore failed: '+String(e.message).slice(0,80));}
  inp.value=''; persons=await j('api/persons'); if(tab==='settings')renderSettings();
}
function personCard(slug,p){'''
s = s.replace(anchor, settings_js, 1)

# 5) alte restore()-Funktion (falls noch da) entfernen — die neue steht jetzt oben
old_restore = re.search(r"async function restore\(inp,merge\)\{.*?\n\}\n(?=async function toggleFav)", s, re.S)
# es gibt jetzt evtl. zwei; entferne die ZWEITE (die alte vor toggleFav)
occ = [m.start() for m in re.finditer(r"async function restore\(inp,merge\)\{", s)]
if len(occ) > 1:
    # entferne die letzte
    m = re.compile(r"async function restore\(inp,merge\)\{.*?\n\}\n", re.S)
    matches = list(m.finditer(s))
    last = matches[-1]
    s = s[:last.start()] + s[last.end():]

open(p, "w", encoding="utf-8").write(s)
print("settings-tab ok:", "renderSettings" in s, "| restore-count:", s.count("async function restore(inp,merge)"))
