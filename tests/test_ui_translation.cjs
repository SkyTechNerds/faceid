const fs=require('fs'),vm=require('vm'),cp=require('child_process'),assert=require('assert/strict');
const root=process.argv[2]||require('path').resolve(__dirname,'..');
const langCtx={localStorage:{getItem:()=> 'ru'},navigator:{language:'ru'},window:{alert:()=>{},confirm:()=>false,prompt:()=>null},document:{readyState:'loading',addEventListener:()=>{}}};
vm.createContext(langCtx);vm.runInContext(fs.readFileSync(root+'/static/assets/i18n-ru.js','utf8').replace(/\}\)\(\);\s*$/,'globalThis.L={translate,translateDialog};})();'),langCtx);
const tr=s=>langCtx.L.translate(s)??s;
const cfg={input_mode:'frigate',thresholds:{match_threshold:.5,unknown_threshold:.35,suggest_threshold:.4,cluster_eps:.55,ignore_threshold:.5,dedupe_threshold:.65},ranges:{match_threshold:[.2,.9],unknown_threshold:[.1,.8],suggest_threshold:[.1,.9],cluster_eps:[.3,.8],ignore_threshold:[.1,.9],dedupe_threshold:[.5,.95]},max_faces_per_person:40,match_top_k:3,min_face_px:48,max_attempts:6,max_ignore_anchors:0,trimmed_keep:10,hires_enroll:true,clip_fallback:true,clip_fallback_cameras:['DOOR','LOBBY','HALL'],live_hires_fallback:true,live_hires_fallback_cameras:['DOOR','LOBBY','HALL'],live_hires_mode:'always',known_cameras:['DOOR','LOBBY','HALL'],backup:{enabled:false,hour:3,keep:7,dir:''},folder:{path:'/media/faceid',camera:'front_door',processed:2,indexed:7,index_cap:5000,scanning:true}};
const person={name:'Мария',count:2,files:['a.jpg','b.jpg'],trimmed:[{file:'old.jpg',kind:'outlier',ts:1793870000,reason:'does not resemble its own person',similar:[]}],cameras:{DOOR:2},meta:{},favorite:true};
const clusters=[[{id:'u1',camera:'DOOR',ts:1793870000,guess:'Мария',guess_score:.5,full_url:'data/full/u1.jpg'},{id:'u2',camera:'LOBBY',ts:1793870010,guess:'Иван',guess_score:.2}],[{id:'u3',camera:'HALL',ts:1793870011}]];
const results={delay:{n:4,median:3,min:1,max:8,p90:7,skipped_late:1,by_attempt:[{attempt:1,n:4,median:3,min:1,max:8}],by_camera:[{camera:'DOOR',n:4,median:3}],by_day:[{day:'2026-11-04',n:2,median:3},{day:'2026-11-05',n:2,median:4}]},coverage:{ir_cameras:['LOBBY'],people:[{name:'Иван',photos:7,diversity:.6,frontal:2,half:3,profile:2,greyscale:1,self_test:43,cameras:[{camera:'DOOR',n:7}]}],advice:[{name:'Иван',missing:['too few photos','no frontal shot','no IR night shot','only one camera (DOOR)','photos look very alike (0.88)','photos barely support each other (self test 14%)']}]},'why-no-face':{events:10,reasons:{ok:2,'no face':3,'too small':2,uncertain:1,'no snapshot':2},face_width:{n:2,median:45},min_face_px:48,cameras:[{camera:'HALL',used:false,events:3,ok:1,no_face:1,too_small:1,uncertain:0}]}};
const analysis={leave_one_out:{n:25,hit:20,wrong:0,miss:5},headroom:.12,stranger_ceiling:.48,threshold:.6,top_k:1,days:3,probe:{events:20,faces:9,selfhits:2,per_person:[{name:'Мария',hits:4,median:.7,min:.61,tight:1}]}};
const samples=[];
function sample(label,html){samples.push({label,html})}
function context(modern){
 const html=fs.readFileSync(root+'/static/'+(modern?'index.html':'classic.html'),'utf8');
 let source=[...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map(m=>m[1]).join('\n');
 source=source.slice(0,modern?source.indexOf('/* ================= start ================= */'):source.lastIndexOf('applyAdvanced();'));
 const els=new Map(),handlers={},dummy=()=>{};
 const el=id=>{if(!els.has(id))els.set(id,{innerHTML:'',textContent:'',value:'0.65',dataset:{},style:{},children:[],classList:{add:dummy,remove:dummy,toggle:dummy},addEventListener:dummy,querySelectorAll:()=>[],append:dummy,appendChild:dummy,remove:dummy,click:dummy,focus:dummy,showModal:dummy,setAttribute:dummy});return els.get(id)};
 const py=`import ast,json\nt=ast.parse(open('${root}/app/webui.py').read())\nprint(json.dumps(next(ast.literal_eval(n.value) for n in ast.walk(t) if isinstance(n,ast.Assign) and any(isinstance(x,ast.Name) and x.id=='TOOL_SPECS' for x in n.targets))))`;
 const tools=JSON.parse(cp.execFileSync('python',['-c',py],{encoding:'utf8'}));
 const state={analysis:{result:analysis},backups:{dir:'data/backups',backups:[{name:'faceid-backup.tar.gz',ts:1793870000,size:10240}]},removed:[],wrong:{found:true,person:'Мария',slug:'maria',score:.7,carried_by_one:true,top_photo:'a.jpg',without_top:.3,photos:[{file:'a.jpg',sim:.7},{file:'b.jpg',sim:.3}],runner_up:{name:'Иван',score:.4}}};
 const route=async u=>u.endsWith('/wrong')?state.wrong:u==='api/settings'?cfg:u==='api/persons'?{maria:person}:u==='api/unknowns'?clusters:u==='api/ignored'?[[{id:'i1',auto:true,from_person:'Иван',group:'g1',ts:1793870000}]]:u==='api/analysis'?state.analysis:u==='api/backups'?state.backups:u==='api/tools'?{tools,state:{}}:u==='api/gallery/quality-scan'?{removed:state.removed}:u==='api/health'?{persons:38,unknowns:1,thresholds:cfg.thresholds}:u==='api/backfill'?{running:false}:u.startsWith('api/history')?{enabled:true,items:[{id:'h1',ts:1793870000,person:'Мария',score:.7,best_score:.8,camera:'HALL',reported:false,now:{person:'Мария',self:true}}]}:[];
 const c={console,addEventListener:dummy,fetch:async u=>({ok:true,json:()=>route(u),headers:{get:()=> 'application/json'}}),window:{addEventListener:dummy,matchMedia:()=>({matches:false}),innerWidth:1200},document:{querySelector:el,querySelectorAll:()=>[],getElementById:id=>el('#'+id),addEventListener:(e,fn)=>handlers[e]??=fn,documentElement:{dataset:{}}},localStorage:{getItem:()=>null,setItem:dummy},navigator:{},location:{},setTimeout:()=>1,clearTimeout:dummy,setInterval:dummy,clearInterval:dummy,scrollTo:dummy,IntersectionObserver:function(){this.disconnect=dummy;this.observe=dummy},requestAnimationFrame:dummy,matchMedia:()=>({matches:false}),confirm:()=>false,alert:dummy,prompt:()=>null};
 vm.createContext(c);vm.runInContext(source,c);vm.runInContext(`persons=${JSON.stringify({maria:person})};${modern?"view='settings';cfg=null;":"tab='settings';groupSettings=()=>{};"}renderAnalysis=renderAnalysis;`,c);
 c.api=route;c.j=route;
 function run(js){return vm.runInContext(js,c)}
 return {c,el,state,handlers,run,html};
}
(async()=>{
 for(const modern of [false,true]){
  const p=modern?'modern':'classic',x=context(modern);sample(p+' static',x.html.replace(/<script[\s\S]*?<\/script>/g,''));
  if(modern)for(const mode of ['frigate','folder']){cfg.input_mode=mode;for(const tab of ['matching','fallback','gallery','backup']){await x.run(`setTab='${tab}';awaitX=renderSettings()`);await x.c.awaitX;sample(p+' settings '+mode+' '+tab,x.el('#main').innerHTML)}}
  else {await x.run('renderSettings()');sample(p+' settings',x.el('#main').innerHTML)}
  for(const wrong of [0,1])for(const headroom of [.02,.12]){x.state.analysis={result:{...analysis,leave_one_out:{...analysis.leave_one_out,wrong},headroom}};await x.run('renderAnalysis()');sample(p+' analysis '+wrong+' '+headroom,x.el('#anbox').innerHTML)}
  x.state.analysis={running:true,processed:3,total:10};await x.run('renderAnalysis()');sample(p+' analysis running',x.el('#anbox').innerHTML);
  await x.run(modern?'renderBackups()':'renderBackupList()');sample(p+' backups',x.el('#bklist').innerHTML);
  if(modern){await x.run("view='checks';renderChecks()");sample(p+' checks',x.el('#main').innerHTML)}
  if(modern){
   for(const js of ["view='sort';clusters="+JSON.stringify(clusters)+";renderSort()","view='people';peopleTab='people';openRow='maria';renderPeople()","view='people';peopleTab='ignored';renderIgnored()","view='add';renderAdd()","view='history';renderHistory()"]){await x.run(js);sample(p+' '+js.slice(0,40),x.el('#main').innerHTML)}
  }else{
   for(const [tab,fn]of [['unknowns','renderUnknowns'],['persons','renderPersons'],['ignored','renderIgnored'],['history','renderHistory'],['tools','renderTools']]){await x.run(`tab='${tab}';${fn}()`);sample(p+' '+tab,x.el('#main').innerHTML)}
  }
  for(const [id,data]of Object.entries(results))for(const d of [data,{error:'no history yet — recognitions are recorded from now on'},{n:0,note:'no recognitions in this window'}])sample(p+' tool '+id,x.run(`TOOL_FMT['${id}'](${JSON.stringify(d)})`));
  if(modern){
   x.run("ask=async(title,html,yes)=>{globalThis.captured=(globalThis.captured||[]).concat([title,html,yes]);return true}");
   for(const removed of [[],[{kind:'outlier',person:'Мария',file:'abc.jpg',mean_sim:.2,median:.6}]]){x.state.removed=removed;await x.handlers.click({target:{closest:()=>({dataset:{},id:'qscan'})}})}
   for(const s of x.c.captured)sample(p+' quality dialog',s);
  }
  x.run("ask=async(title,html,yes)=>{globalThis.dialogs=(globalThis.dialogs||[]).concat([title,html,yes]);return false};confirm=msg=>{globalThis.dialogs=(globalThis.dialogs||[]).concat(msg);return false};alert=msg=>{globalThis.dialogs=(globalThis.dialogs||[]).concat(msg)}");
  for(const one of [true,false]){x.state.wrong.carried_by_one=one;await x.run("markWrong('h1')")}
  if(modern){
   x.run("cfg="+JSON.stringify(cfg));
   for(const target of [{dataset:{cleartrim:'maria'}},{dataset:{ign:'maria'}},{dataset:{del:'maria'}},{dataset:{igdel:'0'}},{dataset:{},id:'dedupeRun'}])await x.handlers.click({target:{closest:()=>target}});
  }else await x.run('crossRiskScan()');
  for(const s of x.c.dialogs||[])sample(p+' action dialog',s);
  for(const bf of [{running:true,processed:2,total:10},{running:true,processed:2,total:10,mode:'folder'},{running:false,result:{processed:5,faces:3,known:2,no_face:4,skipped:0,unchanged:0}},{running:false,mode:'folder',result:{processed:5,faces:3,known:2,no_face:4,skipped:0,unchanged:0}}])sample(p+' scan',x.run(modern?`bf=${JSON.stringify(bf)};scanHtml()`:`backfillRow(${JSON.stringify(bf)})`));
  for(const kind of ['same_image','near_dup','cross','outlier']){
   const t={kind,file:'old.jpg',ts:1793870000,partner:'Иван',reason:kind==='cross'?'too close to Иван (0.750) — a reference this ambiguous makes the two confusable':kind==='outlier'?'barely resembles the other photos of Мария (0.100 where 0.450 is normal for this person) — a reference with this little face information attracts strangers instead of its own person':'',similar:[]};
   x.run('persons.maria.trimmed='+JSON.stringify([t]));sample(p+' trimmed '+kind,x.run(modern?"detailHtml('maria')":"personCard('maria',persons.maria)"));
  }
 }
 const py=`import json,sys\nfrom html.parser import HTMLParser\nclass P(HTMLParser):\n def __init__(self):super().__init__();self.skip=0;self.nodes=[]\n def handle_starttag(self,t,a):\n  if t in ('script','style','code','pre'):self.skip+=1\n  if not self.skip:\n   for k,v in a:\n    if k in ('placeholder','title','aria-label','data-tip','data-tipsub') and v:self.nodes.append(v)\n def handle_endtag(self,t):\n  if t in ('script','style','code','pre'):self.skip=max(0,self.skip-1)\n def handle_data(self,s):\n  if not self.skip and s.strip():self.nodes.append(s)\nout=[]\nfor item in json.load(sys.stdin):\n p=P();p.feed(item['html']);out.extend({'label':item['label'],'source':s} for s in p.nodes)\nprint(json.dumps(out,ensure_ascii=False))`;
 const nodes=JSON.parse(cp.execFileSync('python',['-c',py],{input:JSON.stringify(samples),encoding:'utf8'}));
 const messages=['Delete "Мария" and all their faces?','Permanently delete 2 ignore anchor(s)? This person may resurface as unknown.','Delete all set-aside photos of "Мария"? Your active reference photos stay.','Saved — 3 photo(s) trimmed','Failed: Unknown person','Restore failed: Not a valid .tar.gz backup','Restored 6 file(s) (merge) — 38 people','Restored 6 file(s) (replace) - 38 persons','3 set aside (2 duplicate image, 1 similar face)','2 face(s) assigned','2 faces → Мария · still 50: 2 similar photos set aside to keep angles varied','3 faces → Иван · 1 set aside'];
 for(const s of messages)nodes.push({label:'action dialog generated',source:s});
 const allow=/FACEID|FaceID|Frigate|go2rtc|MQTT|CPU|IR|HTTP|MB|px|UI|NN|API|data\/backups|\.tar\.gz|\.gz|DOOR|LOBBY|HALL|front_door|HD|Esc|Shift|AM|PM|ArcFace|largest face|scripts\/measure-recognition\.py|\bfaceid\b|\bv\d+\b|\bEnglish\b|\bOK\b|\bJPG\b|\bHEIC\b|\bJPEG\b|\bPNG\b|\bWebP\b|abc\.jpg|\.jpg|match_top_k|min_face_px|config\.yaml|\/media\//g;
 const missing=new Map();let checked=0;
 for(const n of nodes){const out=n.label.includes('action dialog')&&!n.source.includes('<')?langCtx.L.translateDialog(n.source):tr(n.source);checked++;const rest=out.replace(allow,'');if(/[a-z]{2,}/i.test(rest)){if(!missing.has(n.source.trim()))missing.set(n.source.trim(),{...n,translated:out})}assert.equal(tr(out),out,'Not idempotent: '+n.source)}
 
 console.log(JSON.stringify({samples:samples.length,nodes:checked,untranslated:missing.size}));
 if(missing.size){console.log([...missing.values()].map(x=>x.label+': '+JSON.stringify(x.source)+' => '+JSON.stringify(x.translated)).join('\n'));process.exitCode=1}
})().catch(e=>{console.error(e.stack);process.exit(1)});
