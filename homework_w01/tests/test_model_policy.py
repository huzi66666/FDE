"""No network: exercise response truncation, protocol and retry boundaries."""
# 模型适配层的离线回归：替换 HTTP 发送器，验证请求内容、重试与追踪脱敏。
import json
import time
from copy import deepcopy
from unittest.mock import patch
import pytest
from fde_product.models import CompatibleModel,ModelError,call_summary

# 构造供应商响应，包含私有推理标记和用量，便于断言它们的保留/过滤边界。
def body(finish='stop',tokens=12,content='{}',tools=None):
    m={'role':'assistant','content':content,'reasoning_content':'PRIVATE_REASONING'}
    if tools:m['tool_calls']=tools
    return {'id':'as-test','choices':[{'finish_reason':finish,'message':m}],
            'usage':{'prompt_tokens':20,'completion_tokens':tokens,'total_tokens':20+tokens,
                     'completion_tokens_details':{'reasoning_tokens':tokens-2}}}

# example.invalid 与虚构密钥仅用于测试；测试会拦截网络发送，不访问真实模型。
def make(**kw):return CompatibleModel(url='https://example.invalid/chat',key='PRIVATE_KEY',model='test',**kw)

# 首次截断只扩大额度重试一次；下一次逻辑请求恢复默认预算，输入不得变化。
def test_truncation_replays_identical_input_once_then_restores_base_budget():
    m=make();calls=[];responses=iter([body('length',8192,''),body(),body()])
    def send(p,t):calls.append((deepcopy(p),t));return next(responses),{'x-request-id':'trace-test'}
    m._send=send
    messages=[{'role':'user','content':'question'}]
    answer,usage=m.complete(messages,[])
    assert answer['content']=='{}' and usage['completion_tokens']==8204
    assert [p['max_tokens'] for p,_ in calls]==[8192,16384]
    assert [t for _,t in calls]==[90,180]
    assert calls[0][0]['messages']==calls[1][0]['messages']==messages
    assert m.last_call['attempts'][0]['error']=='MODEL_OUTPUT_TRUNCATED'
    assert m.last_call['attempts'][1]['response_id']=='as-test'
    assert 'PRIVATE_REASONING' not in json.dumps(m.last_call)
    assert 'question' not in json.dumps(call_summary(m))
    assert 'PRIVATE_KEY' not in json.dumps(m.last_call)
    m.complete(messages,[])
    assert calls[-1][0]['max_tokens']==8192 and calls[-1][1]==90

# 两次截断都不能把不完整工具调用交给 Agent，但已知消耗仍记录在适配器里。
def test_second_truncation_is_terminal_and_partial_tool_calls_never_escape():
    tool=[{'id':'partial','type':'function','function':{'name':'side_effect','arguments':'{}'}}]
    m=make()
    with patch.object(m,'_send',side_effect=[(body('length',8192,'',tool),{}),(body('length',16384,'',tool),{})]) as send:
        with pytest.raises(ModelError,match='MODEL_OUTPUT_TRUNCATED'):m.complete([],[])
        assert send.call_count==2
    assert m.last_call['usage']['completion_tokens']==24576

# 用量不足以证明输出截断，或供应商内容过滤时，不允许扩容重试。
@pytest.mark.parametrize('response,code',[(body('length',10,''),'MODEL_LENGTH_LIMIT_UNCONFIRMED'),(body('content_filter'),'MODEL_FINISH_CONTENT_FILTER')])
def test_non_output_limit_does_not_expand_budget(response,code):
    m=make()
    with patch.object(m,'_send',return_value=(response,{})) as send:
        with pytest.raises(ModelError,match=code):m.complete([],[])
        assert send.call_count==1

# 普通超时只尝试一次；没有供应商用量时保持未知，不伪造消耗。
def test_timeout_is_not_misclassified_as_output_truncation():
    m=make()
    with patch.object(m,'_send',side_effect=TimeoutError()) as send:
        with pytest.raises(ModelError,match='MODEL_TIMEOUT'):m.complete([],[])
        assert send.call_count==1 and m.last_call['usage']=={}

# 扩容重试仍受整轮剩余预算约束；已过期的任务在发请求前就应停止。
def test_retry_respects_remaining_turn_deadline():
    m=make();calls=[];m.deadline=time.monotonic()+30
    def send(p,t):
        calls.append(t)
        if len(calls)==1:return body('length',8192,''),{}
        return body(),{}
    m._send=send;m.complete([],[])
    assert len(calls)==2 and all(0<t<=30 for t in calls)
    m.deadline=time.monotonic()-1
    with pytest.raises(ModelError,match='MODEL_TURN_TIMEOUT'):m.complete([],[])

# 通用 profile 不夹带厂商参数；实际协议续接保留私有推理，公开记录将其过滤。
def test_provider_profiles_are_explicit_and_public_trace_omits_private_history():
    plain=make()._payload([],[])
    assert 'thinking' not in plain and 'reasoning_effort' not in plain
    m=make(request_profile='deepseek-low',token_limit_field='max_completion_tokens')
    payload=m._payload([],[])
    assert payload['thinking']=={'type':'enabled'} and payload['reasoning_effort']=='low'
    assert payload['max_completion_tokens']==8192 and 'max_tokens' not in payload
    messages=[{'role':'assistant','content':'','reasoning_content':'PRIVATE_OLD','tool_calls':[] }]
    with patch.object(m,'_send',return_value=(body(),{})) as send:
        m.complete(messages,[])
        assert send.call_args[0][0]['messages'][0]['reasoning_content']=='PRIVATE_OLD'
    assert 'PRIVATE_OLD' not in json.dumps(m.last_call)

# 核对真正编码进 HTTP 请求体的字段层级，防止仅测试中间配置而遗漏线路格式。
@pytest.mark.parametrize('profile,nested',[('gateway-low',True),('gateway-json',True),('deepseek-low',False),('deepseek-json',False)])
def test_actual_http_body_uses_endpoint_specific_thinking_location(profile,nested):
    model=make(request_profile=profile);captured=[]
    # 伪造可用作上下文管理器的 HTTP 响应，不建立网络连接。
    class Response:
        headers={}
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,*args):return json.dumps(body()).encode()
    # 在请求对象层捕获 JSON，验证课程网关与直连端点的差异。
    class Opener:
        def open(self,req,timeout):captured.append(json.loads(req.data));return Response()
    with patch('fde_product.models.request.build_opener',return_value=Opener()):
        model.complete([{'role':'user','content':'test'}],[])
    sent=captured[0]
    assert ('extra_body' in sent)==nested and ('thinking' in sent)!=nested
    assert (sent['extra_body'] if nested else sent)['thinking']=={'type':'enabled'}
    assert sent['reasoning_effort']=='low'
    assert ('cache' in sent)==nested
    if nested:assert sent['cache']=={'no-cache':True,'no-store':True}


# 重试仅允许改变输出额度；thinking、缓存控制和其他请求字段必须保持一致。
def test_truncation_retry_preserves_nested_wire_parameters():
    model=make(request_profile='gateway-low');sent=[]
    responses=iter([body('length',8192,''),body()])
    def send(payload,timeout):sent.append(deepcopy(payload));return next(responses),{}
    model._send=send;model.complete([],[])
    assert [p.pop('max_tokens') for p in sent]==[8192,16384]
    assert sent[0]==sent[1] and sent[0]['extra_body']['thinking']['type']=='enabled'
