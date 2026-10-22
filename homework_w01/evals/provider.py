"""Promptfoo Python provider; evaluates the actual product with a chosen live model."""
# Promptfoo 的 Python 适配入口，评估的是实际产品流程而不是独立拼接的提示词。
import json,sys,tempfile,time
from pathlib import Path
# 直接加载源码包与共享多轮场景，避免要求把教学产品安装成系统级包。
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'platform'))
from fde_product.business import import_sample
from fde_product.agent import Agent
from fde_product.models import CompatibleModel
sys.path.insert(0,str(ROOT/'tools'))
from interaction_scenarios import run_scenario

# 每次调用使用临时样例库隔离状态；scenario 变量决定单轮还是多轮评估。
def call_api(prompt,options,context):
    home=Path(tempfile.mkdtemp(prefix='fde-eval-'));import_sample(ROOT/'sample_data',home/'sample.sqlite')
    start=time.monotonic();scenario=context.get('vars',{}).get('scenario')
    # 正式评估始终使用 CompatibleModel；多轮逻辑与 live verifier 共用。
    if scenario:result=run_scenario(home,CompatibleModel(timeout=90),scenario)
    else:
        a=Agent(home,CompatibleModel(timeout=90))
        try:result=a.run(prompt)
        finally:a.close()
    # output 供 JavaScript 断言解析；metadata 提供耗时、模型名及 trace 路径。
    # 此接口当前对缺失 token 用量补零，不能将该零值视为确定的零消耗。
    return {'output':json.dumps(result,ensure_ascii=False),'tokenUsage':{'total':result['usage'].get('total_tokens',0),'prompt':result['usage'].get('prompt_tokens',0),'completion':result['usage'].get('completion_tokens',0)},'metadata':{'duration_seconds':time.monotonic()-start,'trace_file':str(home/'trace.jsonl'),'model_name':result['model_name']}}
