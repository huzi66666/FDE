#!/usr/bin/env python3
# 为独立 Node 探针准备临时标签服务；探针会调用真实模型并写入互操作报告。
import subprocess,os,tempfile,threading,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'platform'))
from fde_product.tag_mock import make_server
# 使用临时数据库和随机端口，不复用正在演示的标签状态。
s=make_server(Path(tempfile.mkdtemp(prefix='fde-custom-api-'))/'labels.sqlite',0);t=threading.Thread(target=s.serve_forever,daemon=True);t.start()
try:
 # 子进程继承模型环境变量，仅覆盖本次 Mock 地址；密钥不写入命令参数。
 env=os.environ.copy();env['FDE_MOCK_URL']='http://127.0.0.1:'+str(s.server_port);(ROOT/'artifacts/w01').mkdir(parents=True,exist_ok=True)
 # 等待 Node 完成并保留其退出码，探针失败不能被包装器掩盖。
 code=subprocess.run(['node',str(ROOT/'tools/probe_custom_contract.mjs')],env=env).returncode
# 无论 Node 执行成功还是抛错，都停止临时服务并等待后台线程退出。
finally:s.shutdown();s.server_close();t.join()
raise SystemExit(code)
