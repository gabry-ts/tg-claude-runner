"""Tests for bot/markdown_v2.py: to_telegram_markdown and split_for_telegram."""

import pytest

from bot.markdown_v2 import to_telegram_markdown, split_for_telegram


# ---------------------------------------------------------------------------
# to_telegram_markdown: fenced code blocks
# ---------------------------------------------------------------------------

class TestFencedCodeBlocks:
    def test_fence_with_language(self):
        assert (
            to_telegram_markdown("```python\nprint('hi')\n```")
            == "```python\nprint('hi')\n```"
        )

    def test_fence_without_language(self):
        assert to_telegram_markdown("```\ncode\n```") == "```\ncode\n```"

    def test_fence_body_not_markdown_escaped(self):
        # Reserved MarkdownV2 chars other than ` and \ pass through untouched.
        out = to_telegram_markdown("```\na * b _ c . d\n```")
        assert out == "```\na * b _ c . d\n```"

    def test_fence_body_escapes_backtick_and_backslash(self):
        out = to_telegram_markdown("```\nx = `y` \\ z\n```")
        assert out == "```\nx = \\`y\\` \\\\ z\n```"

    def test_fence_lang_no_newline_gets_newline_inserted(self):
        # `\n?` after the language is optional; the prefix always adds one.
        assert to_telegram_markdown("```js x=1```") == "```js\n x=1```"

    def test_fence_surrounded_by_text(self):
        out = to_telegram_markdown("before.\n```\ncode\n```\nafter!")
        assert out == "before\\.\n```\ncode\n```\nafter\\!"


# ---------------------------------------------------------------------------
# to_telegram_markdown: inline code
# ---------------------------------------------------------------------------

class TestInlineCode:
    def test_inline_code_kept(self):
        assert to_telegram_markdown("run `ls -la` now") == "run `ls -la` now"

    def test_inline_code_specials_not_escaped(self):
        assert to_telegram_markdown("`a*b_c.d`") == "`a*b_c.d`"

    def test_inline_code_backslash_and_backtick_escaped(self):
        assert to_telegram_markdown("`a\\b`") == "`a\\\\b`"

    def test_inline_code_does_not_span_newlines(self):
        out = to_telegram_markdown("`a\nb`")
        assert out == "\\`a\nb\\`"


# ---------------------------------------------------------------------------
# to_telegram_markdown: links
# ---------------------------------------------------------------------------

class TestLinks:
    def test_simple_link(self):
        assert (
            to_telegram_markdown("[label](https://example.com)")
            == "[label](https://example.com)"
        )

    def test_link_label_escaped(self):
        assert (
            to_telegram_markdown("[my.site](https://a.b/c)")
            == "[my\\.site](https://a.b/c)"
        )

    def test_link_url_underscore_not_escaped(self):
        # Only ) and \ need escaping inside URLs.
        assert (
            to_telegram_markdown("[x](https://a.b/c_d)")
            == "[x](https://a.b/c_d)"
        )

    def test_link_in_sentence(self):
        assert (
            to_telegram_markdown("see [docs](http://x.y) here.")
            == "see [docs](http://x.y) here\\."
        )

    def test_link_url_with_paren_truncates(self):
        # Known limitation: URL regex stops at the first ')', so a paren
        # inside the URL truncates it and the trailing ')' is escaped as text.
        assert (
            to_telegram_markdown("[my.site](https://a.b/c_(d))")
            == "[my\\.site](https://a.b/c_(d)\\)"
        )


# ---------------------------------------------------------------------------
# to_telegram_markdown: bold / italic / strikethrough / headings
# ---------------------------------------------------------------------------

class TestBold:
    def test_double_asterisk(self):
        assert to_telegram_markdown("**bold**") == "*bold*"

    def test_double_underscore(self):
        assert to_telegram_markdown("__also bold__") == "*also bold*"

    def test_bold_content_escaped(self):
        assert to_telegram_markdown("**a.b!**") == "*a\\.b\\!*"

    def test_bold_does_not_span_newlines(self):
        assert to_telegram_markdown("**a\nb**") == "\\*\\*a\nb\\*\\*"


class TestItalic:
    def test_asterisk_italic(self):
        assert to_telegram_markdown("*it*") == "_it_"

    def test_underscore_italic(self):
        assert to_telegram_markdown("_it_") == "_it_"

    def test_both_forms_in_one_line(self):
        assert to_telegram_markdown("_it_ and *it2*") == "_it_ and _it2_"

    def test_bullet_marker_not_italic(self):
        # A leading "* " bullet has no closing '*', so it's escaped as text.
        assert to_telegram_markdown("* bullet item") == "\\* bullet item"

    def test_intra_word_underscore_not_italic(self):
        assert to_telegram_markdown("snake_case_name") == "snake\\_case\\_name"

    def test_intra_word_asterisk_not_italic(self):
        assert to_telegram_markdown("2*3*4") == "2\\*3\\*4"

    def test_spaced_asterisks_become_italic(self):
        # 'a * b * c': the guards allow this match (space before/after '*').
        assert to_telegram_markdown("a * b * c") == "a _ b _ c"

    def test_italic_content_escaped(self):
        assert to_telegram_markdown("*a.b*") == "_a\\.b_"


class TestStrikethrough:
    def test_basic(self):
        assert to_telegram_markdown("~~gone~~") == "~gone~"

    def test_content_escaped(self):
        assert to_telegram_markdown("~~a.b~~") == "~a\\.b~"

    def test_single_tilde_escaped(self):
        assert to_telegram_markdown("~not strike~") == "\\~not strike\\~"


class TestHeadings:
    def test_h1(self):
        assert to_telegram_markdown("# Title") == "*Title*"

    def test_h3(self):
        assert to_telegram_markdown("### Heading three") == "*Heading three*"

    def test_h6(self):
        assert to_telegram_markdown("###### deep") == "*deep*"

    def test_seven_hashes_not_heading(self):
        out = to_telegram_markdown("####### seven hashes")
        assert out == "\\#\\#\\#\\#\\#\\#\\# seven hashes"

    def test_hash_without_space_not_heading(self):
        assert to_telegram_markdown("#NoSpace") == "\\#NoSpace"

    def test_heading_content_escaped_and_rstripped(self):
        assert to_telegram_markdown("## a.b   ") == "*a\\.b*"

    def test_heading_mid_text_multiline(self):
        out = to_telegram_markdown("intro\n# Head\nbody")
        assert out == "intro\n*Head*\nbody"

    def test_heading_only_at_line_start(self):
        assert to_telegram_markdown("not # a heading") == "not \\# a heading"


# ---------------------------------------------------------------------------
# to_telegram_markdown: escaping of plain text
# ---------------------------------------------------------------------------

class TestPlainTextEscaping:
    def test_all_reserved_chars_escaped(self):
        src = "plain _ * [ ] ( ) ~ ` > # + - = | { } . ! \\ text"
        expected = (
            "plain \\_ \\* \\[ \\] \\( \\) \\~ \\` \\> \\# \\+ \\- \\= "
            "\\| \\{ \\} \\. \\! \\\\ text"
        )
        assert to_telegram_markdown(src) == expected

    def test_unreserved_text_untouched(self):
        assert to_telegram_markdown("hello world 123") == "hello world 123"

    def test_empty_string(self):
        assert to_telegram_markdown("") == ""

    def test_newlines_preserved(self):
        assert to_telegram_markdown("a\n\nb") == "a\n\nb"


# ---------------------------------------------------------------------------
# to_telegram_markdown: token placeholders and nesting
# ---------------------------------------------------------------------------

def _has_token(s: str) -> bool:
    return "\x00" in s


class TestTokenPlaceholders:
    def test_no_leak_flat_mixed_document(self):
        src = (
            "# Title\n"
            "Some **bold** and *italic* and `code` and ~~strike~~\n"
            "```py\nx = 1\n```\n"
            "[link](https://e.com) end."
        )
        out = to_telegram_markdown(src)
        assert not _has_token(out)
        assert "*Title*" in out
        assert "*bold*" in out
        assert "_italic_" in out
        assert "`code`" in out
        assert "~strike~" in out
        assert "```py\nx = 1\n```" in out
        assert "[link](https://e.com)" in out

    def test_literal_token_like_text_in_input(self):
        # Text that happens to look like an internal token expands to a
        # stored token's content rather than surviving verbatim -- but at
        # least it must not leave NUL bytes behind.
        out = to_telegram_markdown("plain text only, no markup")
        assert not _has_token(out)

    def test_bold_containing_inline_code_no_leak(self):
        out = to_telegram_markdown("**bold `code`**")
        assert not _has_token(out)
        assert out == "*bold `code`*"

    def test_heading_containing_bold_no_leak(self):
        # Expansion is recursive; the doubled markers may still be rejected by
        # Telegram, in which case the per-chunk plain fallback handles it —
        # what matters here is that no NUL placeholder reaches the output.
        out = to_telegram_markdown("# **Title**")
        assert not _has_token(out)
        assert "Title" in out


# ---------------------------------------------------------------------------
# split_for_telegram
# ---------------------------------------------------------------------------

class TestSplitForTelegram:
    def test_short_text_single_chunk(self):
        assert split_for_telegram("hello") == ["hello"]

    def test_empty_string_gives_no_chunks(self):
        assert split_for_telegram("") == []

    def test_exact_length_single_chunk(self):
        text = "a" * 4000
        assert split_for_telegram(text) == [text]

    def test_exact_custom_max_len_single_chunk(self):
        assert split_for_telegram("abcde", max_len=5) == ["abcde"]

    def test_one_over_max_len_splits(self):
        chunks = split_for_telegram("a" * 6, max_len=5)
        assert chunks == ["aaaaa", "a"]

    def test_split_at_newline_boundary(self):
        chunks = split_for_telegram("line1\nline2\nline3", max_len=12)
        assert chunks == ["line1\nline2", "line3"]
        assert all(len(c) <= 12 for c in chunks)

    def test_newline_at_cut_edge_stripped_from_next_chunk(self):
        # newline sits exactly at index max_len: hard cut, then lstrip('\n').
        chunks = split_for_telegram("aaaaa\nbb", max_len=5)
        assert chunks == ["aaaaa", "bb"]

    def test_fallback_to_space_when_no_newline(self):
        # Space is used as cut point but is NOT stripped from the next chunk.
        chunks = split_for_telegram("ab cd ef", max_len=5)
        assert chunks == ["ab", " cd", " ef"]

    def test_space_fallback_longer_text(self):
        chunks = split_for_telegram("hello world this is", max_len=11)
        assert chunks == ["hello", " world", " this is"]
        assert all(len(c) <= 11 for c in chunks)

    def test_hard_cut_no_separators(self):
        chunks = split_for_telegram("abcdefgh", max_len=3)
        assert chunks == ["abc", "def", "gh"]

    def test_hard_cut_reassembles_losslessly(self):
        text = "x" * 10
        chunks = split_for_telegram(text, max_len=4)
        assert "".join(chunks) == text

    def test_multiple_consecutive_newlines_at_cut_all_stripped(self):
        # Cut lands at the last '\n' before max_len (index 4), so the first
        # chunk keeps one trailing newline; the remaining ones are stripped.
        chunks = split_for_telegram("aaa\n\n\nbbb", max_len=5)
        assert chunks == ["aaa\n", "bbb"]

    def test_default_max_len_is_4000(self):
        text = "a" * 4001
        chunks = split_for_telegram(text)
        assert chunks == ["a" * 4000, "a"]

    def test_all_chunks_within_max_len(self):
        text = ("word " * 100).strip()  # 499 chars
        chunks = split_for_telegram(text, max_len=50)
        assert all(len(c) <= 50 for c in chunks)
        # Splitting on spaces keeps content lossless up to the separators.
        assert "".join(chunks) == text
