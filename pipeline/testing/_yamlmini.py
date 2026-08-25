#!/usr/bin/env python3
"""_yamlmini —— 自实现的 YAML 子集解析器/序列化器（Python 3.11 标准库 only）。

为什么不用 PyYAML：质量仪器必须离线可跑、零第三方硬依赖（IR-0004 rev6）。
本仓所有 YAML（catalog/checklist/ledger/meta fixture/workflow）均刻意限制在
以下子集内，本模块是该子集的机械实现（无语义猜测，错即报错带行号）。

支持子集：
  - 块映射（缩进嵌套）、块序列（"- "，含序列内映射续行）
  - 标量：plain / 单引号(''转义) / 双引号(JSON转义)；true/false/null/~
  - 流式 [a, b] 与 {k: v}（一层以上递归）
  - 块标量 "|" 与 "|-"（字面量，clip/strip chomping）
  - 注释：整行 "#" 与行内 " #"（引号外）
  - 键只支持 plain 标量（覆盖本仓全部用例）

不支持（出现即 ValueError，fail-closed）：锚点/别名、多文档、tag、">"、
多行 plain 标量、复杂键。
"""
from __future__ import annotations

import json
import re

_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")


def load(path, encoding="utf-8"):
    with open(path, "r", encoding=encoding, newline="") as fh:
        return loads(fh.read())


def loads(text):
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    p = _Parser(lines)
    value, idx = p.parse_block(0, 0, stop_indent=-1)
    trailing = p.next_significant(idx)
    if trailing is not None:
        raise ValueError("yamlmini: 文档结束后仍有多余内容，行 %d: %r" % (trailing[2], trailing[1]))
    return value


def dump(data):
    out = []
    _dump_block(data, 0, out)
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- parser


class _Parser:
    def __init__(self, lines):
        # 行记录: (indent, content, lineno)；行号 1 基
        self._lines = lines

    def next_significant(self, idx):
        i = idx
        n = len(self._lines)
        while i < n:
            indent, content = self._split_line(i)
            if content == "" or content.startswith("#"):
                i += 1
                continue
            return (indent, content, i + 1)
        return None

    def _split_line(self, i):
        raw = self._lines[i]
        stripped = raw.lstrip(" ")
        indent = len(raw) - len(stripped)
        return indent, stripped.rstrip()

    # ------------------------------------------------------------- blocks

    def parse_block(self, idx, indent, stop_indent):
        sig = self.next_significant(idx)
        if sig is None:
            return None, idx
        s_indent, s_content, _ = sig
        if s_indent < indent:
            return None, idx
        if s_content == "-" or s_content.startswith("- "):
            return self.parse_sequence(idx, s_indent)
        return self.parse_mapping(idx, s_indent)

    def parse_mapping(self, idx, indent):
        result = {}
        i = idx
        n = len(self._lines)
        while i < n:
            sig = self.next_significant(i)
            if sig is None:
                break
            s_indent, s_content, s_lineno = sig
            if s_indent != indent:
                if s_indent < indent:
                    break
                raise ValueError("yamlmini: 映射缩进不一致，行 %d: %r" % (s_lineno, s_content))
            key, sep, rest = self._split_key(s_content)
            if sep is None:
                raise ValueError("yamlmini: 期望 'key: value'，行 %d: %r" % (s_lineno, s_content))
            key = _normalize_key(key)
            i = sig[2]  # 下一条物理行
            rest = _strip_comment(rest).strip()
            if rest == "":
                nxt = self.next_significant(i)
                if nxt is not None and nxt[0] > indent:
                    value, i = self.parse_block(i, nxt[0], indent)
                else:
                    value = None
            elif rest in ("|", "|-"):
                value, i = self._parse_block_scalar(i, indent, chomp=rest)
            else:
                value = _parse_scalar_or_flow(rest)
            result[key] = value
        return result, i

    def parse_sequence(self, idx, indent):
        items = []
        i = idx
        n = len(self._lines)
        while i < n:
            sig = self.next_significant(i)
            if sig is None:
                break
            s_indent, s_content, s_lineno = sig
            if s_indent != indent:
                if s_indent < indent:
                    break
                raise ValueError("yamlmini: 序列缩进不一致，行 %d: %r" % (s_lineno, s_content))
            if s_content != "-" and not s_content.startswith("- "):
                break
            rest = s_content[1:].strip()
            i = sig[2]
            if rest == "":
                nxt = self.next_significant(i)
                if nxt is not None and nxt[0] > indent:
                    item, i = self.parse_block(i, nxt[0], indent)
                else:
                    item = None
                items.append(item)
                continue
            # "- key: value" / "- plain"：内容行重写为虚拟 indent+2 再递归
            virtual = indent + 2
            self._lines[s_lineno - 1] = " " * virtual + rest
            if self._is_key_like(rest):
                item, i = self.parse_mapping(i - 1, virtual)
            else:
                item = _parse_scalar_or_flow(_strip_comment(rest).strip())
            items.append(item)
        return items, i

    def _is_key_like(self, text):
        _, sep, _ = self._split_key(text)
        return sep is not None

    def _split_key(self, content):
        # 返回 (key, sep_found, rest)；只在引号外找 ": " 或行尾 ":"
        quote = None
        for pos, ch in enumerate(content):
            if quote:
                if ch == quote:
                    quote = None
                continue
            if ch in ("'", '"'):
                quote = ch
                continue
            if ch == ":" and (pos + 1 == len(content) or content[pos + 1] == " "):
                return content[:pos].rstrip(), True, content[pos + 1 :]
        return content, None, ""

    def _parse_block_scalar(self, idx, key_indent, chomp):
        # 收集所有 indent > key_indent 的物理行（保留空行），到首个缩进回落为止
        collected = []
        i = idx
        n = len(self._lines)
        while i < n:
            raw = self._lines[i]
            if raw.strip() == "":
                collected.append("")
                i += 1
                continue
            indent = len(raw) - len(raw.lstrip(" "))
            if indent <= key_indent:
                break
            collected.append(raw)
            i += 1
        # 去掉尾部空行（clip/strip 都忽略纯尾空行）
        while collected and collected[-1] == "":
            collected.pop()
        if not collected:
            return ("", i) if chomp == "|-" else ("\n", i)
        min_indent = min(len(r) - len(r.lstrip(" ")) for r in collected)
        body = [r[min_indent:] if r else "" for r in collected]
        text = "\n".join(body)
        if chomp == "|-":
            return text, i
        return text + "\n", i


# ---------------------------------------------------------------- scalars


def _normalize_key(key):
    key = key.strip()
    if key.startswith('"') or key.startswith("'"):
        return _parse_plain_or_quoted(key)
    return key


def _strip_comment(text):
    quote = None
    for pos, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            continue
        if ch == "#" and pos > 0 and text[pos - 1] == " ":
            return text[:pos]
    return text


def _parse_scalar_or_flow(text):
    text = text.strip()
    if text.startswith("[") or text.startswith("{"):
        value, rest = _parse_flow(text)
        rest = _strip_comment(rest).strip()
        if rest:
            raise ValueError("yamlmini: 流式集合后有多余内容: %r" % rest)
        return value
    return _parse_plain_or_quoted(text)


def _parse_plain_or_quoted(text):
    if text.startswith('"'):
        try:
            return json.loads(_cut_double_quoted(text)[0])
        except Exception as exc:  # noqa: BLE001
            raise ValueError("yamlmini: 非法双引号标量 %r: %s" % (text, exc)) from exc
    if text.startswith("'"):
        token = _cut_single_quoted(text)[0]
        return token[1:-1].replace("''", "'")
    return _convert_plain(text)


def _cut_double_quoted(text):
    quote = '"'
    pos = 1
    while pos < len(text):
        if text[pos] == "\\":
            pos += 2
            continue
        if text[pos] == quote:
            return text[: pos + 1], text[pos + 1 :]
        pos += 1
    raise ValueError("yamlmini: 双引号未闭合: %r" % text)


def _cut_single_quoted(text):
    pos = 1
    while pos < len(text):
        if text[pos] == "'":
            if pos + 1 < len(text) and text[pos + 1] == "'":
                pos += 2
                continue
            return text[: pos + 1], text[pos + 1 :]
        pos += 1
    raise ValueError("yamlmini: 单引号未闭合: %r" % text)


def _convert_plain(text):
    if text == "":
        return None
    low = text.lower()
    if low in ("null", "~"):
        return None
    if low == "true":
        return True
    if low == "false":
        return False
    if _INT_RE.match(text):
        return int(text)
    if _FLOAT_RE.match(text) and any(c in text for c in ".eE"):
        return float(text)
    return text


def _parse_flow(text):
    text = text.strip()
    if text.startswith("["):
        items, rest = _parse_flow_seq(text)
        return items, rest
    if text.startswith("{"):
        mapping, rest = _parse_flow_map(text)
        return mapping, rest
    raise ValueError("yamlmini: 非法流式开头: %r" % text)


def _parse_flow_seq(text):
    assert text.startswith("[")
    pos = 1
    items = []
    buf = ""
    depth = 0
    quote = None
    while pos < len(text):
        ch = text[pos]
        if quote:
            buf += ch
            if ch == quote:
                quote = None
            pos += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf += ch
        elif ch in "[{":
            depth += 1
            buf += ch
        elif ch in "]}":
            if depth == 0 and ch == "]":
                if buf.strip():
                    items.append(_parse_scalar_or_flow(buf.strip()))
                elif items:
                    items.append(None)  # 尾逗号
                return items, text[pos + 1 :]
            depth -= 1
            buf += ch
        elif ch == "," and depth == 0:
            items.append(_parse_scalar_or_flow(buf.strip()) if buf.strip() else None)
            buf = ""
        else:
            buf += ch
        pos += 1
    raise ValueError("yamlmini: 流式序列未闭合: %r" % text)


def _parse_flow_map(text):
    assert text.startswith("{")
    pos = 1
    mapping = {}
    key_buf = ""
    val_buf = ""
    in_value = False
    depth = 0
    quote = None

    def emit(ch):
        nonlocal key_buf, val_buf
        if in_value:
            val_buf += ch
        else:
            key_buf += ch

    while pos < len(text):
        ch = text[pos]
        if quote:
            emit(ch)
            if ch == quote:
                quote = None
            pos += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            emit(ch)
        elif ch in "[{":
            depth += 1
            emit(ch)
        elif ch in "]}":
            if depth == 0 and ch == "}":
                if key_buf.strip():
                    mapping[_flow_key(key_buf)] = _parse_scalar_or_flow(val_buf.strip()) if val_buf.strip() else None
                return mapping, text[pos + 1 :]
            depth -= 1
            emit(ch)
        elif ch == ":" and depth == 0 and not in_value:
            in_value = True
        elif ch == "," and depth == 0:
            mapping[_flow_key(key_buf)] = _parse_scalar_or_flow(val_buf.strip()) if val_buf.strip() else None
            key_buf, val_buf, in_value = "", "", False
        else:
            emit(ch)
        pos += 1
    raise ValueError("yamlmini: 流式映射未闭合: %r" % text)


def _flow_key(buf):
    key = buf.strip()
    if key.startswith('"') or key.startswith("'"):
        return _parse_plain_or_quoted(key)
    return key


# ---------------------------------------------------------------- dumper


def _dump_block(data, indent, out):
    pad = " " * indent
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, dict) and value:
                out.append(pad + _dump_key(key) + ":")
                _dump_block(value, indent + 2, out)
            elif isinstance(value, list) and value:
                out.append(pad + _dump_key(key) + ":")
                _dump_block(value, indent + 2, out)
            else:
                out.append(pad + _dump_key(key) + ": " + _dump_scalar(value))
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item:
                first = True
                for key, value in item.items():
                    prefix = pad + ("- " if first else "  ")
                    if isinstance(value, dict) and value:
                        out.append(prefix + _dump_key(key) + ":")
                        _dump_block(value, indent + 4, out)
                    elif isinstance(value, list) and value:
                        out.append(prefix + _dump_key(key) + ":")
                        _dump_block(value, indent + 4, out)
                    else:
                        out.append(prefix + _dump_key(key) + ": " + _dump_scalar(value))
                    first = False
            elif isinstance(item, list) and item:
                out.append(pad + "-")
                _dump_block(item, indent + 2, out)
            else:
                out.append(pad + "- " + _dump_scalar(item))


def _dump_key(key):
    if not isinstance(key, str):
        key = str(key)
    return json.dumps(key, ensure_ascii=False)


def _dump_scalar(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list) and not value:
        return "[]"
    if isinstance(value, dict) and not value:
        return "{}"
    raise ValueError("yamlmini: 不支持的标量类型 %r" % type(value))
