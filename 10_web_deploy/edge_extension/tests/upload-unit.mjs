// Unit tests run the production upload functions with a small DOM/Chrome
// boundary double. Real browser and Bilibili acceptance are separate tests.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
import {webcrypto, createHash} from 'node:crypto';
import {File} from 'node:buffer';

const source = await readFile(new URL('../upload.js', import.meta.url), 'utf8');
const token = '12345678-1234-4234-8234-123456789abc';
const selector = `input[type="file"][data-xiaoruo1-upload-id="${token}"]`;
const pageUrl = 'http://127.0.0.1:48272/upload-test/fixture.html';
const bytes = Buffer.from('real-file-bytes-unit-fixture');
const digest = createHash('sha256').update(bytes).digest('hex');
let count = 0;

function environment(overrides = {}) {
  const input = {isConnected:true, disabled:false, type:'file', accept:'video/mp4', files:[], events:[], dispatchEvent(event){ this.events.push(event.type); }};
  const env = {
    console, URL, Map, File, Event, Uint8Array, Array, Promise, atob, setTimeout, clearTimeout,
    crypto:webcrypto, location:{href:pageUrl}, BROWSER_FAMILY:'edge',
    document:{querySelectorAll(value){return value === selector ? [input] : []; }},
    DataTransfer:class { constructor(){this.files=[];this.items={add:(value)=>this.files.push(value)};} },
    tabId:Number,
    chrome:{tabs:{async get(){return {id:7,url:pageUrl};}},scripting:{}},
    __xiaoruo1UploadObservation:{url:pageUrl,document_id:'doc-unit',entries:new Map([[token,{element:input,observed_at:Date.now()}]])},
    ...overrides
  };
  env.globalThis=env;
  vm.createContext(env);
  vm.runInContext(source,env);
  env.executeAllFrames=async(_tab,func,args)=>[{frameId:0,result:await func(...args)}];
  env.chrome.scripting.executeScript=async({func,args})=>[{frameId:0,result:await func(...args)}];
  const payload = {tab_id:'7',selector,name:'fixture.mp4',size:bytes.length,mime_type:'video/mp4',data_base64:bytes.toString('base64'),sha256:digest,idempotency_key:'fixture-operation-0001',expected_url:pageUrl,timeout:1000,confirm:true};
  return {env,input,payload};
}
async function rejects(label, mutate, pattern) {
  const e=environment(); mutate(e);
  await assert.rejects(()=>e.env.uploadFile(e.payload),pattern,label);
  assert.equal(e.input.events.length,0,label+' must not send change');
  count++;
}

{
  const {env,input,payload}=environment();
  const selected=await env.uploadFile(payload);
  assert.equal(selected.file_selected,true);
  assert.equal(selected.selected_file.name,payload.name);
  assert.equal(selected.selected_file.size,bytes.length);
  assert.equal(selected.selected_file.sha256,digest);
  assert.equal(selected.stages.server_received,null);
  assert.equal(selected.stages.platform_processed,null);
  assert.equal(createHash('sha256').update(Buffer.from(await input.files[0].arrayBuffer())).digest('hex'),digest);
  assert.deepEqual(input.events,['input','change']);
  const duplicate=await env.uploadFile(payload);
  assert.equal(duplicate.duplicate,true);
  assert.equal(input.events.length,2);
  await assert.rejects(()=>env.uploadFile({...payload,name:'different.mp4'}),/idempotency_conflict/);
  await assert.rejects(()=>env.uploadFile({...payload,idempotency_key:'fixture-operation-0002'}),/already_selected/);
  count+=4;
}
await rejects('must be Edge',e=>e.env.BROWSER_FAMILY='chromium-other',/requires_edge/);
await rejects('confirm false',e=>e.payload.confirm=false,/confirmation_required/);
await rejects('confirm truthy string',e=>e.payload.confirm='true',/confirmation_required/);
await rejects('required key',e=>e.payload.idempotency_key='',/idempotency_key_required/);
await rejects('origin denied',e=>e.payload.expected_url='https://example.com/',/page_not_allowed_or_changed/);
await rejects('ambiguous name cannot select first',e=>e.payload.selector='input[name="buploader"]',/selector_not_observed/);
await rejects('duplicate observed attribute',e=>e.env.document.querySelectorAll=()=>[e.input,e.input],/target_ambiguous/);
await rejects('disabled target',e=>e.input.disabled=true,/target_disabled/);
await rejects('stale observation',e=>e.env.__xiaoruo1UploadObservation.entries.get(token).observed_at-=901000,/observation_stale/);
await rejects('disconnected target',e=>e.input.isConnected=false,/observation_stale/);
await rejects('size ceiling',e=>e.payload.size=32*1024*1024+1,/size_not_allowed/);
await rejects('empty file',e=>e.payload.size=0,/size_not_allowed/);
await rejects('sensitive extension',e=>e.payload.name='browser.token',/filename_not_allowed/);
await rejects('path cannot be filename',e=>e.payload.name='../../video.mp4',/filename_not_allowed/);
await rejects('accept mismatch',e=>e.input.accept='image/png',/accept_mismatch/);
await rejects('MIME must match extension',e=>e.payload.mime_type='text/plain',/mime_mismatch/);
await rejects('bad digest',e=>e.payload.sha256='0'.repeat(64),/hash_mismatch/);
await rejects('wrong size',e=>e.payload.size++,/size_mismatch/);
await rejects('bad base64',e=>e.payload.data_base64='!notbase64',/bytes_invalid/);
for (const mutation of ['disabled','selected','replaced']) {
  const {env,input,payload}=environment();
  env.crypto={randomUUID:webcrypto.randomUUID.bind(webcrypto),subtle:{digest:async(...args)=>{
    const answer=await webcrypto.subtle.digest(...args);
    if (mutation==='disabled') input.disabled=true;
    if (mutation==='selected') input.files=[new File(['user selection'],'user.mp4',{type:'video/mp4'})];
    if (mutation==='replaced') env.document.querySelectorAll=()=>[{...input}];
    return answer;
  }}};
  await assert.rejects(()=>env.uploadFile(payload),/target_changed_before_selection|observation_stale/);
  assert.equal(input.events.length,0,'Mid-digest change must not assign or dispatch');
  const repeat=await env.uploadFile(payload);
  assert.equal(repeat.action_may_have_run,false,'Known refusal is cached rather than evicted and retried');
  count++;
}
{
  const {env,input,payload}=environment();
  env.chrome.scripting.executeScript=()=>new Promise(()=>{});
  payload.timeout=1000;
  const first=assert.rejects(()=>env.uploadFile(payload),/timeout_outcome_unknown_do_not_repeat/);
  await new Promise(resolve=>setTimeout(resolve,10));
  await assert.rejects(()=>env.uploadFile({...payload,idempotency_key:'fixture-operation-competing'}),/target_busy_or_outcome_unknown/);
  await first;
  await assert.rejects(()=>env.uploadFile(payload),/outcome_unknown_do_not_repeat/);
  assert.equal(input.events.length,0);
  count+=2;
}
assert.equal(environment().env.uploadAllowedUrl(pageUrl),true);
assert.equal(environment().env.uploadAllowedUrl('http://127.0.0.1:48272/upload-test/fixture.html?other=1'),false);
assert.equal(environment().env.uploadAllowedUrl('https://member.bilibili.com/platform/upload/video/frame'),true);
assert.equal(environment().env.uploadAllowedUrl('https://member.bilibili.com.evil.invalid/platform/upload'),false);
assert.equal(environment().env.uploadAllowedUrl('https://member.bilibili.com:444/platform/upload/video/frame'),false);
assert.equal(environment().env.uploadAllowedUrl('https://member.bilibili.com/platform/home'),false);
console.log(`${count+6} upload unit checks passed; browser integration not implied.`);
