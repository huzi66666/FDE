"""LangGraph product runtime; model choices, typed business tools, durable approval."""
# 运行内核：模型只提出工具调用；事实、权限、人工确认及落盘由程序控制。
from typing import TypedDict
from pathlib import Path
import json,sqlite3,threading,hashlib,uuid,re,time
from jsonschema import validate,ValidationError
from langgraph.graph import StateGraph,START,END
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import interrupt,Command
from langsmith import tracing_context
from .business import Business,BusinessError,ROOT,VERSION,RUNTIME_REVISION,explicit_windows
from .actions import Ledger,ActionError,TagConnector
from .models import ModelError, call_summary
from .observability import setup
from .interaction import date_options,date_options_from_business,date_options_from_text,selected_option,ground_analysis

# checkpoint 保存的图状态；messages 跨轮保留，observations 在新一轮重置。
class State(TypedDict,total=False):
    messages:list
    steps:int
    repairs:int
    usage:dict
    confirmed_windows:list
    time_options:list
    time_options_token:str
    observations:list
    draft:dict|None
    result:dict
    action_mode:bool
    session:str

# 组合业务只读库、动作账本、模型适配器和持久化状态图。
class Agent:
    def __init__(self,home,model,mock_url='http://127.0.0.1:8766',mock_token='training-demo-token',role='analyst',semantics=None,prompt=None,otlp_endpoint=None,otlp_headers=None):
        self.home=Path(home);self.home.mkdir(parents=True,exist_ok=True);self.model=model;self.role=role
        # 分库存储业务数据、动作账本与会话；checkpoint 不代替外部动作的幂等控制。
        self.business=Business(self.home/'sample.sqlite',semantics);self.ledger=Ledger(self.home/'actions.sqlite',TagConnector(mock_url,mock_token))
        self.prompt=Path(prompt or ROOT/'product/system_prompt.txt').read_text();self.prompt_hash=hashlib.sha256(self.prompt.encode()).hexdigest()
        self.provider,self.tracer=setup(self.home/'trace.jsonl',otlp_endpoint,otlp_headers)
        self.connection=sqlite3.connect(self.home/'checkpoints.sqlite',check_same_thread=False);self.saver=SqliteSaver(self.connection);self.lock=threading.RLock()
        # 主循环：model ↔ tools；有草案则进入 approval，批准后 execute 再回到 model。
        graph=StateGraph(State)
        for name,fn in [('model',self.model_node),('tools',self.tool_node),('approval',self.approval_node),('execute',self.execute_node)]:graph.add_node(name,fn)
        graph.add_edge(START,'model');graph.add_conditional_edges('model',self.after_model)
        graph.add_conditional_edges('tools',lambda s:'approval' if s.get('draft') else 'model')
        graph.add_conditional_edges('approval',lambda s:END if s.get('result',{}).get('status')=='rejected' else 'execute')
        graph.add_edge('execute','model');self.graph=graph.compile(checkpointer=self.saver)
    # 关闭追踪提供者与长期持有的 checkpoint 连接。
    def close(self):self.provider.shutdown();self.connection.close()
    # 扩展入口按需导入；当前 W1 包未包含 natural_knowledge 实现。
    def answer_business_model(self,workspace,config_path,question,**options):
        from .natural_knowledge import answer_business_model
        with self.lock,tracing_context(enabled=False),self.tracer.start_as_current_span('agent.business_model'):
            return answer_business_model(self,workspace,config_path,question,**options)

    # 客户知识问答的扩展入口；当前 W1 包未包含 customer_knowledge 实现。
    def answer_knowledge(self,workspace,config_path,case_id,*,include_knowledge=True):
        # Customer definitions use a fresh, tool-free context. W1 graph state and
        # durable approval are neither consumed nor changed by this entry point.
        from .customer_knowledge import answer_knowledge
        with self.lock,tracing_context(enabled=False),self.tracer.start_as_current_span('agent.knowledge'):
            return answer_knowledge(self,workspace,config_path,case_id,include_knowledge=include_knowledge)

    # 生成 Function Calling 定义；动作开关与主机 operator 角色必须同时满足。
    def schemas(self,action_mode):
        specs=[('describe_business','查询样例的对象、可执行口径、业务参考日期和数据覆盖',{},[]),('compare_spending','比较已明确的两个时段的老会员净消费，筛选下降人群，返回证据。结束日期不包含当天。',{'baseline_start':{'type':'string'},'baseline_end':{'type':'string'},'current_start':{'type':'string'},'current_end':{'type':'string'},'store_id':{'type':'string'},'min_decline_pct':{'type':'number','minimum':0,'maximum':100}},['baseline_start','baseline_end','current_start','current_end']),('member_history','读取一个会员的订单依据',{'member_id':{'type':'string'}},['member_id'])]
        if action_mode and self.role=='operator':
            specs += [('prepare_tag','创建样例标签草案，必须由界面明确确认后才能执行。只支持一个会员。',{'member_id':{'type':'string'},'tag':{'enum':['recall_review','follow_up']}},['member_id','tag']),('query_tag','通过 HTTP 核对本任务已创建的标签动作',{'action_id':{'type':'string'}},['action_id'])]
        return [{'type':'function','function':{'name':n,'description':d,'parameters':{'type':'object','properties':p,'required':r,'additionalProperties':False}}} for n,d,p,r in specs]
    # 模型节点：提供本轮上下文，处理工具请求或校验最终 JSON。
    def model_node(self,state):
        if state['steps']>=10:return {'result':{'status':'incomplete','summary':'达到调用上限，请查看工具记录。'}}
        # 只收集本轮成功取得的证据；历史消息中的结论不能直接充当本轮结果。
        comparisons=[o['result'] for o in state['observations'] if o['name']=='compare_spending' and o['result'].get('evidence_id')]
        actions=[o['result'] for o in state['observations'] if o['name'] in ('tag_execution','query_tag') and o['result'].get('id')]
        turn_context='\n本轮执行边界：历史消息只帮助理解需求，历史工具结果不能替代本轮证据。历史中的输出修复指令只针对当时的轮次。本轮已取得有效消费比较 '+str(len(comparisons))+' 次，动作结果 '+str(len(actions))+' 次。用户已确认时段：'+json.dumps(state.get('confirmed_windows',[]),ensure_ascii=False)+'。处理消费分析时，本轮缺少比较证据应按当前需求调用工具，没有确认时段时先澄清。已有本轮有效证据时，可以据此组织最终回答。标签任务按动作工具及受控确认流程办理，无需补充消费比较时段。'
        with self.tracer.start_as_current_span('model.call') as span:
            span.set_attribute('model.name',getattr(self.model,'model','scripted'));span.set_attribute('step',state['steps'])
            try:message,usage=self.model.complete([{'role':'system','content':self.prompt+turn_context+'\n当前主机模式：'+('样例动作模式。可提案和查询；批准、执行与撤销由受控入口处理。' if state['action_mode'] else '只读分析模式，没有标签动作权限。')}]+state['messages'],self.schemas(state['action_mode']))
            except ModelError as e:
                span.set_attribute('error.code',e.code);span.set_attribute('model.call',json.dumps(call_summary(self.model),ensure_ascii=False));return {'result':{'status':'incomplete','summary':e.code},'steps':state['steps']+1}
            span.set_attribute('model.call',json.dumps(call_summary(self.model),ensure_ascii=False))
            for k in ['prompt_tokens','completion_tokens','total_tokens']:
                if isinstance(usage.get(k),(int,float)):span.set_attribute(k,usage[k])
        update={'messages':state['messages']+[message],'steps':state['steps']+1,'usage':{k:state.get('usage',{}).get(k,0)+usage.get(k,0) for k in ['prompt_tokens','completion_tokens','total_tokens'] if isinstance(usage.get(k,0),(int,float))}}
        # 带工具调用时交给工具节点；无工具调用时才尝试解析最终结果。
        if not message.get('tool_calls'):
            try:
                result=json.loads(message.get('content') or '{}')
                if not isinstance(result,dict) or result.get('status') not in ['completed','clarification','refused','incomplete']:raise ValueError()
                # 动作完成须有已生效或已撤销的状态，证据引用必须来自本轮动作。
                if result['status']=='completed' and actions:
                    if actions[-1]['status'] not in ('applied','undone'):raise ValueError()
                    if not result.get('evidence_ids') or not set(result['evidence_ids'])<={a['id'] for a in actions}:raise ValueError()
                # 当前实现按全部比较结果的会员并集校验名单，允许引用证据的子集。
                if result['status']=='completed' and not actions:

                    if not comparisons:raise ValueError()
                    ids={c['evidence_id'] for c in comparisons};members={m for c in comparisons for m in c['declining_members']}
                    if not set(result.get('evidence_ids',[])) or not set(result['evidence_ids'])<=ids:raise ValueError()
                    if {r.get('member_id') for r in result.get('recommendations',[])}!=members:raise ValueError()
                # An empty result caused by missing coverage is not a completed
                # comparison. Decide this from tool evidence, independent of the
                # model's wording; partial coverage with usable rows stays valid.
                # 呈现使用实际引用的证据；完全未覆盖的空结果不能当成分析完成。
                cited=[c for c in comparisons if c['evidence_id'] in result.get('evidence_ids',[])]
                if result['status']=='completed' and not actions and cited and all(not c.get('rows') for c in cited) and any(c.get('missing_coverage_stores') for c in cited):
                    stores=sorted({s for c in cited for s in c.get('missing_coverage_stores',[])})
                    result={**result,'status':'incomplete','recommendations':[],
                            'summary':'、'.join(stores)+' 的所选比较时段缺少数据，暂时无法形成下降会员名单。缺失数据不能按零消费处理。'}
                    with self.tracer.start_as_current_span('output.normalize') as correction:
                        correction.set_attributes({'reason':'DATA_COVERAGE_INSUFFICIENT','model_status':'completed','status':'incomplete'})
                # 日期方案依次取结构化输出、文本提取、业务参考日期兜底；仍需用户确认。
                if result['status']=='clarification':
                    options=date_options(result.get('time_options',[]))
                    if not options:
                        options=date_options_from_text(result.get('summary',''))
                    if not options:
                        options=date_options_from_business(state.get('observations',[]))
                    result.pop('time_options',None)
                    update['time_options']=options
                    update['time_options_token']=uuid.uuid4().hex if options else ''
                    if options:
                        result['summary']='请确认比较时段。可点击下面的日期方案，或回复“按第一个方案”等；也可直接输入带年份的两个完整月份。'
                if result['status']=='completed' and not actions:
                    # 用程序生成事实摘要和理由，保留模型的业务建议。
                    result=ground_analysis(result,cited)
                update['result']=result
                update['messages'][-1]={**message,'content':json.dumps(result,ensure_ascii=False)}
            # 格式或依据不合格时最多修复两次；缺证据与已有证据采用不同修复指令。
            except (ValueError,TypeError,AttributeError) as exc:
                code='MODEL_OUTPUT_INVALID_JSON' if isinstance(exc,json.JSONDecodeError) else 'MODEL_OUTPUT_UNGROUNDED'
                if state.get('repairs',0)<2 and state['steps']<9:
                    update['repairs']=state.get('repairs',0)+1
                    if not comparisons and not actions:
                        repair='本轮尚无可用于完成回答的工具证据，历史工具结果不能替代本轮查询。'
                        repair+=('如果当前需求是消费分析，请根据当前需求和已确认时段调用 compare_spending，再基于本轮结果生成最终 JSON。' if state.get('confirmed_windows') else '如果当前需求是消费分析，比较时段尚未确认，请返回 clarification，询问明确时段；不要猜测时段查询。')
                        repair+='动作只能通过本轮提供的工具和受控确认入口办理，不能复用历史执行结论。'
                    else:
                        repair='请仅修复最终 JSON 的格式、引用或建议名单；保持本轮已有工具事实，evidence_ids 必须逐字引用本轮返回的 evidence_id 或动作 id，不自行增添前缀。消费分析的建议名单必须对应本轮 declining_members。不重复查询，不改变数据。'
                    update['messages'] += [{'role':'user','content':'系统输出校验失败：'+code+'。'+repair+'不输出代码围栏。'}]
                else:update['result']={'status':'incomplete','summary':code}

        return update
    # 有最终结果则结束；否则按是否存在 tool_calls 选择执行工具或继续修复输出。
    def after_model(self,state):
        return END if state.get('result') else ('tools' if state['messages'][-1].get('tool_calls') else 'model')
    # 工具分发不执行模型生成的代码，只调用白名单中固定的 Python 实现。
    def tool_node(self,state):
        messages=list(state['messages']);observations=list(state['observations']);draft=None
        tools={t['function']['name']:t['function']['parameters'] for t in self.schemas(state['action_mode'])}
        calls=messages[-1].get('tool_calls',[])
        for i,call in enumerate(calls):
            name=call.get('function',{}).get('name','');result={}
            with self.tracer.start_as_current_span('tool.'+name) as span:
                span.set_attribute('tool.name',name)
                try:
                    # 限制单条模型消息的工具调用数，并在业务执行前校验参数 Schema。
                    if i>=8:raise BusinessError('TOO_MANY_TOOL_CALLS')
                    if name not in tools:raise ActionError('TOOL_FORBIDDEN')
                    args=json.loads(call['function']['arguments']);validate(args,tools[name])
                    if name=='describe_business':result=self.business.describe()
                    elif name=='compare_spending':
                        # 工具提出的时段须精确等于用户已确认的范围，不能自行猜测。
                        requested=[[args['baseline_start'],args['baseline_end']],[args['current_start'],args['current_end']]]
                        if requested!=state.get('confirmed_windows',[]):raise BusinessError('TIME_RANGE_NOT_CONFIRMED')
                        result=self.business.compare(**args)
                    elif name=='member_history':result=self.business.history(**args)
                    elif name=='prepare_tag':
                        # 此处只写本地草案，真正的标签 HTTP 写入在人工批准之后。
                        if draft:raise ActionError('ONE_DRAFT_PER_STEP')
                        draft=self.ledger.draft(state['session'],**args);result={'status':'awaiting_approval','draft':draft}
                    elif name=='query_tag':
                        # 动作 ID 不能跨会话查询；尚未执行的草案由账本直接返回。
                        d=self.ledger.get(args['action_id'])
                        if d['session']!=state['session']:raise ActionError('FORBIDDEN')
                        result=self.ledger.query(args['action_id'])
                # 预期的参数与业务错误作为工具结果反馈，供模型澄清或解释失败。
                except (KeyError,ValueError,ValidationError):result={'status':'error','code':'INVALID_ARGUMENTS'}
                except (BusinessError,ActionError) as e:result={'status':'error','code':str(e)}
                span.set_attribute('result.status',result.get('status','ok'))
                if result.get('evidence_id'):span.set_attribute('evidence.id',result['evidence_id'])
            observations.append({'name':name,'result':result,'call_id':call.get('id','invalid')})
            # tool_call_id 将工具结果关联回原调用，下一次请求继续携带完整往返消息。
            messages.append({'role':'tool','tool_call_id':call.get('id','invalid'),'content':json.dumps(result,ensure_ascii=False)})
        return {'messages':messages,'observations':observations,'draft':draft}
    # interrupt 将图挂起并持久化；只有独立确认请求才能恢复这份草案。
    def approval_node(self,state):
        if self.role!='operator':raise ActionError('FORBIDDEN')
        d=state['draft'];approval=interrupt({'kind':'tag_confirmation','draft_id':d['id'],'draft_hash':d['hash'],'payload':d['payload'],'message':'确认后会真实调用本地样例标签 API。'})
        if not isinstance(approval,dict) or not isinstance(approval.get('approved'),bool):raise ActionError('INVALID_CONFIRMATION')
        # Role comes from the host configuration, never from the model or request body.
        # 节点恢复时重新读账本，防止只凭旧 checkpoint 重复批准或使用失效哈希。
        current=self.ledger.get(d['id'])
        if current['status']=='draft':current=self.ledger.approve(d['id'],state['session'],approval.get('draft_hash',''),approval['approved'],self.role)
        elif current['hash']!=approval.get('draft_hash') or current['status'] not in ('approved','applied','unknown','executing','rejected'):raise ActionError('APPROVAL_MISMATCH')
        # 拒绝直接结束本轮，不再调用模型，也不执行外部动作。
        if current['status']=='rejected':
            result={'status':'rejected','summary':'已拒绝这份标签草案，未执行本次添加操作。你可以继续提出新的要求。','evidence_ids':[current['id']]}
            return {'draft':None,'result':result,'observations':state['observations']+[{'name':'tag_rejection','result':current}],
                    'messages':state['messages']+[{'role':'assistant','content':json.dumps(result,ensure_ascii=False)}]}
        return {'draft':current,'messages':state['messages']+[{'role':'user','content':'系统记录的确认结果：'+current['status']}]}
    # 执行交给账本决定首次写入还是恢复查询；未知结果也按真实状态回传。
    def execute_node(self,state):
        with self.tracer.start_as_current_span('action.execute') as span:
            result=self.ledger.execute(state['draft']['id']);span.set_attribute('action.status',result['status']);span.set_attribute('action.id',result['id'])
        return {'draft':None,'observations':state['observations']+[{'name':'tag_execution','result':result}], 'messages':state['messages']+[{'role':'user','content':'系统核验的标签执行结果：'+json.dumps(result,ensure_ascii=False)}]}
    # 统一处理新请求、日期选择与审批恢复，返回不含完整私有对话的公开运行记录。
    def run(self,text=None,session=None,action_mode=False,resume=None,time_choice=None):
        session=session or str(uuid.uuid4())
        if not isinstance(session,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}',session):raise ActionError('INVALID_SESSION_ID')
        config={'configurable':{'thread_id':session},'recursion_limit':40}
        # 实例锁覆盖模型等待过程：同一 Agent 的多会话串行，非跨进程锁。
        # 关闭此处 LangSmith 隐式追踪，使用显式配置的本地 OTel 记录。
        with self.lock,tracing_context(enabled=False),self.tracer.start_as_current_span('agent.run') as span:
            span.set_attribute('session.id',session);span.set_attribute('product.version',VERSION);span.set_attribute('prompt.sha256',self.prompt_hash);span.set_attribute('model.mode',self.model.mode)
            current=self.graph.get_state(config)
            # 恢复请求继续现有图，不重建输入；普通请求才初始化新一轮状态。
            if resume is not None:
                if not current.next:raise ActionError('NO_PENDING_DRAFT')
                input=Command(resume=resume)
            else:
                # A paused graph without a draft means the previous turn stopped midway, not that approval is pending.
                if current.next:raise ActionError('CONFIRM_OR_CANCEL_PENDING_DRAFT' if current.values.get('draft') else 'SESSION_INTERRUPTED')
                previous=current.values.get('messages',[])
                if len(previous)>100:raise ActionError('SESSION_CONTEXT_LIMIT')
                # 用户明确日期优先；按钮选择还须核对当前会话保存的方案 token。
                scope=explicit_windows(text or '')
                options=current.values.get('time_options',[])
                choice=None
                if time_choice is not None:
                    if not isinstance(time_choice,dict) or not options or time_choice.get('token')!=current.values.get('time_options_token'):raise ActionError('TIME_OPTION_EXPIRED')
                    choice=next((o for o in options if o['id']==time_choice.get('id')),None)
                    if not choice:raise ActionError('TIME_OPTION_EXPIRED')
                elif not scope:choice=selected_option(text or '',options)
                if choice:
                    scope=choice['windows']
                    text=(text or '')+'\n用户确认日期方案：'+choice['label']
                elif time_choice is not None:raise ActionError('TIME_OPTION_EXPIRED')
                if not scope and current.values.get('confirmed_windows'):
                    # Only carry a scope when the follow-up introduces no new date terms.
                    if not re.search(r'年|月|季度|近期|最近|\d{4}-\d{2}|week|month|recent',text or '',re.I):scope=current.values['confirmed_windows']
                # 保留历史消息帮助理解，但重置步数、证据、修复计数和结果。
                input={'confirmed_windows':scope,'time_options':[],'time_options_token':'','messages':previous+[{'role':'user','content':text or ''}],'steps':0,'repairs':0,'usage':{},'observations':[],'draft':None,'result':{},'action_mode':bool(action_mode and self.role=='operator'),'session':session}
            # 支持 deadline 的模型适配器按本轮 600 秒预算限制每次 HTTP 等待。
            if hasattr(self.model,"deadline"):self.model.deadline=time.monotonic()+600
            out=self.graph.invoke(input,config)
            pauses=out.get('__interrupt__',())
            # 公开结果只导出工具观察、确认信息与摘要，不直接导出完整 messages。
            safe={'product_version':VERSION,'runtime_revision':RUNTIME_REVISION,'session_id':session,'model_mode':self.model.mode,'model_name':getattr(self.model,'model','scripted'),'prompt_sha256':self.prompt_hash,'status':'awaiting_approval' if pauses else out.get('result',{}).get('status','incomplete'),'result':out.get('result',{}),'observations':out.get('observations',[]),'steps':out.get('steps',0),'output_repairs':out.get('repairs',0),'usage':out.get('usage',{}),'trace_id':format(span.get_span_context().trace_id,'032x')}
            if pauses:safe['confirmation']=pauses[0].value
            if out.get('time_options') and not pauses:
                safe['time_options']=out['time_options'];safe['time_options_token']=out['time_options_token']
            # 同一会话的每轮记录使用独立文件名，方便按 trace_id 关联追踪。
            (self.home/'runs').mkdir(exist_ok=True);(self.home/'runs'/(session+'-'+uuid.uuid4().hex[:8]+'.json')).write_text(json.dumps(safe,ensure_ascii=False,indent=2))
            span.set_attribute('result.status',safe['status']);return safe
