"""miniyaml — 本仓库所需 YAML 子集的解析与序列化（Python 3.11 标准库 only）。

为什么自研：离线约束（无 PyYAML 可装）。支持范围刻意收窄到注册表所需：
- 顶层为映射；块风格映射/序列（空格缩进；禁止 Tab）
- 序列项可与父键同缩进（YAML 惯例）；序列项内联首键（"- key: value"）
- 标量：双引号/单引号字符串、裸字符串、true/false、null/~
- 行内列表（仅标量成员）与空集合 [] / {}
- 注释：整行 '#'，行尾 ' #'（引号内不计）
错误统一抛 MiniYAMLError（含行号）。所有标量除 true/false/null 外均为字符串。
"""
from __future__ import annotations

import re


class MiniYAMLError(Exception):
    """YAML（子集）解析错误，带行号。"""

    def __init__(self, message, line=None):
        self.line = line
        if line is not None:
            message = "line %d: %s" % (line, message)
        super().__init__(message)


_KEY_RE = re.compile(r'^(?:"([^"]*)"|\'([^\']*)\'|([^\s:#][^:]*?)):(?:[ ]+(.*))?$')
_TAB_INDENT_RE = re.compile(r"^[ ]*\t")
_SAFE_UNQUOTED = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./+@-]*$")


# ---------------------------------------------------------------- 读取

def _strip_comment(raw):
    out = []
    quote = None
    i = 0
    while i < len(raw):
        ch = raw[i]
        if quote:
            out.append(ch)
            if quote == '"' and ch == "\\" and i + 1 < len(raw):
                out.append(raw[i + 1])
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#" and (not out or out[-1] in " \t"):
            break
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _preprocess(text):
    lines = []
    for no, raw in enumerate(text.splitlines(), 1):
        raw = raw.rstrip("\r")
        body = _strip_comment(raw)
        if not body.strip():
            continue
        if _TAB_INDENT_RE.match(body):
            raise MiniYAMLError("缩进不允许使用 Tab（仅空格）", no)
        stripped = body.lstrip(" ")
        indent = len(body) - len(stripped)
        lines.append((indent, stripped, no))
    return lines


def _unescape(s):
    out = []
    i = 0
    table = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "0": "\0"}
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            out.append(table.get(s[i + 1], s[i + 1]))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _split_inline(s):
    parts = []
    cur = []
    quote = None
    for ch in s:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            cur.append(ch)
        elif ch == ",":
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    tail = "".join(cur)
    if tail.strip() or parts:
        parts.append(tail)
    return [p.strip() for p in parts]


def _parse_scalar(token, line):
    token = token.strip()
    if token == "":
        return None
    if token.startswith("["):
        if not token.endswith("]"):
            raise MiniYAMLError("行内列表缺少闭合 ']'", line)
        inner = token[1:-1].strip()
        if inner == "":
            return []
        return [_parse_scalar(p, line) for p in _split_inline(inner)]
    if token == "{}":
        return {}
    if token[0] == '"':
        if len(token) < 2 or not token.endswith('"'):
            raise MiniYAMLError("双引号字符串未闭合", line)
        return _unescape(token[1:-1])
    if token[0] == "'":
        if len(token) < 2 or not token.endswith("'"):
            raise MiniYAMLError("单引号字符串未闭合", line)
        return token[1:-1].replace("''", "'")
    if token in ("null", "~"):
        return None
    if token == "true":
        return True
    if token == "false":
        return False
    return token


def _parse_block(lines, i, min_indent):
    if i >= len(lines) or lines[i][0] < min_indent:
        return None, i
    indent, content, _no = lines[i]
    if content == "-" or content.startswith("- "):
        return _parse_sequence(lines, i, indent)
    return _parse_mapping(lines, i, indent)


def _parse_sequence(lines, i, indent):
    items = []
    while i < len(lines):
        ind, content, no = lines[i]
        if ind != indent or not (content == "-" or content.startswith("- ")):
            break
        i += 1
        rest = content[1:].lstrip(" ")
        if rest == "":
            if i < len(lines) and lines[i][0] > indent:
                value, i = _parse_block(lines, i, indent + 1)
            else:
                value = None
            items.append(value)
            continue
        if _KEY_RE.match(rest):
            map_indent = indent + (len(content) - len(rest))
            value, i = _parse_mapping(lines, i, map_indent, first_content=rest, first_line=no)
            items.append(value)
        else:
            items.append(_parse_scalar(rest, no))
    return items, i


def _parse_mapping(lines, i, indent, first_content=None, first_line=None):
    result = {}
    content, no = first_content, first_line
    while True:
        if content is None:
            if i >= len(lines):
                break
            ind, content, no = lines[i]
            if ind != indent:
                break
            if content == "-" or content.startswith("- "):
                break
            i += 1
        m = _KEY_RE.match(content)
        if not m:
            raise MiniYAMLError("无法解析的映射行: %r" % content, no)
        key = (m.group(1) or m.group(2) or m.group(3)).strip()
        value = m.group(4)
        if value is None or value.strip() == "":
            if i < len(lines) and lines[i][0] > indent:
                result[key], i = _parse_block(lines, i, indent + 1)
            elif i < len(lines) and lines[i][0] == indent and (
                    lines[i][1] == "-" or lines[i][1].startswith("- ")):
                result[key], i = _parse_sequence(lines, i, indent)
            else:
                result[key] = None
        else:
            result[key] = _parse_scalar(value, no)
        content = None
    return result, i


def load_yaml(text):
    """解析 YAML 子集；空文档返回 None；畸形抛 MiniYAMLError。"""
    lines = _preprocess(text)
    if not lines:
        return None
    value, i = _parse_block(lines, 0, 0)
    if i != len(lines):
        raise MiniYAMLError("存在无法归位的行（缩进/结构异常）: %r" % (lines[i][1],), lines[i][2])
    return value


# ---------------------------------------------------------------- 写出

def _dump_scalar(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    s = str(value)
    if s == "" or s != s.strip() or s in ("true", "false", "null", "~") or not _SAFE_UNQUOTED.match(s):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t") + '"'
    return s


def _dump_node(node, indent):
    pad = " " * indent
    if isinstance(node, dict):
        if not node:
            return [pad + "{}"]
        out = []
        for k, v in node.items():
            ktxt = _dump_scalar(str(k))
            if (isinstance(v, dict) or isinstance(v, list)) and v:
                out.append("%s%s:" % (pad, ktxt))
                out.extend(_dump_node(v, indent + 2))
            elif isinstance(v, (dict, list)):
                out.append("%s%s: %s" % (pad, ktxt, "[]" if isinstance(v, list) else "{}"))
            elif v is None:
                out.append("%s%s: null" % (pad, ktxt))
            else:
                out.append("%s%s: %s" % (pad, ktxt, _dump_scalar(v)))
        return out
    if isinstance(node, list):
        if not node:
            return [pad + "[]"]
        out = []
        for item in node:
            if isinstance(item, dict) and item:
                sub = _dump_node(item, indent + 2)
                out.append(pad + "- " + sub[0][indent + 2:])
                out.extend(sub[1:])
            elif isinstance(item, (dict, list)):
                out.append(pad + "- " + ("[]" if isinstance(item, list) else "{}"))
            else:
                out.append("%s- %s" % (pad, _dump_scalar(item)))
        return out
    return [pad + _dump_scalar(node)]


def dump_yaml(obj):
    """序列化为可被 load_yaml 读回的块风格 YAML（UTF-8 文本，LF）。"""
    return "\n".join(_dump_node(obj, 0)) + "\n"
