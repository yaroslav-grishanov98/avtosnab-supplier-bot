"""
Тестовый скрипт для имитации вызова фронта.
Создаёт привязку Telegram-пользователя к поставщику через прямой эндпоинт.

Использование:
    poetry run python test_direct_register.py

Перед запуском убедись, что бэкенд запущен.
"""

import sys

import requests

# ---------- НАСТРОЙКИ ----------
BACKEND_URL = "https://avtosnab.testant.online/api/v1/bot-user-bindings/direct-register"

# ---------- ВВОД ДАННЫХ ----------
print("=" * 60)
print("  ТЕСТ: Прямая регистрация привязки Telegram ↔ Поставщик")
print("=" * 60)

telegram_user_id = input("\nТвой Telegram user ID: ").strip()
chat_id = input("Твой Telegram chat ID (обычно совпадает с user ID, нажми Enter): ").strip()
supplier_id = input("UUID компании (supplier_id): ").strip()
first_name = input("Имя из Telegram (Enter — пропустить): ").strip()
last_name = input("Фамилия из Telegram (Enter — пропустить): ").strip()
username = input("Username из Telegram (Enter — пропустить): ").strip()

if not chat_id:
    chat_id = telegram_user_id

if not telegram_user_id or not supplier_id:
    print("\n❌ Ошибка: telegram_user_id и supplier_id обязательны!")
    sys.exit(1)

# ---------- ОТПРАВКА ЗАПРОСА ----------
payload = {
    "platform": "telegram",
    "telegram_user_id": telegram_user_id,
    "chat_id": chat_id,
    "supplier_id": supplier_id,
}

if first_name:
    payload["first_name"] = first_name
if last_name:
    payload["last_name"] = last_name
if username:
    payload["username"] = username

print(f"\n📤 Отправляю запрос на {BACKEND_URL}")
print(f"   Данные: {payload}")

try:
    response = requests.post(
        BACKEND_URL,
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=10,
    )

    print(f"\n📥 Ответ: HTTP {response.status_code}")
    print(f"   Тело ответа: {response.json()}")

    if response.status_code in (200, 201):
        print("\n✅ Привязка создана успешно!")
        print("   Теперь нажми /start в боте — он должен тебя узнать.")
    else:
        print(f"\n❌ Ошибка: {response.status_code}")
        print(f"   {response.json().get('detail', 'Неизвестная ошибка')}")

except requests.exceptions.ConnectionError:
    print("\n❌ Не удалось подключиться к бэкенду.")
    print("   Убедись, что бэкенд запущен и доступен по адресу:")
    print(f"   {BACKEND_URL}")
except Exception as e:
    print(f"\n❌ Ошибка: {e}")

print("\n" + "=" * 60)
