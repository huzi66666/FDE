"""Program-owned date confirmation and factual presentation boundaries."""
# 纯程序交互辅助：规范化日期方案、解析用户确认，并从工具结果呈现事实。
from datetime import date
import re

# 保留机器错误码用于定位，界面展示稳定的中文操作提示。
ERROR_MESSAGES = {
    'CONFIRM_OR_CANCEL_PENDING_DRAFT': '还有一份标签草案等待处理。请先点击“确认并执行”或“拒绝”，再提出新的要求。',
    'NO_PENDING_DRAFT': '这份草案已处理，当前没有等待确认的标签草案。',
    'APPROVAL_MISMATCH': '草案已变化，请核对当前显示的草案后再确认。',
    'TIME_OPTION_EXPIRED': '日期方案已失效，请使用本轮显示的方案，或直接输入两个完整月份。',
    'TIME_RANGE_NOT_CONFIRMED': '请确认两个比较时段，可点击本轮日期方案，或输入带年份的两个完整月份。',
    'INVALID_REQUEST': '请求不完整，请检查输入后重试。',
    'FORBIDDEN': '当前身份不能执行此操作。',
    'SESSION_CONTEXT_LIMIT': '这个任务的对话已较长，请新建任务继续。',
    'SESSION_INTERRUPTED': '上一轮在完成前中断，这个任务无法继续。请新建任务。',
}

# 未收录的错误使用通用文案，提示查看证据中的原始编号。
def error_message(code):
    return ERROR_MESSAGES.get(code, '操作未完成，请查看工具依据中的错误编号并重试。')

# 最多接受三个合法、不重叠且不重复的日期方案；输出 ID 和可点击标签。
def date_options(value):
    """Only concrete, ordered ISO intervals can become selectable proposals."""
    options=[]
    if not isinstance(value,list):return options
    for item in value[:3]:
        try:
            if not isinstance(item,dict):continue
            a,b=item['baseline'],item['current']
            if not isinstance(a,list) or not isinstance(b,list) or len(a)!=2 or len(b)!=2:continue
            dates=a+b
            if any(not isinstance(x,str) or date.fromisoformat(x).isoformat()!=x for x in dates):continue
            if not dates[0]<dates[1]<=dates[2]<dates[3]:continue
            windows=[a,b]
            if any(o['windows']==windows for o in options):continue
            options.append({'id':'T'+str(len(options)+1),'windows':windows,
                            'label':f'基准 {a[0]} 至 {a[1]}；比较 {b[0]} 至 {b[1]}（均不含结束日）'})
        except (KeyError,ValueError,TypeError):continue
    return options

# 兼容模型把方案写进正文的情况，只提取明确的 ISO 边界，仍需用户确认。
def date_options_from_text(text):
    """Recover explicit ISO proposals when the model wrote them in text."""
    if not isinstance(text,str):return []
    proposals=[]
    pattern=re.compile(r'(?:baseline|基准)\s*[:：]?\s*(\d{4}-\d{2}-\d{2})\s*(?:~|～|至|到)\s*(\d{4}-\d{2}-\d{2}).{0,80}?(?:current|比较)\s*[:：]?\s*(\d{4}-\d{2}-\d{2})\s*(?:~|～|至|到)\s*(\d{4}-\d{2}-\d{2})',re.I|re.S)
    for match in pattern.finditer(text):
        proposals.append({'baseline':[match[1],match[2]],'current':[match[3],match[4]]})
        if len(proposals)>=3:break
    return date_options(proposals)

# 自然月偏移统一返回月初，通过整除处理跨年，不依赖每月天数。
def _add_months(value,months):
    month=value.month+months
    year=value.year+(month-1)//12
    month=(month-1)%12+1
    return date(year,month,1)

# 模型未给出合法方案时，按最近的业务描述构造参考日期之前两个完整月。
# 此函数只提出候选，不授予查询权限，也不使用电脑当前日期。
def date_options_from_business(observations):
    """Create safe default proposals from program-owned business coverage."""
    if not isinstance(observations,list):return []
    for observation in reversed(observations):
        if observation.get('name')!='describe_business':continue
        result=observation.get('result') or {}
        dataset=result.get('dataset') or {}
        coverage=result.get('coverage') or []
        try:
            reference=date.fromisoformat(dataset['business_reference_date'])
            current_end=date(reference.year,reference.month,1)
            current_start=_add_months(current_end,-1)
            baseline_start=_add_months(current_end,-2)
            baseline_end=current_start
            option={'baseline':[baseline_start.isoformat(),baseline_end.isoformat()],
                    'current':[current_start.isoformat(),current_end.isoformat()]}
            if not any(isinstance(c,dict) and c.get('start')<=option['baseline'][0] and c.get('end')>=option['current'][1] for c in coverage):
                return []
            return date_options([option])
        except (KeyError,TypeError,ValueError):return []
    return []

# 只把明确的肯定选择识别为确认；询问、否定或修改日期都不能授权查询。
def selected_option(text,options):
    # A narrow affirmative prefix prevents mentions, questions and negations
    # from authorizing a query. Explicit dates take precedence in Agent.run.
    match=re.match(r'^\s*(?:就)?(?:按|选择|选|采用|确认)(?:照)?(?:你(?:刚才)?(?:说|给出|推荐)的)?第([一二三123])个(?:方案|时段|日期)?(?:[，,。\s]|$)',text or '')
    if not match or re.search(r'不|别|取消|不要|[？?]|是否|能否|什么|改|换|年|月|季度|近期|最近|\d{4}-\d{2}',text):return None
    index='一二三'.find(match[1]) if match[1] in '一二三' else int(match[1])-1
    return options[index] if index<len(options) else None

# 存储和计算使用整数分，仅在最终展示时换算为元。
def yuan(cents):return f'{cents/100:g}'

# 按工具明细生成固定事实说明，强调全额退款单是支付笔数的子集。
def factual_rationale(row,windows):
    before,after=windows['baseline'],windows['current']
    parts=[f'基准时段 {before[0]} 至 {before[1]}，比较时段 {after[0]} 至 {after[1]}（均不含结束日）。',
           f"净消费从 {yuan(row['baseline_cents'])} 元变为 {yuan(row['current_cents'])} 元，下降 {row['decline_pct']:g}%。"]
    for label,key in [('基准','baseline'),('比较','current')]:
        b=row['order_breakdown'][key]
        subset=f"（其中 {b['fully_refunded_count']} 笔全额退款，已包含在已支付笔数内）" if b['fully_refunded_count'] else ''
        detail=f"{label}时段有 {b['paid_count']} 笔已支付订单{subset}，支付 {yuan(b['paid_cents'])} 元，退款 {yuan(b['refunded_cents'])} 元，净消费 {yuan(b['net_cents'])} 元"
        if not b['paid_count']:detail=f"{label}时段没有已支付订单，净消费为 0 元"
        if b['cancelled_count']:detail+=f"；另有 {b['cancelled_count']} 笔取消订单未计入"
        parts.append(detail+'。')
    return ''.join(parts)

# 只处理已完成分析；重写摘要、理由和限制，保留模型生成的 suggestion。
def ground_analysis(result,comparisons):
    """Render quantities from tools while preserving model-generated suggestions."""
    if result.get('status')!='completed' or not comparisons:return result
    # comparisons 由调用方传入实际引用的比较结果，呈现不重新查询数据库。
    members={m for c in comparisons for m in c['declining_members']}
    periods=['基准 '+c['windows']['baseline'][0]+' 至 '+c['windows']['baseline'][1]+'，比较 '+c['windows']['current'][0]+' 至 '+c['windows']['current'][1]+f"，下降阈值 {c['minimum_decline_pct']:g}%" for c in comparisons]
    summary='；'.join(periods)+'（均不含结束日）。'+f'本轮比较识别到 {len(members)} 位达到下降阈值的老会员。'
    missing=sorted({s for c in comparisons for s in c.get('missing_coverage_stores',[])})
    if missing:summary+='门店 '+ '、'.join(missing)+' 缺少所选时段数据，未计入判断，不能按零消费处理。'
    recs=[]
    for rec in result.get('recommendations',[]):
        evidence=[(r,c) for c in comparisons for r in c['rows'] if r['member_id']==rec['member_id'] and r['declining']]
        recs.append({**rec,'rationale':'\n'.join(factual_rationale(r,c['windows']) for r,c in evidence)})
    # 合并工具限制并去重；建议效果和消费下降原因仍需要业务人员判断。
    limitations=list(dict.fromkeys([x for c in comparisons for x in c.get('limitations',[])]+['召回建议由模型生成，消费下降原因及具体触达方案仍需业务人员核实。']))
    return {**result,'summary':summary,'recommendations':recs,'limitations':limitations}
