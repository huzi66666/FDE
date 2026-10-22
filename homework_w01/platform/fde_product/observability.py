"""OpenTelemetry local evidence, with opt-in OTLP export for W9."""
# 默认将 OTel span 写入本地 JSONL；仅在显式提供端点时向外导出。
import json,threading
from pathlib import Path
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor,SpanExporter,SpanExportResult
from opentelemetry.sdk.resources import Resource

# 每个 span 独占一行，trace_id/parent_span_id 保留调用链关系，便于离线核对。
class FileExporter(SpanExporter):
    def __init__(self,path):self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True);self.lock=threading.Lock()
    # 实例锁保护追加写入；属性内容由埋点调用方决定，不在此处收集密钥。
    def export(self,spans):
        with self.lock,self.path.open('a') as f:
            for s in spans:
                value={'trace_id':format(s.context.trace_id,'032x'),'span_id':format(s.context.span_id,'016x'),'parent_span_id':format(s.parent.span_id,'016x') if s.parent else None,'name':s.name,'start_time':s.start_time,'end_time':s.end_time,'attributes':dict(s.attributes),'status':s.status.status_code.name}
                f.write(json.dumps(value,ensure_ascii=False)+'\n')
        return SpanExportResult.SUCCESS
    # 文件在每次 export 的上下文中关闭，无需维持长期文件句柄。
    def shutdown(self):pass

# 创建独立 Provider，始终启用本地导出，可选再添加 OTLP HTTP 导出器。
def setup(path,endpoint=None,headers=None):
    provider=TracerProvider(resource=Resource.create({'service.name':'fde-member-agent','service.version':'0.2.0-w1'}))
    # 同步处理器在 span 结束时导出；调用方关闭 Agent 时负责 shutdown。
    provider.add_span_processor(SimpleSpanProcessor(FileExporter(path)))
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=endpoint,headers=headers or {},timeout=5)))
    return provider,provider.get_tracer('fde-product')
