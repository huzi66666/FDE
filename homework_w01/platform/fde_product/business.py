"""Executable sample semantics. The model never supplies SQL or totals."""
# 样例数据与可执行指标：模型只传参数，SQL、计算公式与覆盖判断由程序提供。
from pathlib import Path
import csv, hashlib, json, sqlite3, re
from datetime import date

# 路径相对发布包定位；业务修订号随运行结果导出，区别于模型协议修订。
ROOT = Path(__file__).resolve().parents[2]
VERSION = '0.2.0-w1'
CONTRACT = 'w01-0.2.0'
RUNTIME_REVISION = 'w01-runtime-20260922'

# 预期业务错误使用稳定的大写编号，由 Agent 转为工具错误结果。
class BusinessError(Exception):
    pass

# 只接受已实现的净消费与老会员定义；换一个配置名称不会自动实现新指标。
def load_semantics(path=None):
    value=json.loads(Path(path or ROOT/'product/semantics.json').read_text())
    if value['spend']['aggregation']!='paid_minus_refunded_cents' or value['old_member']['basis']!='joined_before_baseline':
        raise BusinessError('UNSUPPORTED_METRIC_DEFINITION')
    return value

# 初始化专用样例库；重复导入会替换库中的样例数据，不可对客户库使用。
def import_sample(folder, database):
    folder=Path(folder); database=Path(database); database.parent.mkdir(parents=True,exist_ok=True)
    meta=json.loads((folder/'manifest.json').read_text())
    rows={name:list(csv.DictReader((folder/(name+'.csv')).open())) for name in ['stores','members','orders','coverage']}
    with sqlite3.connect(database) as c:
        # 外键检查对象关联，CHECK 约束金额和订单状态，金额单位为人民币分。
        c.execute('PRAGMA foreign_keys=ON')
        c.executescript('''CREATE TABLE IF NOT EXISTS stores(store_id TEXT PRIMARY KEY,name TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS members(member_id TEXT PRIMARY KEY,name TEXT NOT NULL,joined_at TEXT NOT NULL,store_id TEXT REFERENCES stores);
        CREATE TABLE IF NOT EXISTS orders(order_id TEXT PRIMARY KEY,member_id TEXT REFERENCES members,paid_at TEXT,paid_cents INTEGER CHECK(paid_cents>=0),refunded_cents INTEGER CHECK(refunded_cents>=0 AND refunded_cents<=paid_cents),status TEXT CHECK(status IN ('paid','cancelled')));
        CREATE TABLE IF NOT EXISTS coverage(store_id TEXT REFERENCES stores,start TEXT,end TEXT,PRIMARY KEY(store_id,start,end));
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);''')
        # One transaction: repeated imports replace the same dataset, never duplicate it.
        for name in ['orders','members','coverage','stores','metadata']:c.execute('DELETE FROM '+name)
        c.executemany('INSERT INTO stores VALUES (:store_id,:name)',rows['stores'])
        c.executemany('INSERT INTO members VALUES (:member_id,:name,:joined_at,:store_id)',rows['members'])
        c.executemany('INSERT INTO orders VALUES (:order_id,:member_id,:paid_at,:paid_cents,:refunded_cents,:status)',rows['orders'])
        c.executemany('INSERT INTO coverage VALUES (:store_id,:start,:end)',rows['coverage'])
        c.executemany('INSERT INTO metadata VALUES (?,?)',[(k,json.dumps(v,ensure_ascii=False)) for k,v in meta.items()])
    return {name:len(value) for name,value in rows.items()}

# 用户时段确认的确定性边界，不从“近期”等相对词推断用户授权。
def explicit_windows(text):
    """Recognize user-supplied calendar months or four ISO boundaries, never 'recent'.

    This is a deterministic confirmation boundary, not an intent classifier.
    Unsupported natural-language periods must be clarified into explicit dates.
    """
    # 四个 ISO 日期必须额外注明结束日不包含当天，否则交回澄清流程。
    iso=re.findall(r'(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)',text or '')
    if len(iso)==4:
        try:
            for x in iso:date.fromisoformat(x)
        except ValueError:return []
        # ISO interval ends are explicitly exclusive in the UI/API contract.
        if re.search(r'不含|不包含|左闭右开|end.exclusive|exclusive|\)',text,re.I):
            return [[iso[0],iso[1]],[iso[2],iso[3]]]
        return []
    # 中文自然月需至少出现一个年份；同年省略写法沿用最近的显式年份。
    tokens=re.findall(r'(?:(\d{4})\s*年\s*)?(\d{1,2})\s*月',text or '')
    if len(tokens)!=2:return []
    years=[int(y) for y,m in tokens if y]
    if not years:return []
    result=[];year=years[0]
    for y,m in tokens:
        year=int(y) if y else year;month=int(m)
        try:
            start=date(year,month,1);end=date(year+(month==12),1 if month==12 else month+1,1)
        except ValueError:return []
        result.append([start.isoformat(),end.isoformat()])
    return sorted(result) if len({tuple(x) for x in result})==2 else []

# 分析路径只读：导入与查询分离，避免工具调用修改业务表。
class Business:
    def __init__(self,database,semantics=None):
        self.path=Path(database).resolve(); self.semantics=load_semantics(semantics)
    # URI 只读模式加 query_only 双重限制，与 import_sample 的写连接区分。
    def connect(self):
        c=sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True);c.row_factory=sqlite3.Row
        c.execute('PRAGMA query_only=ON'); return c
    # 将标准口径、固定业务参考日期和覆盖范围作为工具上下文提供给模型。
    def describe(self):
        with self.connect() as c:
            meta={r['key']:json.loads(r['value']) for r in c.execute('SELECT * FROM metadata')}
            cov=[dict(r) for r in c.execute('SELECT * FROM coverage')]
        return {'status':'ok','semantics':self.semantics,'dataset':meta,'coverage':cov,'required':'必须由用户明确或确认两个时段，不能擅自用默认日期替代澄清。'}
    # 比较两个有序、不重叠的左闭右开区间；阈值未传时使用语义配置。
    def compare(self,baseline_start,baseline_end,current_start,current_end,store_id=None,min_decline_pct=None):
        dates=[baseline_start,baseline_end,current_start,current_end]
        try:
            for x in dates:
                if date.fromisoformat(x).isoformat()!=x:raise ValueError()
        except (ValueError,TypeError):raise BusinessError('INVALID_DATE') from None
        if not baseline_start<baseline_end<=current_start<current_end:raise BusinessError('INVALID_WINDOWS')
        cutoff=self.semantics['decline']['minimum_pct'] if min_decline_pct is None else min_decline_pct
        if not 0<=cutoff<=100:raise BusinessError('INVALID_THRESHOLD')
        with self.connect() as c:
            stores=[r[0] for r in c.execute('SELECT store_id FROM stores WHERE (? IS NULL OR store_id=?)',(store_id,store_id))]
            if not stores:raise BusinessError('UNKNOWN_STORE')
            # 两个时段都完整覆盖的门店才参与计算；未覆盖不是零消费。
            missing=[]; valid=[]
            for sid in stores:
                covered=all(c.execute('SELECT 1 FROM coverage WHERE store_id=? AND start<=? AND end>=?',(sid,a,b)).fetchone() for a,b in [(baseline_start,baseline_end),(current_start,current_end)])
                (valid if covered else missing).append(sid)
            meta={r['key']:json.loads(r['value']) for r in c.execute('SELECT * FROM metadata')}
            results=[]
            for sid in valid:
                # 老会员严格定义为基期开始前入会，不按当前日期或模型判断筛选。
                for m in c.execute('SELECT * FROM members WHERE store_id=? AND joined_at<? ORDER BY member_id',(sid,baseline_start)):
                    amounts=[];counts=[];evidence=[];breakdowns=[]
                    for a,b in [(baseline_start,baseline_end),(current_start,current_end)]:
                        orders=list(c.execute("SELECT order_id,paid_cents,refunded_cents,status FROM orders WHERE member_id=? AND paid_at>=? AND paid_at<? ORDER BY order_id",(m['member_id'],a,b)))
                        # 全额退款单仍属于已支付订单；取消单单列，不参与净消费。
                        os=[o for o in orders if o['status']=='paid'];cancelled=[o for o in orders if o['status']=='cancelled']
                        paid=sum(o['paid_cents'] for o in os);refunded=sum(o['refunded_cents'] for o in os)
                        amounts.append(paid-refunded); counts.append(len(os));evidence.append([o['order_id'] for o in os])
                        breakdowns.append({'paid_count':len(os),'fully_refunded_count':sum(o['paid_cents']>0 and o['paid_cents']==o['refunded_cents'] for o in os),'cancelled_count':len(cancelled),'paid_cents':paid,'refunded_cents':refunded,'net_cents':paid-refunded,'paid_order_ids':[o['order_id'] for o in os],'cancelled_order_ids':[o['order_id'] for o in cancelled]})
                    before,after=amounts
                    # 零基期比例无定义，用 None 保留语义，不伪造为 0% 或 100%。
                    pct=round((before-after)*100/before,2) if before>0 else None
                    results.append({'member_id':m['member_id'],'store_id':sid,'baseline_cents':before,'current_cents':after,'delta_cents':after-before,'decline_pct':pct,'baseline_orders':counts[0],'current_orders':counts[1],'declining':pct is not None and pct>=cutoff and after<before,'evidence_order_ids':evidence,'order_breakdown':dict(zip(['baseline','current'],breakdowns))})
        # 对数据元信息、查询参数和结果摘要求哈希，生成可复核的比较证据标识。
        evidence_id='CMP-'+hashlib.sha256(json.dumps([meta,dates,store_id,cutoff,results],sort_keys=True).encode()).hexdigest()[:16]
        return {'status':'partial' if missing else 'ok','evidence_id':evidence_id,'dataset_version':meta['dataset_version'],'metric_id':self.semantics['spend']['id'],'windows':{'baseline':[baseline_start,baseline_end],'current':[current_start,current_end],'end_exclusive':True},'minimum_decline_pct':cutoff,'rows':results,'declining_members':[r['member_id'] for r in results if r['declining']],'missing_coverage_stores':missing,'limitations':['未覆盖门店的消费未知，不能计为零。'] if missing else [],'business_reference_date':meta['business_reference_date']}
    # 返回单会员原始订单依据，未知会员明确报错；不替代消费比较证据。
    def history(self,member_id):
        with self.connect() as c:
            m=c.execute('SELECT member_id,store_id,joined_at FROM members WHERE member_id=?',(member_id,)).fetchone()
            if not m:raise BusinessError('UNKNOWN_MEMBER')
            return {'status':'ok','member':dict(m),'orders':[dict(r) for r in c.execute('SELECT order_id,paid_at,paid_cents,refunded_cents,status FROM orders WHERE member_id=? ORDER BY paid_at',(member_id,))]}
