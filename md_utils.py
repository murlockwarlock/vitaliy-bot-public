"""
Конвертер Markdown → Telegram HTML и умный сплит длинных сообщений.
Адаптировано из проверенных психоботов.
"""
import re
import html


def _fix_unclosed_html_tags(text: str) -> str:
    """Полностью перестраивает HTML-теги для корректной вложенности.

    Обрабатывает ВСЕ виды невалидного HTML:
    - Лишние закрывающие теги (</i> без <i>) → удаляются
    - Незакрытые теги (<b> без </b>) → закрываются в конце
    - Перекрёстная вложенность (<b><i>...</b></i>) → исправляется порядок
    - Дублированные теги (<b><b>...</b></b>) → дубли пропускаются
    """
    allowed_tags = {'b', 'i', 's', 'code', 'pre', 'a'}
    tag_pattern = re.compile(r'<(/?)([a-z1-6]+)([^>]*)>', re.IGNORECASE)

    segments = []
    last_end = 0
    for match in tag_pattern.finditer(text):
        tag_name = match.group(2).lower()
        if tag_name not in allowed_tags:
            continue
        if match.start() > last_end:
            segments.append(('text', text[last_end:match.start()]))
        is_closing = bool(match.group(1))
        segments.append(('close' if is_closing else 'open', tag_name, match.group(0)))
        last_end = match.end()
    if last_end < len(text):
        segments.append(('text', text[last_end:]))

    result = []
    stack = []

    for seg in segments:
        if seg[0] == 'text':
            result.append(seg[1])
        elif seg[0] == 'open':
            tag_name = seg[1]
            if any(t[0] == tag_name for t in stack):
                continue
            stack.append((tag_name, seg[2]))
            result.append(seg[2])
        elif seg[0] == 'close':
            tag_name = seg[1]
            idx = None
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][0] == tag_name:
                    idx = i
                    break
            if idx is None:
                continue
            tags_to_reopen = []
            while len(stack) > idx + 1:
                t = stack.pop()
                result.append(f'</{t[0]}>')
                tags_to_reopen.append(t)
            stack.pop()
            result.append(f'</{tag_name}>')
            for t in reversed(tags_to_reopen):
                stack.append(t)
                result.append(t[1])

    while stack:
        t = stack.pop()
        result.append(f'</{t[0]}>')

    return ''.join(result)


def markdown_to_html(text: str) -> str:
    """Конвертирует markdown (от AI) в Telegram HTML."""
    if not text:
        return ""

    # Сначала нормализуем уже существующие HTML-теги обратно в markdown
    text = re.sub(r'<b>(.*?)</b>', r'**\1**', text, flags=re.DOTALL)
    text = re.sub(r'<strong>(.*?)</strong>', r'**\1**', text, flags=re.DOTALL)
    text = re.sub(r'<i>(.*?)</i>', r'*\1*', text, flags=re.DOTALL)
    text = re.sub(r'<em>(.*?)</em>', r'*\1*', text, flags=re.DOTALL)
    text = re.sub(r'<pre><code>(.*?)</code></pre>', r'```\1```', text, flags=re.DOTALL)
    text = re.sub(r'<code>(.*?)</code>', r'`\1`', text, flags=re.DOTALL)

    # Экранируем HTML-спецсимволы
    text = html.escape(text, quote=False)

    # Сохраняем блоки кода
    code_blocks = {}

    def save_code_block(match):
        key = f"\x01CODEBLOCK{len(code_blocks)}\x01"
        code_blocks[key] = match.group(1)
        return key

    def save_inline_code(match):
        key = f"\x01INLINE{len(code_blocks)}\x01"
        code_blocks[key] = match.group(1)
        return key

    text = re.sub(r'```(.*?)```', save_code_block, text, flags=re.DOTALL)
    text = re.sub(r'`(.*?)`', save_inline_code, text)

    # Горизонтальные линии
    text = re.sub(r'^\s*[\*_-]{3,}\s*$', '———', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*\*\s*$', '———', text, flags=re.MULTILINE)

    # Списки
    text = re.sub(r'^\s*[-*]\s+', '• ', text, flags=re.MULTILINE)

    # Заголовки → жирный текст
    text = re.sub(
        r'^\s*#{1,6}\s+(.*)',
        lambda m: '\n\n<b>' + m.group(1).replace('***', '').replace('**', '').replace('*', '') + '</b>\n',
        text, flags=re.MULTILINE
    )

    # Bold + Italic (***text*** или ___text___)
    text = re.sub(r'\*\*\*(?=[^<>]*\*\*\*)((?:(?!\n\n)[^<>])+?)\*\*\*', r'<b><i>\1</i></b>', text)
    text = re.sub(r'___((?:(?!\n\n)[^<>])+?)___', r'<b><i>\1</i></b>', text)

    # Bold (**text** или __text__)
    text = re.sub(r'\*\*(?=[^<>]*\*\*)((?:(?!\n\n)[^<>])+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'__(?=[^<>]*__)((?:(?!\n\n)[^<>])+?)__', r'<b>\1</b>', text)

    # Italic (*text* или _text_)
    text = re.sub(r'(?<!\w)\*(?!\s)([^<>\n]+?)(?<!\s)\*(?!\w)', r'<i>\1</i>', text)
    text = re.sub(r'(?<!\w)_(?!\s)([^<>\n]+?)(?<!\s)_(?!\w)', r'<i>\1</i>', text)

    # Strikethrough (~~text~~)
    text = re.sub(r'~~(?=[^<>\n]+~~)([^<>\n]+?)~~', r'<s>\1</s>', text)

    # Ссылки [text](url)
    text = re.sub(r'\[(.*?)\]\((.*?)\)', r'<a href="\2">\1</a>', text)

    # Красивые переносы перед заголовками и списками
    text = re.sub(r'([^\n])\n(<b>)', r'\1\n\n\2', text)
    text = re.sub(r'([^\n])\n(•)', r'\1\n\n\2', text)

    # Убираем лишние переносы
    text = re.sub(r'\n{3,}', '\n\n', text)

    # Чистим оставшийся markdown
    text = text.replace('**', '').replace('__', '').replace('~~', '')
    text = text.replace('<b></b>', '').replace('<i></i>', '')
    text = re.sub(r'(?<![\w*])\*(?![\w*])', '', text)

    # Восстанавливаем блоки кода
    for key, value in code_blocks.items():
        if "CODEBLOCK" in key:
            replacement = f'<pre><code>{value}</code></pre>'
        else:
            replacement = f'<code>{value}</code>'
        text = text.replace(key, replacement)

    text = text.strip()
    text = _fix_unclosed_html_tags(text)

    return text


def remove_markdown(text: str) -> str:
    """Полностью убирает markdown-разметку, оставляя чистый текст."""
    text = re.sub(r'#+\s+', '', text)
    text = re.sub(r'\*\*(.*?)\*\*|__(.*?)__', r'\1', text)
    text = re.sub(r'\*(.*?)\*|_(.*?)_', r'\1', text)
    text = re.sub(r'~~(.*?)~~', r'\1', text)
    text = re.sub(r'`(.*?)`', r'\1', text)
    text = re.sub(r'\[(.*?)\]\((.*?)\)', r'\1', text)
    return text


def split_html_message(text: str, max_length: int = 4090) -> list[str]:
    """Разбивает HTML-текст на части с корректным закрытием/открытием тегов."""
    if not text:
        return []
    if len(text) <= max_length:
        return [text]

    chunks = []
    current_chunk = ""
    open_tags = []

    tag_re = re.compile(r'(</?[a-z1-6]+(?: [^>]+)?>)', re.IGNORECASE)
    parts = tag_re.split(text)

    def _suffix():
        return "".join([f"</{t[0]}>" for t in reversed(open_tags)])

    def _reopen():
        return "".join([t[1] for t in open_tags])

    def _close_size():
        return sum(len(t[0]) + 3 for t in open_tags)

    for part in parts:
        if not part:
            continue

        if part.startswith('<'):
            tag_match = re.match(r'<(/?)([a-z1-6]+)', part, re.IGNORECASE)
            if tag_match:
                is_closing = bool(tag_match.group(1))
                tag_name = tag_match.group(2).lower()

                if is_closing:
                    if open_tags and open_tags[-1][0] == tag_name:
                        open_tags.pop()
                elif tag_name not in ['br', 'hr', 'img']:
                    open_tags.append((tag_name, part))

            suffix = _suffix()
            if len(current_chunk) + len(part) + len(suffix) > max_length:
                if current_chunk.strip():
                    chunks.append(current_chunk + suffix)
                current_chunk = _reopen()

            current_chunk += part
        else:
            while len(current_chunk) + len(part) + _close_size() > max_length:
                remaining_space = max_length - len(current_chunk) - _close_size()

                if remaining_space <= 10:
                    suffix = _suffix()
                    if current_chunk.strip():
                        chunks.append(current_chunk + suffix)
                    current_chunk = _reopen()
                    remaining_space = max_length - len(current_chunk) - _close_size()

                split_at = remaining_space

                for separator in ('\n\n', '. ', '! ', '? ', '\n'):
                    found_at = part.rfind(separator, 0, remaining_space)
                    if found_at != -1 and found_at > (remaining_space // 3):
                        split_at = found_at + len(separator.rstrip())
                        break
                else:
                    split_at = part.rfind(' ', 0, remaining_space)
                    if split_at == -1:
                        split_at = remaining_space

                content = part[:split_at]
                suffix = _suffix()

                if (current_chunk + content).strip():
                    chunks.append(current_chunk + content + suffix)

                current_chunk = _reopen()
                part = part[split_at:].lstrip()

            current_chunk += part

    if current_chunk.strip():
        clean_chunk = re.sub(r'(<[a-z1-6][^>]*>)+$', '', current_chunk)
        if clean_chunk.strip():
            suffix = _suffix()
            final_c = clean_chunk + suffix
            if re.sub(r'<[^>]+>', '', final_c).strip():
                chunks.append(final_c)

    return [c for c in chunks if re.sub(r'<[^>]+>', '', c).strip()]


TELEGRAM_TAGS_PATTERN = r'(?:b|strong|i|em|u|ins|s|strike|del|span|tg-spoiler|a|code|pre|blockquote|tg-emoji)'


def strip_telegram_html(text: str) -> str:
    """Strips Telegram-supported HTML tags and unescapes entities to produce clean human-readable plain text.

    Safely handles both complete tags and malformed/incomplete tag fragments (e.g. '<b', '<i broken', '<code attr')
    without destroying legitimate angle-bracket user text like '<3', 'A < B & C', or 'x > y'.
    """
    if not text:
        return ""
    # 1. Remove complete Telegram tags (e.g., <b>, </i>, <a href="...">, <code class="...">)
    clean = re.sub(
        r'</?' + TELEGRAM_TAGS_PATTERN + r'(?![a-zA-Z0-9_-])(?:\s+[^>]*)?>',
        '',
        text,
        flags=re.IGNORECASE,
    )
    # 2. Remove incomplete/unclosed Telegram tag fragments (e.g., "<b", "<i broken", "<code attr")
    clean = re.sub(
        r'</?' + TELEGRAM_TAGS_PATTERN + r'(?![a-zA-Z0-9_-])[^<>\n\r]*',
        '',
        clean,
        flags=re.IGNORECASE,
    )
    return html.unescape(clean)
