"""Small local review UI; confirmation is a distinct human request."""
# 本机单页界面与 HTTP 入口；按钮只发起请求，权限与执行状态由后端校验。
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlsplit
import json
from .actions import ActionError
from .interaction import error_message
# HTML、CSS 和 JavaScript 内嵌为静态页面；展示建议、证据、日期选择及审批卡片。
PAGE=r'''<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>标准会员运营 Agent</title><style>body{font:16px system-ui;max-width:980px;margin:40px auto;padding:0 24px;color:#17304b;background:#f5f7fa}textarea{width:100%;height:100px;box-sizing:border-box}button{padding:10px 18px;margin:8px;border:0;border-radius:8px;background:#215ed2;color:white}pre{white-space:pre-wrap;background:white;padding:20px;border-radius:12px}#approval{border:2px solid #dd9b31;padding:16px}small{color:#586b80}</style><h1>标准会员运营 Agent</h1><p>松果生活 · 独立虚构样例 · 业务参考日期：2026-09-01</p><small>分析默认只读。动作模式仅操作本地样例标签系统；需要逐项明确确认。</small><p><label><input id="actions" type="checkbox">样例标签动作模式</label></p><textarea id="text">找出 2026 年 8 月相较 7 月消费下降至少 20% 的老会员，比较前后消费，给出召回建议并说明依据。</textarea><p><button id="send">发送</button><button id="new">新任务</button></p><div id="approval" hidden><p id="draft"></p><button id="yes">确认并执行</button><button id="no">拒绝</button></div><div id="dates" hidden><p>确认比较时段（不含结束日）：</p><div id="date-buttons"></div></div><p id="status"></p><pre id="answer"></pre><details><summary>工具依据与运行记录</summary><pre id="evidence"></pre></details><button id="undo" hidden>撤销刚才的标签动作</button><script>
// 页面内保存会话、待确认草案与最近动作；刷新页面不会自动恢复这些变量。
let session=null,confirmation=null,lastAction=null;const el=id=>document.getElementById(id);
// 统一发送 JSON 和本机客户端标记，将后端中文错误作为界面异常显示。
async function call(path,data){let r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-FDE-Client':'local-ui'},body:JSON.stringify(data)});let v=await r.json();if(!r.ok)throw Error(v.message||v.error);return v}
// 将一次运行或撤销响应映射为界面状态；显示后端事实，不在浏览器重新计算。
function display(v){
 session=v.session_id||session;
// 每次替换旧日期按钮，确认时携带当前方案 token，防止使用过期方案。
 el('date-buttons').replaceChildren();el('dates').hidden=!(v.time_options?.length);
 for(const option of v.time_options||[]){const b=document.createElement('button');b.textContent=option.label;b.onclick=()=>busy(()=>call('/api/select-time',{session_id:session,option_id:option.id,options_token:v.time_options_token,action_mode:el('actions').checked}));el('date-buttons').append(b);}
 const labels={completed:'已完成',clarification:'需要补充条件',refused:'当前操作不允许',incomplete:'尚未完成',awaiting_approval:'等待你确认',applied:'标签已生效',unknown:'执行结果待核对',undone:'标签动作已撤销',rejected:'已拒绝执行'};
// 从本轮工具观察中取最近的执行记录；撤销接口返回的是账本对象而非完整运行。
 const executed=(v.observations||[]).filter(o=>o.name==='tag_execution').at(-1)?.result;
 const actual=executed?.result||(!v.session_id&&v.result?.action_id?v.result:null);
 el('status').textContent=actual?(labels[actual.status]||actual.status):(labels[v.status]||v.status||'已完成');
 const r=v.result||{};let lines=[];
 if(actual){lines.push((labels[actual.status]||actual.status)+'；会员 '+actual.payload.member_id+'；标签 '+actual.payload.tag);lines.push('当前标签：'+(actual.label_active?'存在':'不存在')+'；版本 '+actual.label_version);}
 else if(executed){lines.push(labels[executed.status]||executed.status);}
 if(r.summary)lines.push(r.summary);
 for(const rec of r.recommendations||[])lines.push('会员 '+rec.member_id+'：'+rec.suggestion+'\n依据：'+rec.rationale);
 if(r.limitations?.length)lines.push('适用限制：\n'+r.limitations.join('\n'));
 if(r.evidence_ids?.length)lines.push('依据编号：'+r.evidence_ids.join('、'));
// 使用 textContent 展示模型和工具文本，避免把内容当成 HTML 注入页面。
 el('answer').textContent=lines.join('\n\n');
 el('evidence').textContent=JSON.stringify(v,null,2);
// 只有后端返回待确认对象才显示审批按钮；撤销按钮仅针对已生效动作。
 confirmation=v.confirmation;el('approval').hidden=!confirmation;el('draft').textContent=confirmation?'会员 '+confirmation.payload.member_id+'；添加标签 '+confirmation.payload.tag+'。确认后调用本地样例标签系统。':'';
 lastAction=executed?.status==='applied'?executed.id:null;el('undo').hidden=!lastAction;
}
// 请求期间禁用按钮防止重复提交；无论成功失败都恢复交互。
async function busy(fn){for(let b of document.querySelectorAll('button'))b.disabled=true;el('status').textContent='正在运行…';try{display(await fn())}catch(e){el('status').textContent=e.message}finally{for(let b of document.querySelectorAll('button'))b.disabled=false}}
// 发送、选日期、确认、拒绝和撤销走不同接口；新任务只清理页面引用。
el('send').onclick=()=>busy(()=>call('/api/run',{text:el('text').value,session_id:session,action_mode:el('actions').checked}));el('new').onclick=()=>{session=null;confirmation=null;lastAction=null;el('approval').hidden=true;el('undo').hidden=true;el('answer').textContent='新任务';el('dates').hidden=true;el('date-buttons').replaceChildren();el('status').textContent='';el('evidence').textContent='';};for(let [id,approved] of [['yes',true],['no',false]])el(id).onclick=()=>busy(()=>call('/api/resume',{session_id:session,draft_hash:confirmation.draft_hash,approved}));el('undo').onclick=()=>busy(()=>call('/api/undo',{session_id:session,action_id:lastAction}));</script></html>'''

# 创建服务但不启动循环；启动开关限制浏览器能否请求动作模式。
def make_server(agent,port=0,actions=False):
    class Handler(BaseHTTPRequestHandler):
        # 关闭默认访问日志；返回内容显式标注编码并禁止缓存。
        def log_message(self,*a):pass
        def send(self,status,value,html=False):
            data=(value if html else json.dumps(value,ensure_ascii=False)).encode();self.send_response(status);self.send_header('Content-Type','text/html; charset=utf-8' if html else 'application/json');self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(data)
        # 只提供根页面，不暴露运行数据库或证据文件为静态资源。
        def do_GET(self):
            if self.path!='/':return self.send(404,{'error':'NOT_FOUND'})
            self.send(200,PAGE,True)
        # Host、Origin 和客户端标记是本地访问约束，不等同于生产身份认证。
        def do_POST(self):
            host=self.headers.get('Host','');origin=self.headers.get('Origin')
            if host not in [f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'] or self.headers.get('X-FDE-Client')!='local-ui' or (origin and origin not in ['http://'+host]):return self.send(403,{'error':'ORIGIN_REJECTED'})
            try:
                # 先限制请求体大小，再校验各接口字段；错误统一返回结构化 JSON。
                n=int(self.headers.get('Content-Length','0'))
                if not 0<n<=12000:raise ValueError()
                body=json.loads(self.rfile.read(n));session=body.get('session_id')
                # 普通请求与日期选择进入 Agent；审批通过 resume 恢复已保存的图。
                if self.path=='/api/run':
                    text=body.get('text')
                    if not isinstance(text,str) or not text.strip() or len(text)>6000:raise ValueError()
                    result=agent.run(text,session,bool(actions and body.get('action_mode')))
                elif self.path=='/api/select-time':
                    if not session:raise ValueError()
                    result=agent.run(session=session,action_mode=bool(actions and body.get('action_mode')),time_choice={'id':body.get('option_id'),'token':body.get('options_token')})
                elif self.path=='/api/resume':
                    if not session:raise ValueError()
                    result=agent.run(session=session,resume={'approved':body.get('approved'),'draft_hash':body.get('draft_hash')})
                # 撤销直接走账本的角色、会话和状态校验，不交给模型决定。
                elif self.path=='/api/undo':result=agent.ledger.undo(body['action_id'],session,agent.role)
                else:return self.send(404,{'error':'NOT_FOUND'})
                self.send(200,result)
            except (ValueError,TypeError,KeyError,AttributeError):self.send(400,{'error':'INVALID_REQUEST','message':error_message('INVALID_REQUEST')})
            except ActionError as e:self.send(409,{'error':e.code,'message':error_message(e.code)})
    return ThreadingHTTPServer(('127.0.0.1',port),Handler)

# 前台服务循环由 Ctrl+C 结束；Agent 和伴随 Mock 的清理由命令行入口负责。
def serve(agent,port,actions):
    server=make_server(agent,port,actions);print('Product UI: http://127.0.0.1:'+str(server.server_port),flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
