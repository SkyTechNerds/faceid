// Exercise the real translation visitor on a small DOM fixture; no browser or HA needed.
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
class E{
 constructor(tag='div',attrs={}){this.nodeType=1;this.tag=tag;this.attrs={...attrs};this.children=[];this.parentElement=null;this.value='';}
 add(n){this.children.push(n);n.parentElement=this;return n;}
 closest(){return ['script','style','textarea','code','pre'].includes(this.tag)||this.attrs.contenteditable==='true'||this.attrs.class==='logline'?this:this.parentElement?.closest()||null;}
 querySelectorAll(){return this.children.flatMap(c=>c.nodeType===1?[c,...c.querySelectorAll()]:[])}
 hasAttribute(k){return k in this.attrs}getAttribute(k){return this.attrs[k]}setAttribute(k,v){this.attrs[k]=v}
}
const T=s=>({nodeType:3,nodeValue:s,parentElement:null});
const body=new E(),title=body.add(new E()).add(T('Every reference passed'));
const input=body.add(new E('input',{placeholder:'Search people','data-tip':'Restore (merge)'}));input.value='DOOR, Мария, Иван';
const name=body.add(new E()).add(T('Иван')),cam=body.add(new E()).add(T('HALL'));
const technical=['code','pre','textarea','script'].map(tag=>body.add(new E(tag)).add(T('Unknown face')));
technical.push(body.add(new E('div',{class:'logline'})).add(T('Unknown face')));
const textNodes=root=>root.children.flatMap(n=>n.nodeType===3?[n]:textNodes(n));
const document={body,readyState:'loading',addEventListener(){},querySelectorAll:()=>body.querySelectorAll(),createTreeWalker(root){let list=textNodes(root),i=0;return {nextNode(){this.currentNode=list[i++];return this.currentNode||null}}}};
let alerts=[],confirms=[],prompts=[];
const cx={document,localStorage:{getItem:()=> 'ru'},navigator:{},Node:{ELEMENT_NODE:1,TEXT_NODE:3},NodeFilter:{SHOW_TEXT:4},window:{alert:m=>alerts.push(m),confirm:m=>{confirms.push(m);return false},prompt:(m,v)=>{prompts.push([m,v]);return null}}};
vm.createContext(cx);vm.runInContext(fs.readFileSync(require('path').resolve(__dirname,'../static/assets/i18n-ru.js'),'utf8').replace(/\}\)\(\);\s*$/,'globalThis.L={visit,restore,translate,setLang:v=>lang=v};})();'),cx);
const l=cx.L;l.visit(body);
assert.equal(title.nodeValue,'Все эталонные фото прошли проверку');assert.equal(input.attrs.placeholder,'Поиск людей');
assert.equal(input.value,'DOOR, Мария, Иван');assert.equal(name.nodeValue,'Иван');assert.equal(cam.nodeValue,'HALL');
for(const t of technical)assert.equal(t.nodeValue,'Unknown face');
for(let i=0;i<10;i++)l.visit(body);assert.equal(title.nodeValue,'Все эталонные фото прошли проверку');
l.setLang('en');l.restore();assert.equal(title.nodeValue,'Every reference passed');assert.equal(input.attrs['data-tip'],'Restore (merge)');
title.nodeValue='Nothing to set aside.';l.setLang('ru');l.visit(body);assert.equal(title.nodeValue,'Проблемных фото для откладывания нет.');
title.nodeValue='Every reference passed';l.visit(title);assert.equal(title.nodeValue,'Все эталонные фото прошли проверку');
input.attrs['data-tip']='Restore (replace)';l.visit(input);assert.equal(input.attrs['data-tip'],'Восстановить с заменой');
assert.equal(cx.window.confirm('Remove duplicates in all people?'),false);assert.equal(confirms[0],'Найти и отложить дубликаты у всех людей?');
assert.equal(cx.window.prompt('New name','Иван'),null);assert.equal(prompts[0][1],'Иван');
l.setLang('en');cx.window.alert('Every reference passed');assert.equal(alerts[0],'Every reference passed');
for(const [s,w]of [['1 day','1 день'],['2 days','2 дня'],['5 days','5 дней'],['11 days','11 дней'],['21 days','21 день'],['24 faces','24 лица']])assert.equal(l.translate(s),w);
assert.equal(l.translate('Unrecognised external diagnostic'),null);
// Preserve the verified build's browser detection and explicit language override.
const source=fs.readFileSync(require('path').resolve(__dirname,'../static/assets/i18n-ru.js'),'utf8');
for(const [stored,browser,expected] of [[null,'ru-RU','ru'],[null,'en-US','en'],['en','ru-RU','en'],['ru','en-US','ru']]){
 const c={document:{readyState:'loading',addEventListener(){}},localStorage:{getItem:()=>stored},navigator:{language:browser},window:{alert(){},confirm(){return false},prompt(){return null}}};
 vm.createContext(c);vm.runInContext(source.replace(/\}\)\(\);\s*$/,'globalThis.defaultLang=lang;})();'),c);assert.equal(c.defaultLang,expected);
}
console.log(JSON.stringify({passed:true,languageRoundTrip:true,dynamicTextAndAttributes:true,editableAndTechnicalData:'unchanged',dialogReturnValues:'preserved',repeatedTranslation:'stable',russianNumberForms:6}));
