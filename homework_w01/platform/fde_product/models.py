"""Chat Completions adapter with one bounded, observable truncation retry.

Only the configured endpoint is used. Credentials and private reasoning are never
included in public call records. A truncated response never reaches tool execution.
"""
# 模型协议适配层：直接发送 Chat Completions JSON，不使用 MCP 或模型 SDK。
from copy import deepcopy
import json
import os
import socket
import time
from urllib import error, request
from urllib.parse import urlsplit

# 策略版本与网关请求格式分别记录，便于对比不同运行的协议行为。
POLICY_VERSION = "gateway-stages-20260922"
WIRE_FORMAT = "gateway-extra-body-v1"


# 直连端点与课程网关的 thinking 字段层级不同，只按显式 profile 选择。
def thinking_parameters(profile, state, wire_format=WIRE_FORMAT):
    """Gateway JSON nesting is endpoint-specific, independent of thinking state."""
    if profile not in ("deepseek-json", "deepseek-low", "gateway-json", "gateway-low", "gateway-none"):
        return {}
    thinking = {"type": state}
    params = {"reasoning_effort": "low"}
    if profile.startswith("gateway-") and wire_format == WIRE_FORMAT:
        params["extra_body"] = {"thinking": thinking}
    else:
        params["thinking"] = thinking
    return params


# 向上层暴露稳定错误编号，不携带请求密钥或完整供应商错误响应。
class ModelError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)

# 禁止自动跳转，避免认证头和业务请求被转发到非配置端点。
class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

# 可公开消息仅保留内容和工具调用，排除供应商返回的私有推理字段。
def public_message(message):
    return {k:deepcopy(message[k]) for k in ("role", "content", "tool_calls") if k in message}

# 实际续接请求保留 reasoning_content；公开请求副本将其移除。
def public_payload(payload):
    result=deepcopy(payload)
    result["messages"]=[{k:v for k,v in m.items() if k!="reasoning_content"} for m in result.get("messages",[])]
    return result

# trace 使用调用摘要，进一步去掉完整请求和响应正文，保留尝试与用量信息。
def call_summary(model):
    call=getattr(model,"last_call",{}) or {}
    return {**call,"attempts":[{k:v for k,v in a.items() if k not in ("request","response")} for a in call.get("attempts",[])]}

# 只有所有尝试都报告了有效非负整数时才合计该字段，缺失用量不补零。
def aggregate_usage(attempts):
    result={}
    for field in ("prompt_tokens","completion_tokens","total_tokens"):
        values=[a.get("usage",{}).get(field) for a in attempts]
        if values and all(type(v) is int and v>=0 for v in values):result[field]=sum(values)
    values=[a.get("usage",{}).get("completion_tokens_details",{}).get("reasoning_tokens") for a in attempts]
    if values and all(type(v) is int and v>=0 for v in values):result["completion_tokens_details"]={"reasoning_tokens":sum(values)}
    return result

# 对 Agent 暴露 complete(messages, tools)，端点和密钥仅来自参数或环境变量。
class CompatibleModel:
    mode="live"
    def __init__(self,url=None,key=None,model=None,timeout=90,*,request_profile=None,
                 max_tokens=None,retry_max_tokens=None,retry_timeout=None,token_limit_field=None,wire_format=WIRE_FORMAT,gateway_cache_bypass=True):
        # URL 原样使用，不补全 /chat/completions，也不自动读取 .env 文件。
        self.url=url or os.environ.get("FDE_MODEL_URL","")
        self.key=key or os.environ.get("FDE_MODEL_KEY","")
        self.model=model or os.environ.get("FDE_MODEL_NAME","")
        self.request_profile=request_profile or os.environ.get("FDE_MODEL_PROFILE","compatible")
        if wire_format not in (WIRE_FORMAT, "legacy-top-level"):
            raise ModelError("MODEL_WIRE_FORMAT_INVALID")
        self.wire_format = wire_format
        if type(gateway_cache_bypass) is not bool:raise ModelError("MODEL_CACHE_POLICY_INVALID")
        self.gateway_cache_bypass = gateway_cache_bypass
        if self.request_profile not in ("compatible","json-object","deepseek-json","gateway-json","deepseek-low","gateway-low"):
            raise ModelError("MODEL_PROFILE_INVALID")
        # 将环境变量转换为预算参数；重试额度必须大于首轮额度。
        try:
            self.max_tokens=int(max_tokens if max_tokens is not None else os.environ.get("FDE_MODEL_MAX_TOKENS",8192))
            self.retry_max_tokens=int(retry_max_tokens if retry_max_tokens is not None else os.environ.get("FDE_MODEL_RETRY_MAX_TOKENS",self.max_tokens*2))
            self.timeout=float(os.environ.get("FDE_MODEL_TIMEOUT",timeout))
            self.retry_timeout=float(retry_timeout if retry_timeout is not None else os.environ.get("FDE_MODEL_RETRY_TIMEOUT",max(180,self.timeout)))
        except (ValueError,TypeError):raise ModelError("MODEL_POLICY_INVALID") from None
        self.token_limit_field=token_limit_field or os.environ.get("FDE_MODEL_TOKEN_LIMIT_FIELD","max_tokens")
        if not (0<self.max_tokens<self.retry_max_tokens<=1048576 and 0<self.timeout<=self.retry_timeout<=3600
                and self.token_limit_field in ("max_tokens","max_completion_tokens")):
            raise ModelError("MODEL_POLICY_INVALID")
        self.deadline=None
        self.last_call={}
        if not all((self.url,self.key,self.model)):raise ModelError("MODEL_NOT_CONFIGURED")
        # 非本机端点要求 HTTPS，禁止凭据嵌入 URL、查询参数和片段。
        parsed=urlsplit(self.url)
        local=parsed.hostname in ("localhost","127.0.0.1","::1")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:raise ModelError("MODEL_URL_INVALID")
        if parsed.scheme!="https" and not (parsed.scheme=="http" and local):raise ModelError("MODEL_URL_INVALID")
        if not parsed.hostname:raise ModelError("MODEL_URL_INVALID")

    # 返回不含密钥的策略配置，供运行证据记录和对比。
    def policy_config(self):
        return {"gateway_cache_bypass":self.gateway_cache_bypass,"wire_format":self.wire_format,"request_profile":self.request_profile,"max_tokens":self.max_tokens,
                "retry_max_tokens":self.retry_max_tokens,"timeout":self.timeout,
                "retry_timeout":self.retry_timeout,"token_limit_field":self.token_limit_field}

    # tools 使用 Function Calling Schema；auto 允许模型自行选择是否调用工具。
    def _payload(self,messages,tools):
        payload={"model":self.model,"messages":messages,"stream":False,self.token_limit_field:self.max_tokens}
        if tools:payload.update(tools=tools,tool_choice="auto")
        if self.request_profile in ("json-object","deepseek-json","gateway-json") and not tools:
            payload["response_format"]={"type":"json_object"}
        payload.update(thinking_parameters(self.request_profile, "enabled", self.wire_format))
        if self.request_profile.startswith("gateway-") and self.gateway_cache_bypass:
            # Observed same response ID for opposite switches: bypass response cache.
            payload["cache"] = {"no-cache":True,"no-store":True}
        return payload

    # 执行一次 HTTP 请求，限制响应体大小与读取时间，只保留允许的追踪头。
    def _send(self,payload,timeout):
        req=request.Request(self.url,data=json.dumps(payload).encode(),headers={
            "Authorization":"Bearer "+self.key,"Content-Type":"application/json"})
        deadline=time.monotonic()+timeout
        with request.build_opener(NoRedirect).open(req,timeout=timeout) as response:
            headers={k.lower():v for k,v in getattr(response,"headers",{}).items()}
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError()
            sock=getattr(getattr(getattr(response,"fp",None),"raw",None),"_sock",None)
            if sock is not None:sock.settimeout(remaining)
            raw=response.read(2_000_001)
            if time.monotonic()>deadline:raise TimeoutError()
        if len(raw)>2_000_000:raise ModelError("MODEL_RESPONSE_TOO_LARGE")
        return json.loads(raw),{k:v for k,v in headers.items() if k in (
            "llm_provider-x-request-id","x-request-id","x-litellm-call-id","request-id","x-bce-request-id")}

    # 一个逻辑调用最多两次 HTTP 尝试，第二次仅用于已确认的输出截断。
    def complete(self,messages,tools):
        self.last_call={"policy_version":POLICY_VERSION,"attempts":[],"usage":{}}
        original=self._payload(messages,tools)
        field=next((f for f in ("max_tokens","max_completion_tokens") if f in original),None)
        for number in range(2):
            # 每次从相同输入复制请求，只扩大输出预算，避免把截断结果加入上下文。
            payload=deepcopy(original)
            if number:payload[field]=self.retry_max_tokens
            allowance=self.timeout if number==0 else self.retry_timeout
            # 单次等待还要受 Agent 整轮剩余预算约束，重试不会重置整轮期限。
            if self.deadline is not None:allowance=min(allowance,self.deadline-time.monotonic())
            if allowance<=0:raise ModelError("MODEL_TURN_TIMEOUT")
            attempt={"attempt":number+1,"request":public_payload(payload),"timeout_seconds":round(allowance,3),
                     "max_tokens":payload.get(field),"usage":{},"trace_ids":{}}
            self.last_call["attempts"].append(attempt)
            started=time.monotonic()
            try:
                body,trace=self._send(payload,allowance)
                attempt["trace_ids"]=trace
                if isinstance(body.get("id"),str):attempt["response_id"]=body["id"]
                choice=body["choices"][0];message=choice["message"]
                if not isinstance(message,dict) or message.get("role")!="assistant":raise ValueError("invalid role")
                usage=body.get("usage") or {}
                if not isinstance(usage,dict):raise ValueError("invalid usage")
                attempt.update(http_status=200,finish_reason=choice.get("finish_reason"),usage=usage,response=public_message(message))
                finish=choice.get("finish_reason")
                # length 本身不足以判定输出额度耗尽，还需供应商返回的 token 用量证明。
                if finish=="length":
                    generated=usage.get("completion_tokens")
                    if field and type(generated) is int and generated>=payload[field]:
                        attempt["error"]="MODEL_OUTPUT_TRUNCATED"
                        if number==0 and self.retry_max_tokens>payload[field]:continue
                        raise ModelError("MODEL_OUTPUT_TRUNCATED")
                    raise ModelError("MODEL_LENGTH_LIMIT_UNCONFIRMED")
                if finish not in (None,"stop","tool_calls"):
                    raise ModelError("MODEL_FINISH_"+str(finish).upper())
                # 只有完整响应可交给工具执行；私有推理仅为下一次协议续接保留。
                clean={k:deepcopy(message[k]) for k in ("role","content","tool_calls","reasoning_content") if k in message}
                return clean,aggregate_usage(self.last_call["attempts"])
            # 网络超时、权限/HTTP 错误与格式错误直接终止，不伪装成截断重试。
            except (TimeoutError,socket.timeout):
                attempt["error"]="MODEL_TIMEOUT";raise ModelError("MODEL_TIMEOUT") from None
            except error.HTTPError as exc:
                code="MODEL_HTTP_"+str(exc.code)
                attempt["http_status"]=exc.code
                attempt["trace_ids"]={k.lower():v for k,v in (exc.headers or {}).items() if k.lower() in ("x-request-id","x-bce-request-id","llm_provider-x-request-id","x-litellm-call-id")}
                exc.close();attempt["error"]=code;raise ModelError(code) from None
            except error.URLError as exc:
                code="MODEL_TIMEOUT" if isinstance(exc.reason,(TimeoutError,socket.timeout)) else "MODEL_NETWORK_ERROR"
                attempt["error"]=code;raise ModelError(code) from None
            except (ValueError,KeyError,IndexError,TypeError,AttributeError):
                attempt["error"]="MODEL_INVALID_RESPONSE";raise ModelError("MODEL_INVALID_RESPONSE") from None
            except ModelError as exc:
                attempt["error"]=exc.code;raise
            finally:
                # 成功和失败尝试都保留耗时，并由适配器汇总已知用量。
                attempt["seconds"]=round(time.monotonic()-started,3)
                self.last_call["usage"]=aggregate_usage(self.last_call["attempts"])

# 离线测试替身：依次返回预置响应或抛出指定错误，不代表真实模型能力。
class ScriptedModel:
    """Deterministic teaching fixture, never evidence of live Agent behavior."""
    mode="scripted"
    def __init__(self,turns):self.turns=iter(deepcopy(turns))
    def complete(self,messages,tools):
        try:turn=next(self.turns)
        except StopIteration:raise ModelError("SCRIPT_EXHAUSTED") from None
        if isinstance(turn,str):raise ModelError(turn)
        return turn,{}
