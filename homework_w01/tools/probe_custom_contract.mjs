// Minimal independent Node implementation: CSV semantics + model tool use + HTTP API.
// This probe is evidence of interoperability, not a full self-built course submission.
// 独立 Node 互操作探针：验证 CSV 口径、模型工具调用和标签 API，不是完整 Agent 底座。
import {readFile,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {randomUUID} from 'node:crypto';
const root=resolve(import.meta.dirname,'..');
// 简化 CSV 读取器只适用于当前不含引号转义、字段内逗号的教学数据。
async function csv(name){let [head,...rows]=(await readFile(resolve(root,'sample_data',name+'.csv'),'utf8')).trim().split(/\r?\n/).map(x=>x.split(','));return rows.map(r=>Object.fromEntries(head.map((k,i)=>[k,r[i]])));}
// 并行读取独立实现所需的原始样例与标准语义，不复用 Python 分析结果。
const [members,orders,coverage,semantics]=await Promise.all([csv('members'),csv('orders'),csv('coverage'),readFile(resolve(root,'product/semantics.json'),'utf8').then(JSON.parse)]);
// 固定实现左闭右开区间、基期前入会、支付减退款和 20% 下降阈值。
// 未覆盖门店不参与名单；零基期比例用 null 表示，不当成零下降。
function compare(a){for(const k of ['baseline_start','baseline_end','current_start','current_end'])if(!/^\d{4}-\d{2}-\d{2}$/.test(a[k]||''))throw Error('INVALID_WINDOWS');if(!(a.baseline_start<a.baseline_end&&a.baseline_end<=a.current_start&&a.current_start<a.current_end))throw Error('INVALID_WINDOWS');let missing=coverage.filter(c=>!(c.start<=a.baseline_start&&c.end>=a.current_end)).map(c=>c.store_id);let rows=members.filter(m=>m.joined_at<a.baseline_start&&!missing.includes(m.store_id)).map(m=>{let sum=(start,end)=>orders.filter(o=>o.member_id===m.member_id&&o.status==='paid'&&o.paid_at>=start&&o.paid_at<end).reduce((s,o)=>s+Number(o.paid_cents)-Number(o.refunded_cents),0);let before=sum(a.baseline_start,a.baseline_end),after=sum(a.current_start,a.current_end);return {member_id:m.member_id,baseline_cents:before,current_cents:after,decline_pct:before?(before-after)*100/before:null};});return {status:missing.length?'partial':'ok',rows,declining_members:rows.filter(r=>r.decline_pct!==null&&r.decline_pct>=20&&r.current_cents<r.baseline_cents).map(r=>r.member_id),missing_coverage_stores:missing};}
// 将两种只读能力声明为 Function Calling 工具；本探针不提供模型审批工具。
const tools=[{type:'function',function:{name:'describe_business',description:'读取业务口径与覆盖',parameters:{type:'object',properties:{},required:[],additionalProperties:false}}},{type:'function',function:{name:'compare_spending',description:'对用户明确的两个时段比较老会员净消费并筛选下降会员，结束日期不含当天',parameters:{type:'object',properties:Object.fromEntries(['baseline_start','baseline_end','current_start','current_end'].map(k=>[k,{type:'string'}])),required:['baseline_start','baseline_end','current_start','current_end'],additionalProperties:false}}}];
// 模型配置从环境读取，URL 必须是完整 HTTPS 地址且不带查询参数或内嵌凭据。
const env=process.env,url=new URL(env.FDE_MODEL_URL);if(url.protocol!=='https:'||url.username||url.password||url.search||url.hash)throw Error('INVALID_MODEL_URL');
// 固定任务用于跨语言对照；该示例没有完整的会话持久化和日期确认机制。
let messages=[{role:'system',content:'你是独立实现的会员运营 Agent。根据用户目标选择工具，数字来自工具。先了解口径，再比较时段。最终用中文说明建议与依据，不要声称已经执行动作。'},{role:'user',content:'找出 2026 年 8 月相较 7 月消费下降至少 20% 的老会员，比较前后消费并给召回建议，覆盖未知不能算零。'}],observations=[],answer='',calls=0;
// 最多十轮模型往返：执行已知工具，把结果按 tool_call_id 反馈到消息历史。
// 每次请求独立超时并拒绝重定向；工具实际执行发生在本进程，不由模型直接访问数据库。
for(;calls<10;calls++){let r=await fetch(url,{method:'POST',redirect:'error',signal:AbortSignal.timeout(90000),headers:{'Authorization':'Bearer '+env.FDE_MODEL_KEY,'Content-Type':'application/json'},body:JSON.stringify({model:env.FDE_MODEL_NAME,messages,tools,tool_choice:'auto',stream:false})});if(!r.ok)throw Error('MODEL_HTTP_'+r.status);let m=(await r.json()).choices[0].message;messages.push(m);if(!m.tool_calls?.length){answer=m.content;break;}for(let c of m.tool_calls){let a=JSON.parse(c.function.arguments),v=c.function.name==='describe_business'?{semantics,coverage}:c.function.name==='compare_spending'?compare(a):{error:'TOOL_FORBIDDEN'};observations.push({name:c.function.name,result:v});messages.push({role:'tool',tool_call_id:c.id,content:JSON.stringify(v)});}}
// 用样例名单与最终文本进行最小互操作判断，不等同于完整证据校验或建议质量评估。
let analysis=observations.find(o=>o.name==='compare_spending')?.result;let report={implementation:'independent_node',scope:'Interoperability probe only; not full self-built baseline',model_mode:'live',model_name:env.FDE_MODEL_NAME,calls:calls+1,analysis_pass:JSON.stringify(analysis?.declining_members)===JSON.stringify(['M001','M004','M007'])&&['M001','M004','M007'].every(id=>answer?.includes(id)),answer,observations};
// 若提供 Mock 地址，程序直接测试添加、重复添加、查询及撤销；这是合成测试操作。
// 此路径不实现人工审批流程，只操作教学会员 M002 的 follow_up 标签。
if(env.FDE_MOCK_URL){let id=randomUUID(),payload={member_id:'M002',tag:'follow_up'};async function api(method,path,body){let r=await fetch(env.FDE_MOCK_URL+path,{method,headers:{Authorization:'Bearer '+(env.FDE_MOCK_TOKEN||'training-demo-token'),'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});if(!r.ok)throw Error('MOCK_HTTP_'+r.status);return r.json();}let first=await api('PUT','/v1/tag-actions/'+id,payload),repeat=await api('PUT','/v1/tag-actions/'+id,payload),read=await api('GET','/v1/tag-actions/'+id),undo=await api('POST','/v1/tag-actions/'+id+'/undo',{});report.http_pass=first.status==='applied'&&repeat.label_version===first.label_version&&read.label_active===true&&undo.label_active===false;}
// 保存独立探针报告；同名文件会覆盖，检查失败时设置非零退出码。
await writeFile(resolve(root,'artifacts/w01/custom-contract-probe.json'),JSON.stringify(report,null,2));console.log(JSON.stringify({analysis_pass:report.analysis_pass,http_pass:report.http_pass,scope:report.scope}));if(!report.analysis_pass||report.http_pass===false)process.exitCode=1;
