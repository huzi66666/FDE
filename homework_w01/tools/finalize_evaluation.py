#!/usr/bin/env python3
"""Merge an actual Promptfoo report; cannot turn failed evals into passed state."""
# 将已有 Promptfoo 报告合并到环境检查；本脚本不执行评估，也不生成虚假通过记录。
from pathlib import Path
import argparse,json
p=argparse.ArgumentParser();p.add_argument('--root',default=str(Path(__file__).resolve().parents[1]));a=p.parse_args();out=Path(a.root)/'artifacts/w01'
# 读取真实评估结果中的统计与逐项响应；缺失或损坏的报告会直接失败。
r=json.loads((out/'promptfoo-results.json').read_text());stats=r['results']['stats'];cases=r['results']['results']
# 当前规则要求至少三项成功、零失败/错误且均为 live；未验证完整七场景身份。
ok=stats['successes']>=3 and stats['failures']==0 and stats.get('errors',0)==0 and len(cases)>=3 and all(c['success'] and json.loads(c['response']['output']).get('model_mode')=='live' for c in cases)
# A report left over from another model must not count for the model in this run's baseline.
# 还要逐项核对模型名称，避免拿另一模型的旧评估报告更新本次基线。
model=json.loads((out/'model_baseline.json').read_text()).get('model_name')
same=ok and all(json.loads(c['response']['output']).get('model_name')==model for c in cases);code='PROMPTFOO_REPORT_VERIFIED' if same else ('PROMPTFOO_MODEL_MISMATCH' if ok else 'PROMPTFOO_FAILED');ok=same
# 仅更新 evaluation 检查项，保留其他环境结论，并以退出码表示合并结果。
env=json.loads((out/'environment_check.json').read_text());env['checks']['evaluation']={'status':'pass' if ok else 'fail','code':code};(out/'environment_check.json').write_text(json.dumps(env,ensure_ascii=False,indent=2)+'\n');print(env['checks']['evaluation']);raise SystemExit(0 if ok else 1)
