import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
import {webcrypto} from 'node:crypto';
let count=0;
const source=async(name)=>readFile(new URL('../'+name,import.meta.url),'utf8');
const identity=await source('identity.js');const advanced=await source('advanced.js');const actions=await source('unified-actions.js');
function environment() {
 let store={};let detachListener=null;let selected={id:17,url:'https://example.com/',windowId:9,active:false};
 const calls=[];
 const env={URL,Map,Set,Promise,Date,Number,String,Array,Boolean,Math,Error,Uint8Array,setTimeout,clearTimeout,console,crypto:webcrypto,
  BROWSER_FAMILY:'edge',BROWSER_VERSION:'154.0',EXTENSION_VERSION:'1.9.0',tabId:Number,
  chrome:{runtime:{lastError:null},storage:{local:{get(key,cb){cb(store);},set(value,cb){Object.assign(store,value);cb();}}},
   tabs:{async get(id){return {...selected,id};},async update(id,value){Object.assign(selected,value);return {...selected,id};},onRemoved:{addListener(){}}},
   windows:{async update(id,value){calls.push(['window',id,value]);}},
   permissions:{async contains(){return false;}},
   debugger:{async attach(...args){calls.push(['attach',...args]);},async detach(...args){calls.push(['detach',...args]);},
    async sendCommand(...args){calls.push(['cdp',...args]);return {root:{nodeId:1,nodeName:'HTML',nodeValue:'PRIVATE',attributes:['id','safe-id','value','PRIVATE-VALUE','class','card'],children:[{nodeId:2,nodeName:'#text',nodeValue:'PRIVATE-TEXT'}]}};},
    onDetach:{addListener(fn){detachListener=fn;}}},scripting:{}},
  location:{href:'https://example.com/'}};
 env.globalThis=env;vm.createContext(env);
 vm.runInContext(identity,env);vm.runInContext(advanced,env);vm.runInContext(actions,env);
 return {env,calls,store,detach:()=>detachListener({tabId:17})};
}
{
 const {env,store}=environment();const a=await env.browserIdentity();const b=await env.browserIdentity();
 assert.equal(a.instance_id,b.instance_id);assert.equal(a.browser_kind,'edge');assert.equal(store.xiaoruo1_browser_instance_id,a.instance_id);count+=3;
 const custom=environment();custom.env.BROWSER_FAMILY='chromium-other';custom.env.XIAORUO1_BROWSER_KIND='xiaoruo_browser';assert.equal((await custom.env.browserIdentity()).browser_kind,'xiaoruo_browser');count++;
 const other=environment();other.env.BROWSER_FAMILY='chromium-other';assert.equal((await other.env.browserIdentity()).browser_kind,'chromium_other');count++;
 assert(!identity.includes('TOKEN'));count++;
}
{
 const {env,calls}=environment();const st=await env.debugStatus({tab_id:'17'});assert.equal(st.available,false);assert.equal(st.attached,false);assert.equal(st.reason,'debugger_permission_not_granted');count+=3;
 await assert.rejects(()=>env.debugAttach({tab_id:'17'}),/confirmation_required/);count++;
 const denied=await env.debugAttach({tab_id:'17',confirm:true});assert.equal(denied.ok,false);assert.equal(calls.length,0);count+=2;
 assert(!advanced.includes('permissions.request'));assert(!advanced.includes('Runtime.evaluate'));assert(!advanced.includes('Network.'));count+=3;
}
{
 const {env,calls,detach}=environment();env.chrome.permissions.contains=async()=>true;
 const attached=await env.debugAttach({tab_id:'17',confirm:true});assert.equal(attached.attached,true);assert.equal((await env.debugStatus({tab_id:'17'})).attached,true);count+=2;
 const snap=await env.debugSnapshot({tab_id:'17'});const serialized=JSON.stringify(snap);assert(!serialized.includes('PRIVATE'));assert(serialized.includes('safe-id'));assert.equal(calls.filter(c=>c[0]==='cdp')[0][2],'DOM.getDocument');count+=3;
 detach();assert.equal((await env.debugStatus({tab_id:'17'})).attached,false);await assert.rejects(()=>env.debugSnapshot({tab_id:'17'}),/not_owned/);count+=2;
 await env.debugDetach({tab_id:'17',confirm:true});assert.equal(calls.filter(c=>c[0]==='detach').length,0);count++;
}
{
 const {env}=environment();const value=await env.activateTab({tab_id:'17'});assert(value.verified && value.active);count++;
 let scrollTop=0;const element={scrollLeft:0,get scrollTop(){return scrollTop;},scrollBy(args){scrollTop=Math.min(600,scrollTop+(args.top||0));}};
 env.document={scrollingElement:element,querySelectorAll(selector){return selector==='#one'?[element]:[];}};
 const moved=env.scrollInPage({direction:'down',pixels:600});assert.equal(moved.verified,true);assert.equal(moved.after.y,600);count+=2;
 const boundary=env.scrollInPage({direction:'down',pixels:600});assert.equal(boundary.verified,false);assert.equal(boundary.scrolled,false);count+=2;
 assert.throws(()=>env.scrollInPage({selector:'.missing'}),/ambiguous/);count++;
}
{
 const {env}=environment();const entry={tagName:'INPUT',type:'password',disabled:false};
 env.document={querySelectorAll(){return [entry];},body:{innerText:''}};
 await assert.rejects(()=>env.scopedPageAction({selector:'#password',text:'secret'},'type'),/sensitive/);count++;
 env.document.querySelectorAll=()=>[];await assert.rejects(()=>env.scopedPageAction({selector:'.ambiguous'},'click'),/ambiguous/);count++;
 const root={querySelectorAll(){return [];}};env.document.querySelectorAll=()=>[{shadowRoot:null}];await assert.rejects(()=>env.scopedPageAction({shadow_path:['#closed']},'read'),/open_shadow_root_unavailable/);count++;
}
const manifest=JSON.parse(await source('manifest.json'));assert.match(manifest.version,/^1\.9\.\d+$/);assert(!manifest.permissions.includes('debugger'));assert.deepEqual(manifest.optional_permissions,['debugger']);count+=3;
console.log(`${count} unified extension boundary checks passed; not real-browser acceptance`);
