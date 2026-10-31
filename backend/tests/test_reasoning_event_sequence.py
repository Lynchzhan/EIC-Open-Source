"""零外呼回归：执行生产 AST 片段和真实 replay 函数，隔离数据库与应用启动。

FakeSession 仅实现本用例经过的查询语义；不验证 SQLAlchemy、ASGI 或 provider。
订阅者先消费 reasoningConfig，之后才提交首个 aimsg，固定实时丢片窗口。
"""
import ast
import asyncio
import copy
import json
import time
from pathlib import Path
from types import SimpleNamespace
import unittest


SOURCE = Path(__file__).parents[1] / "src/eic/ai/chat_execution.py"
EVENT_SOURCE = SOURCE.with_name("chat_events.py")
REPLAY_SOURCE = SOURCE.with_name("chat_producer.py")


class BoundDependencies(ast.NodeTransformer):
    """只替换装配引用，执行的仍是生产函数原始语句。"""
    def visit_Attribute(self, node):
        node = self.generic_visit(node)
        if isinstance(node.value, ast.Name) and node.value.id == 'dependencies':
            return ast.copy_location(ast.Name(id=node.attr, ctx=node.ctx), node)
        if isinstance(node.value, ast.Name) and node.value.id == 'runtime' and node.attr == 'sessions':
            return ast.copy_location(ast.Name(id='SessionLocal', ctx=node.ctx), node)
        return node


class Column:
    def __init__(self, name):
        self.name = name

    def __eq__(self, value):
        return lambda row: getattr(row, self.name) == value

    def __gt__(self, value):
        return lambda row: getattr(row, self.name) > value


class Query:
    def __init__(self, target):
        self.target, self.conditions = target, []

    def where(self, *conditions):
        self.conditions.extend(conditions)
        return self

    def order_by(self, *columns):
        return self

    def limit(self, count):
        return self


def production_functions():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    graph = next(node for node in ast.walk(tree)
                 if isinstance(node, ast.AsyncFunctionDef) and node.name == "graph_model")
    start = next(i for i, node in enumerate(graph.body)
                 if isinstance(node, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == "actual_reasoning"
                         for t in node.targets))
    end = next(i for i in range(start + 1, len(graph.body))
               if isinstance(graph.body[i], ast.If))
    reasoning = graph.body[start:end]
    # 包含旧版 emit 前的 sequence += 1，确保旧实现确实被回归击中。
    content_branches = [node for node in ast.walk(graph)
                        if isinstance(node, ast.If)
                        and isinstance(node.test, ast.Name) and node.test.id == "content"
                        and any(isinstance(call, ast.Call)
                                and isinstance(call.func, ast.Attribute)
                                and call.func.attr == "audited_sse_message"
                                for call in ast.walk(node))]
    assert len(content_branches) == 1, "正文持久化分支必须完整且唯一"
    content_branch = content_branches[0]
    snippets = {}
    for name, body in (("emit_reasoning", reasoning), ("emit_content", content_branch.body)):
        function = ast.AsyncFunctionDef(
            name=name,
            args=ast.arguments(posonlyargs=[], args=[ast.arg(arg="sequence")],
                               kwonlyargs=[], kw_defaults=[], defaults=[]),
            body=copy.deepcopy(body) + [ast.Return(value=ast.Name(id="sequence", ctx=ast.Load()))],
            decorator_list=[],
        )
        snippets[name] = function
    functions = []
    for path, names in ((EVENT_SOURCE, {"sse_payload", "sse_message"}),
                        (REPLAY_SOURCE, {"replay_chat_events"})):
        nodes = [copy.deepcopy(node) for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
        assert len(nodes) == len(names), "生产函数提取必须完整且唯一"
        functions.extend(nodes)
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                              *functions, *snippets.values()], type_ignores=[])
    return compile(ast.fix_missing_locations(BoundDependencies().visit(module)), str(SOURCE), "exec")


def decode(chunk):
    lines = chunk.splitlines()
    return (next(line[7:] for line in lines if line.startswith("event: ")),
            json.loads(next(line[6:] for line in lines if line.startswith("data: "))))


class ReasoningEventSequenceTests(unittest.TestCase):
    def test_live_replay_retains_first_answer_after_reasoning(self):
        async def run():
            rows, emitted = [], []
            stored = SimpleNamespace(status="generating")
            event_model = SimpleNamespace(**{name: Column(name) for name in
                                           ("id", "tenant_id", "request_id", "event_type", "sequence")})

            class FakeSession:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *args):
                    pass

                async def scalars(self, query):
                    return sorted((row for row in rows if all(c(row) for c in query.conditions)),
                                  key=lambda row: row.sequence)

                async def scalar(self, query):
                    assert query.target is event_model.id, "不应依赖最终回答补偿路径"
                    matches = await self.scalars(query)
                    return matches[0].id if matches else None

            async def owned(*args):
                return None, stored

            async def recover(*args, **kwargs):
                pass

            async def emit(chunk):
                emitted.append(chunk)

            async def audited(request_id, principal, sequence, event, text, extra=None):
                payload = namespace["sse_payload"](text, extra)
                rows.append(SimpleNamespace(id=len(rows) + 1, tenant_id=1, request_id=request_id,
                                            sequence=sequence, event_type=event, payload=json.dumps(payload)))
                return namespace["sse_message"](request_id, sequence, event, text, extra)

            expected = "EIC_0123456789abcdef0123456789abcdef"
            from fastapi import HTTPException
            from eic.core.runtime_refs import RuntimeReferenceUnavailable
            namespace = dict(json=json, asyncio=asyncio, time=time,
                             HTTPException=HTTPException, RuntimeReferenceUnavailable=RuntimeReferenceUnavailable,
                             require_runtime_chat_permission=recover, ChatEvent=event_model, select=Query,
                             SessionLocal=FakeSession, require_owned_chat_request=owned,
                             generation=SimpleNamespace(recover_expired=recover),
                             audited_sse_message=audited, services=SimpleNamespace(emit=emit),
                             request_id="fixture", principal=SimpleNamespace(tenant_id=1),
                             parameter_metadata={"reasoning": {}}, metadata={},
                             parts=[], answer_parts=[], content=expected,
                             stream_metrics={"eventPersistenceMs": 0.0})
            exec(production_functions(), namespace)
            sequence = await namespace["emit_reasoning"](4)
            replay = namespace["replay_chat_events"]("fixture", namespace["principal"])
            try:
                first = await asyncio.wait_for(anext(replay), 1)
                self.assertEqual(decode(first)[0], "reasoningConfig")
                # 在订阅者已推进游标后才提交首片，避免 completed 回放掩盖实时竞态。
                sequence = await namespace["emit_content"](sequence)
                self.assertGreater(rows[1].sequence, rows[0].sequence)
                self.assertEqual([r.sequence for r in rows], [4, 5])
                self.assertEqual(sequence, 6, "正文落盘后必须推进游标，避免后续 done 复用序号")
                await audited("fixture", namespace["principal"], sequence, "done", "",
                              {"status": "completed", "partial": False})
                stored.status = "completed"

                async def drain():
                    return [decode(chunk) async for chunk in replay
                            if not chunk.startswith(":")]

                remaining = await asyncio.wait_for(drain(), 1)
                self.assertEqual([event for event, _ in remaining], ["aimsg", "done"])
                self.assertEqual("".join(body["text"] for event, body in remaining
                                         if event == "aimsg"), expected)
                self.assertEqual("".join(namespace["answer_parts"]), expected)
                self.assertEqual(remaining[-1][1]["status"], "completed")
            finally:
                await replay.aclose()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
