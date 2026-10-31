"""仅解析源码及两种实体的模板展开文本；不导入应用或运行生成器。"""
import ast
import json
from pathlib import Path
from types import SimpleNamespace

root = Path(__file__).resolve().parents[1] / "src" / "eic"
module = ast.parse((root / "code_generator.py").read_text(encoding="utf-8"))
ast.parse((root / "main.py").read_text(encoding="utf-8"))
registry = next(n.value for n in module.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "ENTITIES" for t in n.targets))
mapping = next(n.value for n in ast.walk(module) if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "replacements" for t in n.targets))
files = []


def value(node, entity):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "entity":
        return getattr(entity, node.attr)
    if isinstance(node, ast.IfExp):
        return value(node.body if value(node.test, entity) else node.orelse, entity)
    raise ValueError(f"未识别的静态模板表达式：{type(node).__name__}")


for call in registry.elts:
    name, slug, description = [ast.literal_eval(arg) for arg in call.args]
    entity = SimpleNamespace(name=name, slug=slug, description=description,
                             parent=next((ast.literal_eval(k.value) for k in call.keywords if k.arg == "parent"), False))
    replacements = {ast.literal_eval(k): value(v, entity) for k, v in zip(mapping.keys, mapping.values)}
    for filename in ("schemas.py.tpl", "routes.py.tpl", "api.js.tpl", "page.vue.tpl"):
        source = (root / "codegen_templates" / filename).read_text(encoding="utf-8")
        for token, replacement in replacements.items():
            source = source.replace(token, replacement)
        if "__PARENT_" in source or "__ENTITY__" in source or "__SLUG__" in source or "__LABEL__" in source:
            raise ValueError(f"未展开的占位符：{filename}")
        if filename.endswith(".py.tpl"):
            ast.parse(source, filename=f"{name}/{filename}")
        files.append({"name": f"{name}/{filename}", "source": source})
print(json.dumps({"mode": "static-source-review-only", "files": files}, ensure_ascii=False))
