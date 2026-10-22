#!/usr/bin/env python3
"""W1 product entry point. Credentials are read only from explicit environment."""
# 命令行入口：组织初始化、单次运行和本地服务，不在此实现业务计算。
import argparse,json,sys,os
from pathlib import Path
# 从源码包直接导入 platform/fde_product，无需先安装为系统级 Python 包。
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'platform'))
from fde_product.business import import_sample

# --home 隔离运行状态，--actions 决定主机角色，--port=0 请求随机空闲端口。
def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=['init','mock','run','serve','start']);p.add_argument('--home',default=str(ROOT/'.runtime/product'));p.add_argument('--port',type=int);p.add_argument('--text');p.add_argument('--session');p.add_argument('--actions',action='store_true');p.add_argument('--confirm-hash');p.add_argument('--reject',action='store_true')
    args=p.parse_args();home=Path(args.home);mock=None;mock_thread=None
    # init 仅导入样例，避免在不需要模型的步骤读取或校验模型配置。
    if args.command=='init':print(json.dumps(import_sample(ROOT/'sample_data',home/'sample.sqlite')));return
    # mock 独立启动标签系统，供另一个 serve 进程或互操作客户端调用。
    if args.command=='mock':
        from fde_product.tag_mock import make_server
        server=make_server(home/'tag-system.sqlite',args.port if args.port is not None else 8766,os.getenv('FDE_MOCK_TOKEN','training-demo-token'))
        print('Sample tag API: http://127.0.0.1:'+str(server.server_port),flush=True)
        try:server.serve_forever()
        except KeyboardInterrupt:pass
        finally:server.server_close()
        return
    # start 在同进程后台线程运行 Mock；分析库不存在时才自动导入样例。
    if args.command=='start':
        from fde_product.tag_mock import make_server
        import threading
        if not (home/'sample.sqlite').exists():import_sample(ROOT/'sample_data',home/'sample.sqlite')
        mock=make_server(home/'tag-system.sqlite',0,os.getenv('FDE_MOCK_TOKEN','training-demo-token'))
        mock_thread=threading.Thread(target=mock.serve_forever,daemon=True);mock_thread.start()
        print('Sample tag API: http://127.0.0.1:'+str(mock.server_port),flush=True)
    from fde_product.agent import Agent
    from fde_product.models import CompatibleModel,ModelError
    from fde_product.actions import ActionError
    try:
        # 适配器读取显式环境变量；角色由启动参数指定，不由用户对话赋予。
        model=CompatibleModel(timeout=90)
        agent=Agent(home,model,mock_url=('http://127.0.0.1:'+str(mock.server_port)) if mock else os.getenv('FDE_MOCK_URL','http://127.0.0.1:8766'),mock_token=os.getenv('FDE_MOCK_TOKEN','training-demo-token'),role='operator' if args.actions else 'analyst')
        if args.command in ('serve','start'):
            from fde_product.web import serve
            serve(agent,args.port if args.port is not None else 8765,args.actions)
        else:
            # CLI 确认须同时提供原会话和草案哈希；--reject 表示拒绝该草案。
            resume={'approved':not args.reject,'draft_hash':args.confirm_hash} if args.confirm_hash else None
            print(json.dumps(agent.run(args.text,args.session,args.actions,resume),ensure_ascii=False,indent=2))
    except ModelError as e:raise SystemExit(e.code)
    except ActionError as e:raise SystemExit(e.code)
    finally:
        # 包括启动失败与 Ctrl+C 路径，统一释放 Agent、Mock 服务和后台线程。
        if 'agent' in locals():agent.close()
        if mock:mock.shutdown();mock.server_close();mock_thread.join()
if __name__=='__main__':main()
