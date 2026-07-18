"""Convert standard markdown to Telegram MarkdownV2 (best-effort).

Telegram MarkdownV2 reserves these characters and requires backslash-escape
outside of code/links: _ * [ ] ( ) ~ ` > # + - = | { } . ! \\

Inside code spans/blocks: only ` and \\ must be escaped.
Inside link URLs: only ) and \\ must be escaped.
"""

import re


def _escape_special(s: str) -> str:
    return re.sub(r'([_*\[\]()~`>#+\-=|{}.!\\])', r'\\\1', s)


def _escape_in_code(s: str) -> str:
    return s.replace('\\', '\\\\').replace('`', '\\`')


def _escape_in_link(s: str) -> str:
    return s.replace('\\', '\\\\').replace(')', '\\)')


_TOKEN_RE = re.compile(r'\x00T(\d+)\x00')
_SPLIT_RE = re.compile(r'(\x00T\d+\x00)')


def to_telegram_markdown(text: str) -> str:
    """Convert markdown -> Telegram MarkdownV2.

    Tokenises code/inline-code/links/bold/italic/strike/headings, escapes the
    rest, then re-injects tokens. Best-effort: edge cases may produce invalid
    MarkdownV2, callers should fall back to plain text on send failure.
    """
    tokens: list[str] = []

    def store(s: str) -> str:
        idx = len(tokens)
        tokens.append(s)
        return f'\x00T{idx}\x00'

    # 1. Fenced code blocks
    def _repl_fence(m: re.Match) -> str:
        lang = (m.group(1) or '').strip()
        body = m.group(2)
        prefix = f'```{lang}\n' if lang else '```\n'
        return store(prefix + _escape_in_code(body) + '```')

    text = re.sub(r'```(\w*)\n?(.*?)```', _repl_fence, text, flags=re.DOTALL)

    # 2. Inline code
    text = re.sub(
        r'`([^`\n]+)`',
        lambda m: store('`' + _escape_in_code(m.group(1)) + '`'),
        text,
    )

    # 3. Links [label](url)
    text = re.sub(
        r'\[([^\]\n]+)\]\(([^)\n]+)\)',
        lambda m: store(
            '[' + _escape_special(m.group(1)) + '](' + _escape_in_link(m.group(2)) + ')'
        ),
        text,
    )

    # 4. Bold (**...**, __...__)
    text = re.sub(
        r'\*\*([^*\n]+?)\*\*|__([^_\n]+?)__',
        lambda m: store('*' + _escape_special(m.group(1) or m.group(2)) + '*'),
        text,
    )

    # 5. Italic (*...*, _..._) — guarded against bullet markers and intra-word _
    text = re.sub(
        r'(?<![*\w\\])\*([^*\n]+?)\*(?![*\w])',
        lambda m: store('_' + _escape_special(m.group(1)) + '_'),
        text,
    )
    text = re.sub(
        r'(?<![_\w\\])_([^_\n]+?)_(?!\w)',
        lambda m: store('_' + _escape_special(m.group(1)) + '_'),
        text,
    )

    # 6. Strikethrough
    text = re.sub(
        r'~~([^~\n]+)~~',
        lambda m: store('~' + _escape_special(m.group(1)) + '~'),
        text,
    )

    # 7. Headings -> bold (Telegram has no heading syntax)
    text = re.sub(
        r'^(#{1,6})[ \t]+([^\n]+)',
        lambda m: store('*' + _escape_special(m.group(2).rstrip()) + '*'),
        text,
        flags=re.MULTILINE,
    )

    # 8. Escape the remainder, leaving tokens intact
    parts = _SPLIT_RE.split(text)
    out = []
    for p in parts:
        if p.startswith('\x00T') and p.endswith('\x00'):
            out.append(p)
        else:
            out.append(_escape_special(p))
    text = ''.join(out)

    # 9. Expand tokens. Constructs can nest (bold containing inline code,
    # heading containing bold), leaving a token inside another token's text —
    # keep expanding until none remain (bounded: nesting is at most a few
    # levels deep, and a stale pass means an unknown placeholder we bail on).
    for _ in range(10):
        expanded = _TOKEN_RE.sub(lambda m: tokens[int(m.group(1))], text)
        if expanded == text:
            break
        text = expanded

    return text


def split_for_telegram(text: str, max_len: int = 4000) -> list[str]:
    """Split a (potentially long) message at safe boundaries for Telegram."""
    chunks: list[str] = []
    while len(text) > max_len:
        cut = text.rfind('\n', 0, max_len)
        if cut <= 0:
            cut = text.rfind(' ', 0, max_len)
        if cut <= 0:
            cut = max_len
        chunks.append(text[:cut])
        text = text[cut:].lstrip('\n')
    if text:
        chunks.append(text)
    return chunks
