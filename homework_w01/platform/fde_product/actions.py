"""Durable action ledger and real HTTP connector. No analytical-table writes."""
# 本地账本管理批准与恢复，HTTP 连接器操作独立标签系统，不写分析库。
import hashlib,json,sqlite3,uuid
from urllib import request,error
from urllib.parse import urlsplit
from pathlib import Path
from .models import NoRedirect

# 动作错误的机器编号由界面映射为中文提示，避免将底层异常直接展示给用户。
class ActionError(Exception):
    def __init__(self,code):self.code=code;super().__init__(code)

# 固定键顺序与分隔符，确保同一草案内容生成稳定的确认哈希。
def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()

# 样例 REST API 客户端：限制为本机 HTTP 地址，并禁止请求重定向。
class TagConnector:
    def __init__(self,url,token,timeout=5):
        p=urlsplit(url)
        if p.scheme!='http' or p.hostname not in ('127.0.0.1','localhost') or p.query or p.fragment or p.username:raise ActionError('MOCK_URL_INVALID')
        self.url=url.rstrip('/');self.token=token;self.timeout=timeout
    # 统一编码请求和映射错误；网络失败只能说明结果未知，不能证明远端没执行。
    def call(self,method,path,body=None):
        req=request.Request(self.url+path,data=None if body is None else json.dumps(body).encode(),method=method,headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'})
        try:
            with request.build_opener(NoRedirect).open(req,timeout=self.timeout) as r:return json.loads(r.read(200001))
        except error.HTTPError as e:
            status=e.code;e.close();raise ActionError('API_HTTP_'+str(status)) from None
        except (error.URLError,TimeoutError,ValueError):raise ActionError('API_RESULT_UNKNOWN') from None
    # 查询、幂等添加和撤销均以持久化 action_id 定位同一个外部动作。
    def query(self,action_id):return self.call('GET','/v1/tag-actions/'+action_id)
    def apply(self,action_id,payload):return self.call('PUT','/v1/tag-actions/'+action_id,payload)
    def undo(self,action_id):return self.call('POST','/v1/tag-actions/'+action_id+'/undo',{})

# 生命周期：draft → approved/rejected/superseded；批准后 executing → applied/unknown。
# checkpoint 保存对话进度，账本保存动作事实，两者不能互相替代。
class Ledger:
    def __init__(self,path,connector):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True);self.connector=connector
        with self.connect() as c:c.execute('CREATE TABLE IF NOT EXISTS drafts(id TEXT PRIMARY KEY,session TEXT,payload TEXT,hash TEXT,status TEXT,result TEXT)')
    # 操作使用独立短连接，状态变更由各方法中的事务保护。
    def connect(self):
        c=sqlite3.connect(self.path,timeout=15);c.row_factory=sqlite3.Row;return c
    # 读取并反序列化草案；不存在的 ID 不返回虚构的默认状态。
    def get(self,id):
        with self.connect() as c:r=c.execute('SELECT * FROM drafts WHERE id=?',(id,)).fetchone()
        if not r:raise ActionError('DRAFT_NOT_FOUND')
        d=dict(r);d['payload']=json.loads(d['payload']);d['result']=json.loads(d['result']) if d['result'] else None;return d
    # 只允许样例会员和标签；新草案使同会话尚未执行的旧草案失效。
    def draft(self,session,member_id,tag):
        if tag not in ['recall_review','follow_up'] or member_id not in [f'M{i:03}' for i in range(1,10)]:raise ActionError('NOT_ALLOWLISTED')
        payload={'member_id':member_id,'tag':tag};id=str(uuid.uuid4());h=digest({'id':id,'payload':payload})
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute("UPDATE drafts SET status='superseded' WHERE session=? AND status IN ('draft','approved')",(session,))
            c.execute('INSERT INTO drafts VALUES (?,?,?,?,?,NULL)',(id,session,json.dumps(payload),h,'draft'))
        return self.get(id)
    # 批准绑定角色、会话和内容哈希；事务内再次确认草案仍在等待处理。
    def approve(self,id,session,expected_hash,approved,role):
        if role!='operator':raise ActionError('FORBIDDEN')
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');r=c.execute('SELECT * FROM drafts WHERE id=?',(id,)).fetchone()
            if not r or r['session']!=session or r['hash']!=expected_hash:raise ActionError('APPROVAL_MISMATCH')
            if r['status']!='draft':raise ActionError('DRAFT_NOT_PENDING')
            c.execute('UPDATE drafts SET status=? WHERE id=?',('approved' if approved else 'rejected',id))
        return self.get(id)
    # 保存远端结果或错误编号，后续恢复以该状态决定查询还是执行。
    def save_result(self,id,status,result):
        with self.connect() as c:c.execute('UPDATE drafts SET status=?,result=? WHERE id=?',(status,json.dumps(result),id))
    # 首次批准执行 PUT；executing/unknown 恢复时只 GET 核对，避免盲目重发。
    def execute(self,id):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');r=c.execute('SELECT * FROM drafts WHERE id=?',(id,)).fetchone()
            if not r:raise ActionError('DRAFT_NOT_FOUND')
            if r['status'] in ('applied','undone'):return self.get(id)
            if r['status'] in ('executing','unknown'):recover=True
            elif r['status']=='approved':recover=False;c.execute("UPDATE drafts SET status='executing' WHERE id=?",(id,))
            else:raise ActionError('NOT_APPROVED')
        # 网络请求在数据库事务结束后发出，避免等待远端时长期占用写锁。
        try:result=self.connector.query(id) if recover else self.connector.apply(id,json.loads(r['payload']))
        except ActionError as e:
            # A 404 on recovery is not proof a timed-out in-flight mutation cannot commit.
            self.save_result(id,'unknown',{'code':e.code});return self.get(id)
        self.save_result(id,result['status'],result);return self.get(id)
    # 未执行的状态只读本地账本，执行过的状态才访问标签 API。
    def query(self,id):
        d=self.get(id)
        if d['status'] in ('draft','approved','rejected','superseded'):return d
        result=self.connector.query(id);self.save_result(id,result['status'],result);return self.get(id)
    # 撤销由受控入口发起，核对角色与会话后交给远端做版本冲突判断。
    def undo(self,id,session,role):
        d=self.get(id)
        if role!='operator' or d['session']!=session:raise ActionError('FORBIDDEN')
        if d['status'] not in ('applied','undone'):raise ActionError('NOT_REVERSIBLE')
        result=self.connector.undo(id);self.save_result(id,result['status'],result);return self.get(id)
