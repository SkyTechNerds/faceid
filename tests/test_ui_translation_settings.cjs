const fs=require('fs'),vm=require('vm'),assert=require('assert/strict'),cp=require('child_process');
const root=process.argv[2]||require('path').resolve(__dirname,'../static');
const code=fs.readFileSync(root+'/assets/i18n-ru.js','utf8');
const cx={localStorage:{getItem:()=> 'ru'},navigator:{language:'ru'},window:{alert:()=>{},confirm:()=>false,prompt:()=>null},document:{readyState:'loading',addEventListener:()=>{}}};
vm.createContext(cx);
vm.runInContext(code.replace(/\}\)\(\);\s*$/,'globalThis.testAPI={translate,translateDialog,setLang:v=>lang=v};})();'),cx);
const tr=s=>cx.testAPI.translate(s)??s;
const cfg={input_mode:'frigate',thresholds:{match_threshold:.5,unknown_threshold:.35,suggest_threshold:.4,cluster_eps:.55,ignore_threshold:.5,dedupe_threshold:.65},ranges:{match_threshold:[.2,.9],unknown_threshold:[.1,.8],suggest_threshold:[.1,.9],cluster_eps:[.3,.8],ignore_threshold:[.1,.9],dedupe_threshold:[.5,.95]},max_faces_per_person:40,match_top_k:3,min_face_px:48,max_attempts:6,max_ignore_anchors:0,trimmed_keep:10,hires_enroll:true,clip_fallback:true,clip_fallback_cameras:['DOOR','LOBBY','HALL'],live_hires_fallback:true,live_hires_fallback_cameras:['DOOR','LOBBY','HALL'],live_hires_mode:'always',known_cameras:['DOOR','LOBBY','HALL'],backup:{enabled:false,hour:3,keep:7,dir:''},folder:{path:'/media/faceid',camera:'front_door',processed:0,indexed:0,index_cap:5000}};
(async()=>{
 const classic=fs.readFileSync(root+'/classic.html','utf8');
 const settings=classic.slice(classic.indexOf('async function renderSettings(){'),classic.indexOf('// Die Einstellungen sind'));
 const ctx={tab:'settings',main:{innerHTML:''},j:async()=>cfg,esc:s=>s,document:{getElementById:()=>null},groupSettings:()=>{},dedupePreview:()=>{},renderBackupList:()=>{},renderAnalysis:()=>{},advanced:()=>false};vm.createContext(ctx);vm.runInContext(settings,ctx);await ctx.renderSettings();
 const parse=`import json,sys\nfrom html.parser import HTMLParser\nclass P(HTMLParser):\n def __init__(self):super().__init__();self.section='';self.h=False;self.code=0;self.nodes=[]\n def handle_starttag(self,t,a):\n  if t=='h2':self.h=True\n  if t=='code':self.code+=1\n  for k,v in a:\n   if k in ('title','placeholder','aria-label') and v:self.nodes.append({'text':v,'section':self.section,'attr':k})\n def handle_endtag(self,t):\n  if t=='h2':self.h=False\n  if t=='code':self.code-=1\n def handle_data(self,s):\n  if self.h:self.section=s.strip()\n  if s.strip() and not self.code:self.nodes.append({'text':s,'section':self.section})\np=P();p.feed(sys.stdin.read());print(json.dumps(p.nodes,ensure_ascii=False))`;
 const nodes=JSON.parse(cp.execFileSync('python',['-c',parse],{input:ctx.main.innerHTML,encoding:'utf8'}));
 const sections=['Keeping the gallery clean','Remove duplicate photos','Does it actually work?','Automatic backup','Manual backup & restore'];
 let checked=0,untranslated=[];
 for(const n of nodes){if(!sections.includes(n.section))continue;const t=tr(n.text);checked++;let clean=t.replace(/FaceID|data\/backups|\.tar\.gz/g,'');if(/[A-Za-z]{2,}/.test(clean))untranslated.push({source:n.text,translated:t});assert.equal(tr(t),t,'Translation is not stable: '+n.text);}
 assert.deepEqual(untranslated,[],JSON.stringify(untranslated,null,2));
 for(const value of ['DOOR','LOBBY','HALL','Мария','Иван','data/backups'])assert.equal(tr(value),value);
 let previewCount=0;
 for(const modern of [false,true])for(const [same,similar,thr]of [[0,0,.7],[0,15,.65],[2,15,.65],[1,1,.6]]){
  const html=modern?fs.readFileSync(root+'/index.html','utf8'):classic;
  const start=html.indexOf('function dedupePreview()'),end=html.indexOf(modern?'let anTimer;':'async function runDedupe()',start);
  const p={textContent:''},el={value:String(thr)};
  const c={_dedupeTimer:null,dedTimer:null,clearTimeout:()=>{},setTimeout:fn=>{c.pending=Promise.resolve(fn());return 1;},document:{getElementById:id=>id==='dedupePrev'?p:el},$:id=>id==='#dedupePrev'?p:el,j:async()=>({same_image:same,similar_face:similar}),api:async()=>({same_image:same,similar_face:similar})};
  vm.createContext(c);vm.runInContext(html.slice(start,end),c);c.dedupePreview();await c.pending;
  const result=tr(p.textContent);assert(!/would|near-identical|identical|restorable|nothing to remove|sensitivity/i.test(result),result);assert(result.includes(thr.toFixed(2)),result);if(similar)assert(result.includes(String(similar)),result);previewCount++;
 }
 const messages=['Restore (replace)','Restore (merge)','write a backup into the auto-backup folder now'];
 for(const msg of messages){cx.testAPI.setLang('ru');assert.notEqual(cx.testAPI.translateDialog(msg),msg);cx.testAPI.setLang('en');assert.equal(cx.testAPI.translateDialog(msg),msg);}
 assert(classic.includes('id="bkHour"')&&classic.includes('onchange="restore(this,false)"')&&classic.includes('onchange="restore(this,true)"'));
 console.log(JSON.stringify({passed:true,settingsTextNodes:checked,dynamicPreviews:previewCount,englishDialogPassthrough:messages.length,technicalIdentifiers:'unchanged'}));
})().catch(e=>{console.error(e.stack);process.exit(1)});
