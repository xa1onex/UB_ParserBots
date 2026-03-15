"""
Bot Cloner & State Architect — интеллектуальный клонер ботов на Telethon.
Строит древовидный граф состояний (текст + кнопки) с учётом переходов по командам и callback.

Навигация через edit_message:
  - При нажатии инлайн-кнопки (CallbackQuery) запоминается триггер (callback_data) и message_id.
  - При приходе MessageEdited для этого message_id новое содержимое записывается как дочерний узел.
  - В JSON: переход [Кнопка/Callback] -> [Изменённое сообщение] (ключ children = callback_data).
"""

import asyncio
import hashlib
import json
import os
import re
import sys
import uuid
from datetime import datetime
from typing import Optional, Tuple

from telethon import TelegramClient, events
from telethon.tl.types import KeyboardButtonCallback, KeyboardButtonUrl

API_ID = 39025103
API_HASH = "31fa6df66fe3c58d5d9c5dea8aa5e151"

# --- Анализ сообщений: белый список (KEEP) vs мусор (SKIP) ---
# Цель: максимально полное копирование функционала любого бота без привязки к нише.

# Призывы к подписке (ОП) — учитываются только вместе с «только ссылки на каналы»
SUBSCRIPTION_KEYWORDS = ("подпишись", "подписка", "вступи", "канал", "subscribe", "join")

# Универсальная навигация (стрелки, пагинация, управляющий текст)
NAV_ARROWS = ("⬅️", "➡️", "⬅", "➡", "🔙", "◀️", "▶️", "⏫", "⏬", "🔼", "🔽", "◀", "▶", "←", "→")
NAV_CONTROL_WORDS = (
    "назад", "в меню", "отмена", "далее", "продолжить", "запуск", "menu", "back", "cancel", "next",
    "вернуться", "❌",
)
NAV_PAGE_PATTERN = re.compile(r"^\s*(\d+\s*/\s*\d+|стр\.\s*\d+|\(\d+\s+из\s+\d+\))\s*$", re.IGNORECASE)


def _has_callback_buttons(buttons: list) -> bool:
    """Есть хотя бы одна кнопка с callback_data — функциональный узел дерева."""
    if not buttons:
        return False
    for btn in buttons:
        if btn.get("callback_data"):
            return True
    return False


def _has_navigation_buttons(buttons: list) -> bool:
    """Есть стрелки, пагинация (1/5, стр. 2) или управляющий текст (Назад, Menu и т.д.)."""
    if not buttons:
        return False
    for btn in buttons:
        label = (btn.get("text") or "").strip()
        label_lower = label.lower()
        if not label:
            continue
        for arrow in NAV_ARROWS:
            if arrow in label:
                return True
        for word in NAV_CONTROL_WORDS:
            if word in label_lower:
                return True
        if NAV_PAGE_PATTERN.match(label):
            return True
    return False


def _is_functional_link(url: str, bot_username: Optional[str] = None) -> bool:
    """
    Ссылка функциональная (белый список): бот, системные функции, группы, шаринг, оплата.
    Не ведёт на «чужой» канал для ОП.
    """
    if not url:
        return True
    u = str(url).lower().strip()
    # Системные функции Telegram
    if re.search(r"t\.me/(addemoji|addstickers|addtheme)/", u):
        return True
    if "t.me/share" in u or "t.me/$" in u:
        return True
    if "startgroup=true" in u or "startgroup=" in u:
        return True
    # Реферальные/командные ссылки самого бота (t.me/BotName?start=...)
    if bot_username:
        bot_clean = str(bot_username).lower().replace("@", "")
        if bot_clean in u and "start=" in u:
            return True
    return False


def _is_external_channel_link(url: str) -> bool:
    """Ссылка ведёт на внешний канал/чат (joinchat, t.me/+), а не на функциональный контент."""
    if not url:
        return False
    u = str(url).lower()
    if "joinchat" in u or "t.me/+" in u or re.search(r"t\.me/c/\d+", u):
        return True
    # Прямая ссылка на канал вида t.me/channelname без start= и не функциональная
    if re.match(r"https?://(?:t\.me|telegram\.me)/[a-z0-9_]+/?$", u) and "start=" not in u:
        return True
    return False


def _text_has_subscription_call(text: Optional[str]) -> bool:
    """В тексте есть призыв к подписке (ОП)."""
    if not text:
        return False
    lower = text.lower().strip()
    for kw in SUBSCRIPTION_KEYWORDS:
        if kw in lower:
            return True
    return False


def _all_button_links_are_external_channels(buttons: list, bot_username: Optional[str]) -> bool:
    """
    Все инлайн-кнопки со ссылками ведут на внешние каналы (не функциональные).
    Кнопки без url / с callback не считаются «внешними».
    """
    if not buttons:
        return True
    for btn in buttons:
        url = btn.get("url") or btn.get("url_link")
        if not url:
            continue  # callback или просто кнопка без ссылки
        if _is_functional_link(str(url), bot_username):
            return False  # хотя бы одна функциональная — не «только каналы»
        if not _is_external_channel_link(str(url)):
            return False  # не канал и не функциональная — не считаем «все каналы»
    # Все ссылки в кнопках — внешние каналы (или ссылок нет)
    return True


def analyze_message(
    text: Optional[str],
    buttons: list,
    bot_username: Optional[str] = None,
) -> Tuple[bool, str]:
    """
    Определяет, сохранять ли сообщение в дерево (белый список) или пропустить (мусор).
    Возвращает (keep, reason): keep=True — сохраняем, reason — причина для Debug-лога.
    """
    buttons = buttons or []
    text_lower = (text or "").lower().strip()

    # 1. Приоритет Callback: хотя бы одна кнопка с callback_data — обязательно сохраняем
    if _has_callback_buttons(buttons):
        return True, "Callback-кнопки"

    # 2. Универсальная навигация (стрелки, пагинация, Назад/Menu и т.д.) — всегда валидно
    if _has_navigation_buttons(buttons):
        return True, "Навигация"

    # 3. Есть только функциональные ссылки (целевой бот?start=, addemoji, addstickers, share и т.д.) — сохраняем
    has_any_url = any((btn.get("url") or btn.get("url_link")) for btn in buttons)
    if has_any_url:
        all_functional = all(
            _is_functional_link(str(btn.get("url") or btn.get("url_link") or ""), bot_username)
            for btn in buttons
            if btn.get("url") or btn.get("url_link")
        )
        if all_functional:
            return True, "Функциональные ссылки"
        # 3.1. Все кнопки ведут на сторонние боты/каналы (не целевой бот) — реклама, пропускаем
        all_non_functional = all(
            not _is_functional_link(str(btn.get("url") or btn.get("url_link") or ""), bot_username)
            for btn in buttons
            if btn.get("url") or btn.get("url_link")
        )
        if all_non_functional:
            return False, "Ссылки на сторонние боты/каналы (реклама)"

    # 4. В тексте есть ссылка на сторонний бот/канал (не целевой) и нет функциональных кнопок — реклама
    if text:
        for m in re.finditer(r"https?://(?:t\.me|telegram\.me)/[^\s\)\]\>]+", text, re.IGNORECASE):
            link = m.group(0).split(")")[0].split("]")[0]  # обрезать до первой скобки из markdown
            if not _is_functional_link(link, bot_username) and re.search(r"t\.me/[\w]+", link, re.IGNORECASE):
                # В тексте ссылка на другого бота/канал; кнопок с callback/навигации нет (уже проверено)
                return False, "В тексте ссылка на сторонний бот/канал (реклама)"

    # 5. Интеллектуальный фильтр мусора: ОП-заглушка только если ВСЕ условия:
    #    — в тексте призыв к подписке
    #    — нет ни одной callback-кнопки (уже проверено выше)
    #    — все инлайн-кнопки ведут на внешние каналы (joinchat, t.me/+)
    if _text_has_subscription_call(text):
        if _all_button_links_are_external_channels(buttons, bot_username):
            return False, "Forced Channel Subscription (ОП: призыв и только ссылки на каналы)"

    # 6. Остальное — меню/сообщение бота, сохраняем для полноты клона
    return True, "Меню с кнопками / сообщение бота"


def format_buttons(buttons) -> list:
    """Извлекает текст, callback_data и url из кнопок (для инлайна и обычных)."""
    if not buttons:
        return []
    result = []
    for row in buttons:
        # Telethon: row может быть list кнопок или объект с .buttons
        for btn in (row if isinstance(row, (list, tuple)) else getattr(row, "buttons", [row])):
            text = getattr(btn, "text", None) or (btn if isinstance(btn, str) else str(btn))
            item = {"text": text}
            data = getattr(btn, "data", None)
            if data is not None:
                if hasattr(data, "decode"):
                    data = data.decode("utf-8", errors="replace")
                item["callback_data"] = data
            url = getattr(btn, "url", None)
            if url is not None:
                item["url"] = url
            result.append(item)
    return result


def compute_state_hash(text: str, buttons: list) -> str:
    """Уникальный хеш состояния по тексту и кнопкам (для дедупликации и проверки циклов)."""
    payload = (text or "") + json.dumps(buttons or [], sort_keys=True, ensure_ascii=False)
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def get_message_text_and_media(message) -> tuple:
    """Извлекает текст (или подпись к медиа) и тип медиа из сообщения."""
    text = getattr(message, "text", None) or getattr(message, "message", None) or ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    text = text or ""
    media_type = None
    if getattr(message, "photo", None):
        media_type = "photo"
    elif getattr(message, "document", None):
        media_type = "document"
    elif getattr(message, "video", None):
        media_type = "video"
    elif getattr(message, "audio", None):
        media_type = "audio"
    elif getattr(message, "voice", None):
        media_type = "voice"
    return text, media_type


class StateNode:
    """Узел дерева состояний: текст + кнопки + медиа + local_media_path для загруженных файлов."""

    __slots__ = (
        "id", "text", "buttons", "message_id", "children", "created_at",
        "is_inline_response", "media_type", "state_hash", "parent_id", "last_seen", "local_media_path"
    )

    def __init__(self, text: str = "", buttons: list = None, message_id: int = None):
        self.id = str(uuid.uuid4())
        self.text = text or ""
        self.buttons = buttons or []
        self.message_id = message_id
        self.children = {}
        self.created_at = datetime.now().strftime("%H:%M:%S")
        self.is_inline_response = False
        self.media_type = None
        self.state_hash = compute_state_hash(self.text, self.buttons)
        self.parent_id = None
        self.last_seen = self.created_at
        self.local_media_path = None  # относительный путь к скачанному файлу (downloads/bot/msg_123.jpg)

    def to_dict(self):
        return {
            "id": self.id,
            "state_hash": self.state_hash,
            "text": self.text,
            "buttons": self.buttons,
            "message_id": self.message_id,
            "media_type": self.media_type,
            "local_media_path": getattr(self, "local_media_path", None),
            "created_at": self.created_at,
            "last_seen": getattr(self, "last_seen", None),
            "children": {
                trigger: node.to_dict() for trigger, node in self.children.items()
            },
        }


class StateTree:
    """
    Древовидная структура переходов бота с дедупликацией по хешу состояния.
    Один и тот же контент (текст + кнопки) не создаёт бесконечную вложенность.
    """

    def __init__(self):
        self.root = StateNode(text="[ROOT]")
        self.root.state_hash = compute_state_hash(self.root.text, self.root.buttons)
        self.states_by_id = {self.root.id: self.root}
        self.states_by_hash = {self.root.state_hash: self.root.id}
        self.states_by_message_id = {}
        self.current_state_id = self.root.id
        self.pending_trigger = None
        self.pending_edit_message_id = None
        self.inline_sequence = 0

    def get_ancestor_chain(self, state_id: str) -> list:
        """Цепочка id от узла до корня (включая сам узел и root)."""
        chain = []
        node = self.states_by_id.get(state_id)
        while node:
            chain.append(node.id)
            node = self.states_by_id.get(node.parent_id) if node.parent_id else None
        return chain

    def resolve_state(
        self,
        parent: StateNode,
        trigger: str,
        text: str,
        buttons: list,
        media_type: Optional[str],
        message_id: Optional[int],
        is_inline_response: bool = False,
        local_media_path: Optional[str] = None,
    ) -> Tuple[StateNode, bool]:
        """
        Возвращает узел для перехода (parent --[trigger]--> node) и флаг added_edge.
        - Если состояние с таким хешем уже в цепочке предков или равно текущему — не создаём новую ветку, возвращаем (node, False).
        - Если состояние с таким хешем есть в дереве — переиспользуем узел, добавляем ребро parent -> node, возвращаем (node, True).
        - Иначе создаём новый узел, возвращаем (node, True).
        """
        if not trigger or not trigger.strip():
            trigger = "_empty_"

        state_hash = compute_state_hash(text, buttons)
        now = datetime.now().strftime("%H:%M:%S")
        chain = self.get_ancestor_chain(self.current_state_id)

        # Состояние уже есть в дереве
        existing_id = self.states_by_hash.get(state_hash)
        if existing_id is not None:
            existing = self.states_by_id.get(existing_id)
            if existing is None:
                del self.states_by_hash[state_hash]
            else:
                # Текущий узел или предок — цикл/повтор: не добавляем новый уровень
                if existing.id == self.current_state_id or existing.id in chain:
                    existing.last_seen = now
                    if local_media_path is not None:
                        existing.local_media_path = local_media_path
                    self.current_state_id = existing.id
                    return existing, False
                # Узел из другой ветки — переиспользуем, вешаем ребро parent -> existing
                parent.children[trigger] = existing
                existing.text = text
                existing.buttons = buttons
                existing.media_type = media_type
                existing.message_id = message_id
                existing.state_hash = state_hash
                existing.last_seen = now
                existing.is_inline_response = is_inline_response
                existing.local_media_path = local_media_path
                if message_id is not None:
                    self.states_by_message_id[message_id] = existing.id
                self.current_state_id = existing.id
                return existing, True

        # Новый узел
        child = StateNode(text=text, buttons=buttons, message_id=message_id)
        child.media_type = media_type
        child.state_hash = state_hash
        child.parent_id = parent.id
        child.last_seen = now
        child.is_inline_response = is_inline_response
        child.local_media_path = local_media_path

        self.states_by_id[child.id] = child
        self.states_by_hash[state_hash] = child.id
        parent.children[trigger] = child
        if message_id is not None:
            self.states_by_message_id[message_id] = child.id
        self.current_state_id = child.id
        return child, True

    def register_message_id(self, state: StateNode, message_id: int):
        if message_id is not None:
            self.states_by_message_id[message_id] = state.id

    def find_state_by_message_id(self, message_id: int) -> Optional[StateNode]:
        sid = self.states_by_message_id.get(message_id)
        return self.states_by_id.get(sid) if sid else None

    def to_dict(self):
        return self.root.to_dict()


class BotCloner:
    """Клонер бота: строит State Tree по диалогу с ботом (Telethon)."""

    def __init__(self, client: TelegramClient, bot_username: str):
        self.client = client
        self.bot = bot_username
        self.bot_id = None
        self.bot_entity = None
        self.tree = StateTree()
        self._log = []
        self._messages_from_bot_count = 0
        self._debug_events_shown = 0
        # Имя текущего пользователя (записывающего сессию) — подменяется на {user_name} в дереве
        self._user_first_name = None
        # Папка для медиа (downloads/[bot_username]/) создаётся в get_bot_entity()
        self._downloads_dir = None

    async def get_bot_entity(self):
        try:
            entity = await self.client.get_entity(self.bot)
            self.bot_entity = entity
            self.bot_id = entity.id
            me = await self.client.get_me()
            self._user_first_name = (getattr(me, "first_name", None) or "").strip() or None
            # Папка для медиа: downloads/[username_бота]/
            bot_clean = (self.bot or "bot").strip().replace("@", "")
            self._downloads_dir = os.path.join("downloads", bot_clean)
            os.makedirs(self._downloads_dir, exist_ok=True)
            print(f"\n[CLONER] Запись для бота: @{self.bot} (ID: {self.bot_id})")
            print(f"[!] Медиафайлы: {self._downloads_dir}/")
            if self._user_first_name:
                print(f"[!] Имя пользователя «{self._user_first_name}» будет сохраняться как {{user_name}} в дереве.")
            print("[!] Пиши боту и жми кнопки в Telegram — всё будет выводиться сюда.")
            print("[!] Когда закончишь обход бота — введи в ЭТУ консоль:  stop  и нажми Enter.")
            print("[!] Сохранение произойдёт в любой момент (можно не ждать «окончания» — запись идёт в реальном времени).")
        except Exception as e:
            print(f"[!] Ошибка: не удалось найти бота @{self.bot}. {e}")
            os._exit(1)

    async def _download_media_from_message(self, message) -> Optional[str]:
        """
        Скачивает медиа из сообщения (photo, video, document) в downloads/[bot]/.
        Имя файла: msg_{message_id}.{ext}. Возвращает относительный путь или None.
        Не качает повторно, если файл уже есть.
        """
        if not message or not getattr(message, "media", None):
            return None
        msg_id = getattr(message, "id", None) or 0
        media = message.media
        ext = ".jpg"
        kind = "photo"
        if getattr(message, "photo", None):
            ext = ".jpg"
            kind = "Фото"
        elif getattr(message, "video", None):
            ext = ".mp4"
            kind = "Видео"
        elif getattr(message, "document", None):
            doc = message.document
            if getattr(doc, "attributes", None):
                for a in doc.attributes:
                    if type(a).__name__ == "DocumentAttributeFilename" and getattr(a, "file_name", None):
                        name = a.file_name
                        if "." in name:
                            ext = "." + name.rsplit(".", 1)[-1].lower()
                        break
            if ext == ".jpg":
                ext = ".bin"
            kind = "Документ"
        else:
            return None

        rel_dir = os.path.join("downloads", (self.bot or "bot").strip().replace("@", ""))
        filename = f"msg_{msg_id}{ext}"
        rel_path = os.path.join(rel_dir, filename).replace("\\", "/")
        full_path = os.path.join(self._downloads_dir, filename)

        if os.path.isfile(full_path):
            print(f"[MEDIA] Файл уже есть: {rel_path}")
            return rel_path
        try:
            path = await self.client.download_media(media, file=full_path)
            if path:
                print(f"[MEDIA] {kind} успешно скачано: {rel_path}")
                return rel_path
        except Exception as e:
            print(f"[MEDIA] Ошибка загрузки: {e}")
        return None

    def _normalize_trigger(self, raw: str) -> str:
        t = (raw or "").strip()
        return t if t else "_empty_"

    def _print_recorded(self, kind: str, trigger: str, text: str, buttons: list, media_type: str = None, local_media_path: str = None, is_repeat: bool = False):
        """Печатает в терминал всё, что записано в дерево (текст, медиа, кнопки). При is_repeat — только короткую строку. flush=True — сразу в консоль."""
        _flush = True
        ts = datetime.now().strftime("%H:%M:%S")
        if is_repeat:
            print(f"[{ts}] Повтор (уже в дереве), триггер: {trigger!r}", flush=_flush)
            return
        print(f"\n{'='*60}", flush=_flush)
        print(f"[{ts}] ЗАПИСАНО: {kind} | триггер: {trigger!r}", flush=_flush)
        if media_type:
            print(f"  МЕДИА: [{media_type}]", flush=_flush)
        if local_media_path:
            print(f"  ФАЙЛ: {local_media_path}", flush=_flush)
        print("-" * 60, flush=_flush)
        print(text or "(нет текста)", flush=_flush)
        if buttons:
            print("-" * 60, flush=_flush)
            for i, b in enumerate(buttons):
                line = f"  [{i+1}] текст: {b.get('text') or '(нет)'}"
                if b.get("callback_data"):
                    line += f"  | callback_data: {b['callback_data']!r}"
                if b.get("url"):
                    line += f"  | url: {b['url']}"
                print(line, flush=_flush)
        print("=" * 60, flush=_flush)

    def _is_bot_chat(self, event) -> bool:
        """Проверка, что событие из чата с нашим ботом: chat_id или peer чата = bot_id."""
        cid = getattr(event, "chat_id", None)
        if cid is not None and cid == self.bot_id:
            return True
        # В личке chat_id может быть id пользователя; проверяем peer сообщения
        msg = getattr(event, "message", None)
        if msg is not None:
            pid = getattr(msg, "peer_id", None)
            if pid is not None and getattr(pid, "user_id", None) == self.bot_id:
                return True
        pid = getattr(event, "peer_id", None)
        if pid is not None and getattr(pid, "user_id", None) == self.bot_id:
            return True
        return False

    def _apply_username_placeholder(self, text: str, buttons: list) -> Tuple[str, list]:
        """
        Подмена в сохраняемых данных:
        - Юзернейм копируемого бота → {bot_username} (в тексте, url, callback_data).
        - Имя текущего пользователя (first_name) → {user_name} (в тексте и в подписях кнопок),
          чтобы в клоне подставлять имя того, кто пишет боту.
        """
        source_bot_username = (self.bot or "").strip().replace("@", "")
        new_text = text or ""
        new_buttons = [dict(btn) for btn in (buttons or [])]

        # 1. Юзернейм бота → {bot_username}
        if source_bot_username:
            if new_text and source_bot_username.lower() in new_text.lower():
                new_text = re.sub(re.escape(source_bot_username), "{bot_username}", new_text, flags=re.IGNORECASE)
                new_text = re.sub(re.escape("@" + source_bot_username), "@{bot_username}", new_text, flags=re.IGNORECASE)
                print("[REPLACE] Username detected in text, changed to {bot_username}.")
            for b in new_buttons:
                url = b.get("url") or b.get("url_link")
                if url:
                    url_str = str(url)
                    if source_bot_username.lower() in url_str.lower():
                        b["url"] = re.sub(re.escape(source_bot_username), "{bot_username}", url_str, flags=re.IGNORECASE)
                        if b.get("url_link"):
                            b["url_link"] = b["url"]
                        print("[REPLACE] Username detected in URL, changed to {bot_username}.")
                cd = b.get("callback_data")
                if cd is not None and source_bot_username.lower() in str(cd).lower():
                    b["callback_data"] = re.sub(re.escape(source_bot_username), "{bot_username}", str(cd), flags=re.IGNORECASE)
                    print("[REPLACE] Username detected in callback_data, changed to {bot_username}.")

        # 2. Имя пользователя (first_name) → {user_name} — в дереве шаблон, в клоне подставлять имя того, кто пишет
        user_first_name = getattr(self, "_user_first_name", None) or ""
        if user_first_name and len(user_first_name) > 1:
            if new_text and user_first_name.lower() in new_text.lower():
                new_text = re.sub(re.escape(user_first_name), "{user_name}", new_text, flags=re.IGNORECASE)
                print("[REPLACE] User first name in text, changed to {user_name}.")
            for b in new_buttons:
                label = b.get("text")
                if label and isinstance(label, str) and user_first_name.lower() in label.lower():
                    b["text"] = re.sub(re.escape(user_first_name), "{user_name}", label, flags=re.IGNORECASE)
                    print("[REPLACE] User first name in button text, changed to {user_name}.")

        return new_text, new_buttons

    async def start(self):
        await self.get_bot_entity()
        # Вывод в консоль сразу, без буфера (чтобы «ЗАПИСАНО» появлялось без задержки)
        if hasattr(sys.stdout, "reconfigure"):
            try:
                sys.stdout.reconfigure(line_buffering=True)
            except Exception:
                pass

        # ----- Новые сообщения: и по entity, и по chat_id (на случай проблем с фильтром) -----
        @self.client.on(events.NewMessage(outgoing=True))
        async def on_outgoing(event):
            if not self._is_bot_chat(event):
                return
            if self._debug_events_shown < 5:
                self._debug_events_shown += 1
                print(f"[DEBUG] Исходящее в чат бота msg_id={getattr(event.message, 'id', '?')}")
            text = (event.message.text or "").strip()
            trigger = self._normalize_trigger(text)
            self.tree.pending_trigger = trigger
            self._log.append({"time": datetime.now().strftime("%H:%M:%S"), "from": "USER", "text": text})
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"\n{'='*60}\n[{ts}] ВЫ -> БОТ (триггер): {text!r}\n{'='*60}")

        @self.client.on(events.NewMessage(incoming=True))
        async def on_incoming(event):
            # Обрабатываем только сообщения, отправленные ботом (в личке chat_id может быть id пользователя)
            if getattr(event, "sender_id", None) != self.bot_id:
                return
            ts0 = datetime.now().strftime("%H:%M:%S")
            print(f"[{ts0}] Получено сообщение от бота, обрабатываю...", flush=True)
            if self._debug_events_shown < 5:
                self._debug_events_shown += 1
                print(f"[DEBUG] Входящее от бота msg_id={getattr(event.message, 'id', '?')}")
            try:
                text, media_type = get_message_text_and_media(event.message)
                buttons = format_buttons(event.message.buttons)

                keep, reason = analyze_message(text, buttons, self.bot)
                if not keep:
                    self._log.append({"time": datetime.now().strftime("%H:%M:%S"), "from": "BOT", "junk": True, "text": text[:80]})
                    ts = datetime.now().strftime("%H:%M:%S")
                    print(f"\n[SKIP] Мусорное сообщение (Reason: {reason})", flush=True)
                    print(f"  Текст: {text[:200] or '(нет)'}", flush=True)
                    if buttons:
                        for b in buttons:
                            print(f"  Кнопка: {b.get('text')} | url: {b.get('url')}")
                    return
                self._messages_from_bot_count += 1

                parent = self.tree.states_by_id.get(self.tree.current_state_id) or self.tree.root
                if self.tree.pending_trigger:
                    trigger = self.tree.pending_trigger
                    self.tree.pending_trigger = None
                else:
                    trigger = "_inline_" + str(self.tree.inline_sequence)
                    self.tree.inline_sequence += 1

                ts = datetime.now().strftime("%H:%M:%S")
                print(f"[KEEP] Состояние сохранено (Trigger: {trigger!r}, Reason: {reason})", flush=True)
                text, buttons = self._apply_username_placeholder(text, buttons)
                # Сначала записываем в дерево и выводим «ЗАПИСАНО» — без ожидания загрузки медиа
                node, added = self.tree.resolve_state(
                    parent, trigger, text, buttons, media_type, event.message.id, is_inline_response=False,
                    local_media_path=None,
                )
                if not added:
                    ts = datetime.now().strftime("%H:%M:%S")
                    print(f"\n[{ts}] Повторное состояние (хеш совпал с предком), ветка не создана. Триггер: {trigger!r}", flush=True)
                self._log.append({"time": datetime.now().strftime("%H:%M:%S"), "from": "BOT", "text": text[:80], "buttons": buttons})
                self._print_recorded("БОТ (новое сообщение)", trigger, text, buttons, media_type, None, is_repeat=not added)
                # Медиа качаем в фоне только если в сообщении есть фото/видео/документ
                if node and media_type:
                    msg = event.message
                    async def _download_then_set_path(n, m):
                        path = await self._download_media_from_message(m)
                        if path and n:
                            n.local_media_path = path
                    asyncio.create_task(_download_then_set_path(node, msg))
            except Exception as e:
                print(f"[!] Ошибка при обработке входящего: {e}")
                import traceback
                traceback.print_exc()

        # ----- CallbackQuery: нажатие инлайн-кнопки → запоминаем триггер и ждём edit_message -----
        @self.client.on(events.CallbackQuery())
        async def on_callback(event: events.CallbackQuery.Event):
            if event.message and getattr(event.message.peer_id, "user_id", None) != self.bot_id:
                return
            try:
                data = event.data
                if hasattr(data, "decode"):
                    callback_data = data.decode("utf-8", errors="replace")
                else:
                    callback_data = str(data)

                msg_id = getattr(event, "message_id", None) or (event.message.id if event.message else None)
                if msg_id is not None:
                    state = self.tree.find_state_by_message_id(msg_id)
                    if state:
                        self.tree.current_state_id = state.id
                    # Ожидание правки: следующее MessageEdited с этим message_id = результат нажатия кнопки
                    self.tree.pending_edit_message_id = msg_id

                self.tree.pending_trigger = self._normalize_trigger(callback_data)
                self._log.append({
                "time": datetime.now().strftime("%H:%M:%S"),
                "from": "USER",
                    "action": "callback",
                    "callback_data": callback_data,
                })
                ts = datetime.now().strftime("%H:%M:%S")
                print(f"\n{'='*60}\n[{ts}] ИНЛАЙН-КНОПКА (триггер): {callback_data!r} → ждём edit_message (msg_id={msg_id})\n{'='*60}")
            except Exception as e:
                print(f"[!] Ошибка в CallbackQuery: {e}")

        # ----- MessageEdited: навигация через edit — записываем [Кнопка/Callback] -> [Изменённое сообщение] -----
        @self.client.on(events.MessageEdited())
        async def on_edited(event):
            # Только правки, сделанные ботом (в личке sender_id может быть единственная надёжная проверка)
            sender_id = getattr(event, "sender_id", None)
            if sender_id != self.bot_id and not (sender_id is None and self._is_bot_chat(event)):
                return
            if self._debug_events_shown < 8:
                self._debug_events_shown += 1
                print(f"[DEBUG] MessageEdited от бота msg_id={getattr(event.message, 'id', '?')}")
            ts0 = datetime.now().strftime("%H:%M:%S")
            print(f"[{ts0}] Получена правка сообщения, записываю...", flush=True)
            try:
                text, media_type = get_message_text_and_media(event.message)
                buttons = format_buttons(event.message.buttons)
                msg_id = event.message.id
            except Exception as e:
                print(f"[!] Ошибка чтения MessageEdited: {e}")
                return

            try:
                # Ожидание правки: мы нажали кнопку и ждём именно эту правку — всегда записываем (не фильтруем как мусор)
                waiting_this_edit = (
                    self.tree.pending_trigger is not None
                    and self.tree.pending_edit_message_id is not None
                    and msg_id == self.tree.pending_edit_message_id
                )
                state = self.tree.find_state_by_message_id(msg_id)
                # Не фильтруем правку, если это ответ на кнопку (waiting_this_edit) или обновление уже известного сообщения (state)
                is_edit_of_known_message = waiting_this_edit or (state is not None)
                keep, reason = analyze_message(text, buttons, self.bot)
                if not is_edit_of_known_message and not keep:
                    ts = datetime.now().strftime("%H:%M:%S")
                    print(f"\n[SKIP] Мусорное сообщение при редакт (Reason: {reason})", flush=True)
                    print(f"  Текст: {text[:200] or '(нет)'}", flush=True)
                    return
                if not is_edit_of_known_message:
                    print(f"[KEEP] При редакт сохранено (Reason: {reason})", flush=True)
                text, buttons = self._apply_username_placeholder(text, buttons)

                parent = self.tree.states_by_id.get(self.tree.current_state_id) or self.tree.root
                node_to_update = None  # после вывода «ЗАПИСАНО» качаем медиа и обновляем этот узел

                if waiting_this_edit and parent:
                    trigger_used = self.tree.pending_trigger
                    node, added = self.tree.resolve_state(
                        parent, trigger_used, text, buttons, media_type, msg_id, is_inline_response=True,
                        local_media_path=None,
                    )
                    node_to_update = node
                    self.tree.pending_trigger = None
                    self.tree.pending_edit_message_id = None
                    if not added:
                        ts = datetime.now().strftime("%H:%M:%S")
                        print(f"\n[{ts}] То же меню (хеш совпал), новый уровень не создан. Триггер: {trigger_used!r}", flush=True)
                    self._print_recorded(
                        "[Кнопка/Callback] → [Изменённое сообщение]",
                        trigger_used, text, buttons, media_type, None, is_repeat=not added
                    )
                elif state:
                    # Уже есть узел с этим message_id: если это ответ на клик (is_inline_response), обновляем по месту
                    if getattr(state, "is_inline_response", False):
                        old_hash = state.state_hash
                        state.text = text
                        state.buttons = buttons
                        state.media_type = media_type
                        state.local_media_path = None
                        state.state_hash = compute_state_hash(text, buttons)
                        state.last_seen = datetime.now().strftime("%H:%M:%S")
                        if old_hash in self.tree.states_by_hash and self.tree.states_by_hash[old_hash] == state.id:
                            del self.tree.states_by_hash[old_hash]
                        self.tree.states_by_hash[state.state_hash] = state.id
                        self.tree.current_state_id = state.id
                        node_to_update = state
                        self._print_recorded("БОТ ОБНОВИЛ (тот же узел)", "_inline_", text, buttons, media_type, None)
                    else:
                        node, added = self.tree.resolve_state(
                            state, "_inline_", text, buttons, media_type, msg_id, is_inline_response=True,
                            local_media_path=None,
                        )
                        node_to_update = node
                        if not added:
                            ts = datetime.now().strftime("%H:%M:%S")
                            print(f"\n[{ts}] То же состояние (цикл), уровень не создан.", flush=True)
                        self._print_recorded("БОТ ОБНОВИЛ (новый узел _inline_)", "_inline_", text, buttons, media_type, None, is_repeat=not added)
                else:
                    trigger = self.tree.pending_trigger or "_inline_" + str(self.tree.inline_sequence)
                    if not self.tree.pending_trigger:
                        self.tree.inline_sequence += 1
                    self.tree.pending_trigger = None
                    self.tree.pending_edit_message_id = None
                    node, added = self.tree.resolve_state(
                        parent, trigger, text, buttons, media_type, msg_id, is_inline_response=False,
                        local_media_path=None,
                    )
                    node_to_update = node
                    if not added:
                        ts = datetime.now().strftime("%H:%M:%S")
                        print(f"\n[{ts}] Повторное состояние при редакт, ветка не создана. Триггер: {trigger!r}", flush=True)
                    self._print_recorded("БОТ ОБНОВИЛ (новый узел)", trigger, text, buttons, media_type, None, is_repeat=not added)

                # Медиа качаем в фоне только если в сообщении есть фото/видео/документ
                if node_to_update and media_type:
                    msg_ed = event.message
                    node_ref = node_to_update
                    async def _download_then_set_path_edit(n, m):
                        path = await self._download_media_from_message(m)
                        if path and n:
                            n.local_media_path = path
                    asyncio.create_task(_download_then_set_path_edit(node_ref, msg_ed))
            except Exception as exc:
                print(f"[!] Ошибка при обработке MessageEdited: {exc}")
                import traceback
                traceback.print_exc()

        async def check_console():
            while True:
                user_input = await asyncio.to_thread(input, "")
                if user_input.lower().strip() in ("stop", "exit", "quit", "стоп"):
                    print("\n[!] Завершаем сеанс и сохраняем дерево...")
                    await self.client.disconnect()
                    break

        print("\n--- ЗАПИСЬ ИДЁТ (введи stop для выхода и сохранения) ---\n")

        try:
            await asyncio.gather(
                self.client.run_until_disconnected(),
                check_console(),
            )
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            self._save_report()

    def _save_report(self):
        timestamp = datetime.now().strftime("%d%m_%H%M")
        filename_json = f"state_tree_{self.bot}_{timestamp}.json"
        filename_md = f"state_tree_{self.bot}_{timestamp}.md"

        data = self.tree.to_dict()
        with open(filename_json, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        with open(filename_md, "w", encoding="utf-8") as f:
            f.write(f"# Дерево состояний @{self.bot}\n\n")
            self._write_tree_md(f, data, level=0)

        print("\n[ГОТОВО]")
        print(f"1. Дерево (JSON): {filename_json}")
        print(f"2. Дерево (Markdown): {filename_md}")

    def _write_tree_md(self, f, node: dict, level: int):
        indent = "  " * level
        text = (node.get("text") or "")[:200]
        buttons = node.get("buttons") or []
        children = node.get("children") or {}
        media = node.get("media_type")
        local_path = node.get("local_media_path")
        f.write(f"{indent}- **Текст:** {text}\n")
        if media:
            f.write(f"{indent}  **Медиа:** [{media}]\n")
        if local_path:
            f.write(f"{indent}  **Файл:** `{local_path}`\n")
        if buttons:
            for b in buttons:
                t = b.get("text") or b.get("callback_data") or b.get("url") or ""
                f.write(f"{indent}  Кнопки: `{t}`")
                if b.get("callback_data"):
                    f.write(f" (callback: `{b['callback_data']}`)")
                f.write("\n")
        for trigger, child in children.items():
            f.write(f"\n{indent}**Триггер:** `{trigger}`\n")
            self._write_tree_md(f, child, level + 1)


# --- ЗАПУСК ---
client = TelegramClient("userbot_session", API_ID, API_HASH)


async def main():
    bot_input = input("Введите @username бота: ").strip().replace("@", "")
    if bot_input.lower() in ("exit", "stop"):
        return
    cloner = BotCloner(client, bot_input)
    await cloner.start()


if __name__ == "__main__":
    with client:
        client.loop.run_until_complete(main())
