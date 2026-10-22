#!/usr/bin/env python3
"""Portable artifact checker; execution evidence is separately rerun by reviewers."""
# 产物结构与证据完整性检查器；不会重跑模型，也不能证明报告中的业务结论。
import argparse,json,hashlib,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'platform'))
from jsonschema import Draft202012Validator
# 当前门禁强制要求的七个基础场景；不等同于 verifier 实际执行的全部场景。
REQUIRED_CASES={'analysis','clarification','missing_coverage','readonly','http_action','idempotency','undo'}

# root 是交付物目录，Schema 和能力矩阵始终来自当前工具所在发布包。
def check(root,dry=False):
    root=Path(root).resolve();errors=[];values={}
    # dry-run 只验证四份 Schema 自身是否合法，不要求学员已生成任何产物。
    if dry:
        for p in (ROOT/'contracts/w01').glob('*.schema.json'):Draft202012Validator.check_schema(json.loads(p.read_text()))
        return {'status':'dry_run','note':'Only schemas were checked; learner execution not verified.','errors':[]}
    # 先做 Schema 校验，结构有误时提前返回，避免后续读取不存在的字段。
    for name in ['environment_check','product_fit_gap','model_baseline','runtime_evidence']:
        p=root/'artifacts/w01'/(name+'.json')
        try:
            value=json.loads(p.read_text());schema=json.loads((ROOT/'contracts/w01'/(name+'.schema.json')).read_text())
            errors += [name+': '+e.message for e in Draft202012Validator(schema).iter_errors(value)];values[name]=value
        except (OSError,ValueError):errors.append(name+': MISSING_OR_INVALID')
    if errors:return {'status':'fail','errors':errors}
    # 核对跨产物的一致性、环境状态和必需行为，不能仅凭单个文件通过判断完成。
    versions={v['product_version'] for v in values.values()}
    if len(versions)!=1:errors.append('PRODUCT_VERSION_MISMATCH')
    env=values['environment_check']
    if any(c['status']!='pass' for c in env['checks'].values()):errors.append('ENVIRONMENT_NOT_READY')
    ev=values['runtime_evidence'];cases={c['case_id']:c for c in ev['cases']}
    if len(cases)!=len(ev['cases']) or not REQUIRED_CASES<=cases.keys() or not all(c['passed'] for c in cases.values()):errors.append('REQUIRED_BEHAVIOR_FAILED')
    # fit-gap 只能引用已发布能力编号；标为 verified 的项目必须附带证据。
    known={x['id'] for x in json.loads((ROOT/'product/capabilities.json').read_text())['capabilities']}
    for item in values['product_fit_gap']['items']:
        if not set(item['capability_ids'])<=known:errors.append('UNKNOWN_CAPABILITY_ID')
        if item['evidence_status']=='verified' and not item['evidence']:errors.append('VERIFIED_WITHOUT_EVIDENCE')
    if not (root/'docs/w01/project_assessment.md').is_file():errors.append('PROJECT_ASSESSMENT_MISSING')
    # 解析路径后限制在提交根目录内；哈希用于检测证据文件变化，不证明内容真实。
    for rel,expected in ev['artifact_hashes'].items():
        p=(root/rel).resolve()
        if not p.is_relative_to(root) or not p.is_file():errors.append('INVALID_EVIDENCE_PATH');continue
        if hashlib.sha256(p.read_bytes()).hexdigest()!=expected:errors.append('EVIDENCE_HASH_MISMATCH: '+rel)
    # 每个场景和模型运行都必须引用已纳入哈希清单的证据。
    for c in cases.values():
        if c['evidence'] not in ev['artifact_hashes']:errors.append('CASE_EVIDENCE_NOT_HASHED: '+c['case_id'])
    for r in values['model_baseline']['runs']:
        if r['run_id'] not in ev['artifact_hashes']:errors.append('BASELINE_RUN_NOT_HASHED')
        if r['actual_status']!=r['expected_status'] or not r['passed']:errors.append('MODEL_BASELINE_FAILED: '+r['case_id'])
    if not any(x['case_id']=='analysis' for x in values['model_baseline']['runs']):errors.append('NO_LIVE_ANALYSIS')
    return {'status':'fail' if errors else 'pass','errors':errors,'limits':['Artifact checks do not prove authorship or replace live behavior replay and rubric review.']}
# 用非零退出码向自动化流程报告失败，同时打印所有已收集到的错误。
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',default=str(ROOT));p.add_argument('--dry-run',action='store_true');a=p.parse_args();r=check(a.root,a.dry_run);print(json.dumps(r,ensure_ascii=False,indent=2));raise SystemExit(1 if r['status']=='fail' else 0)
