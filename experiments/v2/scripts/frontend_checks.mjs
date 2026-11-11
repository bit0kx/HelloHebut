// Real frontend request module with deterministic HTTP/stream fault injection.
import assert from 'node:assert/strict'
import { readFile, writeFile } from 'node:fs/promises'
const source = await readFile(new URL('../../../frontend/src/lib/backends.js', import.meta.url), 'utf8')
const { resumeChat, requestChat, createInitialSettings, ensureSession, saveSettings } = await import('data:text/javascript;base64,' +
  Buffer.from(source.replace('import.meta.env.VITE_PYTHON_API_URL', 'undefined')).toString('base64'))
globalThis.localStorage = { setItem() {}, getItem() { return null } }
const settings = () => ({ endpoint: 'http://fixture', sessionEndpoint:'http://fixture', sessionToken:'owner-token', conversationId:'c',
  pendingRequest:{runId:'job', id:'request', convId:'c', message:'question', sequence:1} })
const final = { status:'completed', result:{conv_id:'c', response:'校区资料已核验', entities:{province:['河北']}} }
const json = data => new Response(JSON.stringify(data), {headers:{'Content-Type':'application/json'}})
const checks = []
async function check(name, fn) {
  try { await fn(); checks.push({check:name,passed:true}) }
  catch (error) { checks.push({check:name,passed:false,error_type:error.name}); console.error(error) }
}
await check('split_utf8_sse_final_and_condition_restore', async () => {
  const s = settings(), stages = []
  globalThis.fetch = async url => {
    if (!url.includes('/events')) return json({status:'running'})
    assert.ok(url.endsWith('after_sequence=1'))
    const bytes = new TextEncoder().encode('id: 2\nevent: progress\ndata: '+JSON.stringify({sequence:2,stage:'verify'})+
      '\n\nid: 3\nevent: final\ndata: '+JSON.stringify({...final, sequence:3, type:'final'})+'\n\n')
    return new Response(new ReadableStream({start(controller) {
      for (let i=0;i<bytes.length;i+=7) controller.enqueue(bytes.slice(i,i+7))
      controller.close()
    }}))
  }
  const answer = await resumeChat(s, stage => stages.push(stage))
  assert.equal(answer.response, final.result.response)
  assert.deepEqual(stages,['verify']); assert.equal(s.pendingRequest,undefined)
  assert.deepEqual(s.lastConditions, final.result.entities)
})
await check('stream_disconnect_queries_same_job_without_resubmit', async () => {
  let calls=0; const s=settings()
  globalThis.fetch = async (url, options) => {
    assert.equal(options.method,undefined)
    assert.ok(url.includes('/chat/tasks/job'))
    if (url.includes('/events')) throw new TypeError('offline')
    return json(++calls===1 ? {status:'running'} : final)
  }
  assert.equal((await resumeChat(s)).response, final.result.response)
  assert.equal(calls,2)
})
await check('failed_task_clears_retry_key', async () => {
  const s=settings(); globalThis.fetch=async()=>json({status:'failed',error_type:'BudgetExceeded'})
  await assert.rejects(resumeChat(s)); assert.equal(s.pendingRequest,undefined)
})
await check('lost_post_response_retry_keeps_id_and_server_owned_identity', async () => {
  const s=settings()
  delete s.pendingRequest.runId
  globalThis.fetch=async(url,options)=> {
    if (options.method==='POST') {
      const body=JSON.parse(options.body)
      assert.equal(body.client_request_id,'request'); assert.equal(body.user_id,undefined)
      return json({run_id:'job'})
    }
    return json(final)
  }
  assert.equal((await requestChat(s,'question')).response,final.result.response)
})
await check('same_message_retry_queries_existing_run_without_enqueue', async () => {
  const s=settings()
  globalThis.fetch=async(url,options)=> {
    assert.equal(options.method,undefined)
    assert.ok(url.endsWith('/chat/tasks/job'))
    return json(final)
  }
  assert.equal((await requestChat(s,'question')).response,final.result.response)
})
await check('different_message_cannot_replace_pending_request', async () => {
  for (const withRunId of [true,false]) {
    const s=settings()
    if (!withRunId) delete s.pendingRequest.runId
    const original = s.pendingRequest
    globalThis.fetch=async()=>assert.fail('must not send HTTP')
    await assert.rejects(requestChat(s,'different question'))
    assert.equal(s.pendingRequest,original)
    assert.equal(s.pendingRequest.id,'request')
  }
})
await check('resume_recovers_id_when_post_response_was_lost', async () => {
  const s=settings(); delete s.pendingRequest.runId
  globalThis.fetch=async(url,options)=> {
    if (options.method==='POST') {
      assert.equal(JSON.parse(options.body).client_request_id,'request')
      return json({run_id:'job'})
    }
    return json(final)
  }
  assert.equal((await resumeChat(s)).response,final.result.response)
})
await check('expired_resume_clears_identity_conditions_and_pending', async () => {
  const s=settings(); s.userId='old'; s.lastConditions={rank:['12000']}
  globalThis.fetch=async()=>new Response('{}',{status:401})
  await assert.rejects(resumeChat(s),error=>error.status===401)
  assert.equal(s.sessionToken,''); assert.equal(s.sessionEndpoint,''); assert.equal(s.userId,'')
  assert.equal(s.conversationId,''); assert.equal(s.pendingRequest,undefined)
  assert.deepEqual(s.lastConditions,{})
})
await check('endpoint_change_never_forwards_old_bearer_or_task', async () => {
  const s=settings(); s.endpoint='http://second-backend'
  saveSettings(s)
  assert.equal(s.sessionToken,''); assert.equal(s.pendingRequest,undefined)
  globalThis.fetch=async(url,options)=> {
    assert.equal(url,'http://second-backend/session')
    assert.equal(options.headers,undefined)
    return json({session_token:'new-token',user_id:'new-owner'})
  }
  await ensureSession(s)
  assert.equal(s.sessionToken,'new-token'); assert.equal(s.sessionEndpoint,'http://second-backend')
})
await check('legacy_saved_session_is_bound_once_without_losing_owner', async () => {
  localStorage.getItem=()=>JSON.stringify({endpoint:'http://fixture/',sessionToken:'old-token',
    userId:'old-owner',conversationId:'c',pendingRequest:{id:'old-request',message:'old'}})
  const s=createInitialSettings()
  assert.equal(s.sessionEndpoint,'http://fixture'); assert.equal(s.sessionToken,'old-token')
  s.endpoint='http://changed'; saveSettings(s)
  assert.equal(s.sessionToken,''); assert.equal(s.pendingRequest,undefined)
  localStorage.getItem=()=>null
})
await check('late_session_response_cannot_overwrite_new_backend_session', async () => {
  const s={endpoint:'http://first',sessionToken:'',sessionEndpoint:''}
  let resolveOld
  globalThis.fetch=async url=>url==='http://first/session'
    ? new Promise(resolve=>{resolveOld=resolve}) : json({session_token:'new',user_id:'new-owner'})
  const first=ensureSession(s)
  s.endpoint='http://second'
  await ensureSession(s)
  resolveOld(json({session_token:'old',user_id:'old-owner'}))
  await assert.rejects(first)
  assert.equal(s.sessionToken,'new'); assert.equal(s.sessionEndpoint,'http://second')
})
const report = {mode:'frontend_http_and_stream_fault_fixtures',checks,
  passed:checks.filter(c=>c.passed).length,total:checks.length,
  limitations:'Verifies request/reconnect contracts, not browser layout or visual inspection.'}
await writeFile(new URL('../reports/stage6_frontend.json',import.meta.url),JSON.stringify(report,null,2)+'\n')
console.log(JSON.stringify({passed:report.passed,total:report.total}))
process.exitCode = report.passed===report.total ? 0 : 1
