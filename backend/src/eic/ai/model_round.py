"""每次执行独有的协议续轮状态，不进入明文图状态或日志。"""
from dataclasses import dataclass, field


@dataclass
class ModelRoundState:
    protocol_continuation: dict = field(default_factory=dict, repr=False)

    @classmethod
    def restore(cls, payload=None):
        from eic.ai.protocol_bridge import restore_protocol_continuation
        return cls(restore_protocol_continuation(payload))

    def export(self):
        from eic.ai.protocol_bridge import export_protocol_continuation
        return export_protocol_continuation(self.protocol_continuation)
