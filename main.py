import asyncio
import json
import os
from datetime import datetime
import hashlib

from telethon import TelegramClient, events

#jkehtv
API_ID = 39025103
API_HASH = "31fa6df66fe3c58d5d9c5dea8aa5e151"

class BotRecorder:
    def __init__(self, client, bot_username):
        self.client = client
        self.bot = bot_username
        self.history = []
        self.bot_id = None

    async def get_bot_id(self):
        try:
            entity = await self.client.get_entity(self.bot)
            self.bot_id = entity.id
            print(f"\n[REC] Начата запись для бота: @{self.bot} (ID: {self.bot_id})")
            print("[!] Просто пользуйся ботом в своем приложении Telegram.")
            print("[!] Чтобы закончить и сохранить отчет, напиши 'stop' здесь в консоли или нажми Ctrl+C.")
        except Exception as e:
            print(f"[!] Ошибка: не удалось найти бота @{self.bot}. Проверь юзернейм.")
            os._exit(1)

    def format_buttons(self, buttons):
        if not buttons: return None
        result = []
        for row in buttons:
            for btn in row:
                result.append(btn.text)
        return result

    async def start(self):
        await self.get_bot_id()

        # Слушаем исходящие (ваши действия)
        @self.client.on(events.NewMessage(chats=self.bot_id, outgoing=True))
        async def handle_outgoing(event):
            entry = {
                "time": datetime.now().strftime("%H:%M:%S"),
                "from": "USER",
                "text": event.message.text,
                "action": "sent_message"
            }
            self.history.append(entry)
            print(f"\n[ВЫ -> БОТ]: {event.message.text}")

        # Слушаем входящие (ответы бота)
        @self.client.on(events.NewMessage(chats=self.bot_id, incoming=True))
        async def handle_incoming(event):
            btns = self.format_buttons(event.message.buttons)
            entry = {
                "time": datetime.now().strftime("%H:%M:%S"),
                "from": "BOT",
                "text": event.message.text,
                "buttons": btns,
                "action": "new_message"
            }
            self.history.append(entry)
            print(f"\n[БОТ -> ВАМ]: {event.message.text[:100]}...")
            if btns: print(f"Кнопки: {btns}")

        # Слушаем изменения сообщений (динамические кнопки)
        @self.client.on(events.MessageEdited(chats=self.bot_id))
        async def handle_edit(event):
            btns = self.format_buttons(event.message.buttons)
            entry = {
                "time": datetime.now().strftime("%H:%M:%S"),
                "from": "BOT",
                "text": event.message.text,
                "buttons": btns,
                "action": "edited_message"
            }
            self.history.append(entry)
            print(f"\n[БОТ ОБНОВИЛ КНОПКИ]: {event.message.text[:50]}...")
            if btns: print(f"Новые кнопки: {btns}")

        # Консольный ввод для остановки
        async def check_console():
            while True:
                user_input = await asyncio.to_thread(input, "")
                if user_input.lower().strip() in ['stop', 'exit', 'quit', 'стоп']:
                    print("\n[!] Завершаем сеанс и сохраняем отчет...")
                    await self.client.disconnect()
                    break

        print("\n--- ЗАПИСЬ ИДЕТ (напиши 'stop' для финиша) ---")
        try:
            await asyncio.gather(
                self.client.run_until_disconnected(),
                check_console()
            )
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            self.save_report()

    def save_report(self):
        if not self.history:
            print("[!] История пуста, отчет не создан.")
            return

        timestamp = datetime.now().strftime('%d%m_%H%M')
        filename = f"scenario_{self.bot}_{timestamp}.json"
        md_filename = f"scenario_{self.bot}_{timestamp}.md"

        with open(filename, "w", encoding="utf8") as f:
            json.dump(self.history, f, indent=2, ensure_ascii=False)
        
        with open(md_filename, "w", encoding="utf8") as f:
            f.write(f"# Сценарий @{self.bot}\n\n")
            for item in self.history:
                icon = "👤" if item['from'] == "USER" else "🤖"
                f.write(f"### {item['time']} {icon} {item['from']}\n")
                f.write(f"**Текст:** {item['text']}\n\n")
                if item.get('buttons'):
                    f.write(f"**Кнопки:** `{'`, `'.join(item['buttons'])}` \n\n")
                f.write("---\n")
                
        print(f"\n[ГОТОВО]")
        print(f"1. Технический отчет: {filename}")
        print(f"2. Дерево действий (Markdown): {md_filename}")

# --- ЗАПУСК ---
client = TelegramClient("userbot_session", API_ID, API_HASH)

async def main():
    bot_input = input("Введите @username: ").strip().replace("@", "")
    if bot_input.lower() in ['exit', 'stop']: return

    recorder = BotRecorder(client, bot_input)
    await recorder.start()

if __name__ == "__main__":
    with client:
        client.loop.run_until_complete(main())