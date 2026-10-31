"""标准工具外层预算必须产生明确的不确定结果，且不重试。"""
import asyncio

import pytest

from eic.ai.standard_tools import StandardToolDefinition, ControlledToolSet
from eic.ai.tool_center import ToolExecutionError


def test_outer_timeout_waits_cleanup_and_never_replays():
    calls=[]
    async def dispatch(call):
        calls.append('entered')
        try:
            await asyncio.Event().wait()
        finally:
            calls.append('closed')
    registry=ControlledToolSet([StandardToolDefinition('fixture','fixture',timeout_ms=30)],dispatch=dispatch)
    async def run():
        with pytest.raises(ToolExecutionError) as error:
            await registry.ainvoke('fixture',{},call_id='one',remaining_ms=1000)
        assert error.value.code=='tool_timeout'
        assert error.value.effect_status=='unknown' and error.value.retryable is False
        assert calls==['entered','closed']
    asyncio.run(run())
