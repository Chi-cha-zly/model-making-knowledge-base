"""Pure text rendering and lexical normalization shared by ingestion and retrieval."""

import re
import unicodedata

STOP_WORDS = set(
    '你好 请 请问 帮我 推荐 一下 模型 制作 方法 怎么 如何 什么 这个 那个 可以 应该 想要 我想 知道 更多 资料 参考 注意 安全 处理 入门 使用 的 是 有 吗 呢 吧 了 和 与 the a an and or how what to for of is please'.split()
)

STOP_PHRASES = sorted(STOP_WORDS | {'请帮我', '模型制作', '告诉我'}, key=lambda x: (-len(x), x))

TOKEN_RE = re.compile(r'[a-z0-9]+(?:[-_][a-z0-9]+)*|[\u4e00-\u9fff]+')


def readable_excerpt(value):
    """Render structured evidence without dropping meaningful false/zero values."""
    if value is None:
        return ''
    if isinstance(value, dict):
        lines = []
        for label, item in value.items():
            rendered = readable_excerpt(item)
            if rendered:
                separator = '\n' if isinstance(item, (dict, list)) else ''
                lines.append(f'{label}：{separator}{rendered}')
        return '\n'.join(lines)
    if isinstance(value, list):
        return '\n'.join('- ' + text for item in value if (text := readable_excerpt(item)))
    if isinstance(value, bool):
        return '是' if value else '否'
    return str(value).strip()


def _normalize(value):
    return unicodedata.normalize('NFKC', value).casefold()


def _tokens(text, *, single=False):
    tokens = []
    for word in TOKEN_RE.findall(_normalize(text)):
        if word in STOP_WORDS:
            continue
        if re.fullmatch(r'[\u4e00-\u9fff]+', word):
            if len(word) == 1:
                if single:
                    tokens.append(word)
            else:
                tokens.extend(
                    word[i : i + 2]
                    for i in range(len(word) - 1)
                    if word[i : i + 2] not in STOP_WORDS
                )
                if len(word) > 2:
                    tokens.append(word)
        else:
            tokens.append(word)
    return tokens
