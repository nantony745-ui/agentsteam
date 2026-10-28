"""ACP 表单答复校验；Codex 原生问题保留字段 ID、选项和补充说明。"""


def validate_answer(schema: dict, content: dict) -> None:
    properties = schema.get("properties", {})
    if content.keys() - properties.keys():
        raise ValueError("回答包含未知字段")
    for key in schema.get("required", []):
        if key not in content or content[key] == "":
            raise ValueError(f"请回答：{properties.get(key, {}).get('title', key)}")
    for key, value in content.items():
        spec = properties[key]
        kind = spec.get("type")
        valid = {"string": isinstance(value, str), "boolean": type(value) is bool,
                 "integer": type(value) is int, "number": type(value) in {int, float},
                 "array": isinstance(value, list) and all(isinstance(item, str) for item in value)}
        if not valid.get(kind, False):
            raise ValueError(f"字段类型不正确：{key}")
        choices = spec.get("enum") or [item["const"] for item in spec.get("oneOf", []) if "const" in item]
        if choices and value not in choices:
            raise ValueError(f"请选择有效选项：{key}")
        if kind == "string":
            if len(value) < spec.get("minLength", 0) or len(value) > spec.get("maxLength", float("inf")):
                raise ValueError(f"回答长度不正确：{key}")
        if kind in {"integer", "number"}:
            if value < spec.get("minimum", -float("inf")) or value > spec.get("maximum", float("inf")):
                raise ValueError(f"数值超出范围：{key}")
        if kind == "array":
            allowed = spec.get("items", {}).get("enum")
            if allowed and any(item not in allowed for item in value):
                raise ValueError(f"请选择有效选项：{key}")
