#!/usr/bin/env python3
"""Тест загрузки в Яндекс.Диск через прямой API с обработкой ошибок"""
import os
import sys
import requests
import tempfile
import time
from dotenv import load_dotenv

load_dotenv()

token = os.getenv('YANDEX_DISK_TOKEN')
folder = os.getenv('YANDEX_DISK_FOLDER', 'app:/bot-screenshots')

if not token:
    print("❌ YANDEX_DISK_TOKEN не указан в .env")
    sys.exit(1)

print(f"🔑 Токен: {token[:20]}... (длина: {len(token)})")
print(f"📁 Папка: {folder}")

def safe_json_response(response, description="ответ"):
    """Безопасное получение JSON с детальной диагностикой"""
    if response.status_code != 200:
        print(f"⚠️ {description}: статус {response.status_code}")
        print(f"   Headers: {dict(response.headers)}")
        print(f"   Body (first 200 chars): {response.text[:200]!r}")
        return None
    try:
        return response.json()
    except requests.exceptions.JSONDecodeError as e:
        print(f"⚠️ {description}: невалидный JSON")
        print(f"   Status: {response.status_code}")
        print(f"   Content-Type: {response.headers.get('Content-Type')}")
        print(f"   Body: {response.text[:200]!r}")
        return None

# === Проверка прав токена ===
print("\n🔐 Проверка прав токена...")
try:
    info_resp = requests.get(
        'https://oauth.yandex.ru/info',
        headers={'Authorization': f'OAuth {token}'},
        timeout=15,
        allow_redirects=False  # важно: не следовать редиректам
    )
    
    print(f"   Статус: {info_resp.status_code}")
    print(f"   Content-Type: {info_resp.headers.get('Content-Type')}")
    
    if info_resp.status_code == 401:
        print("❌ Токен невалиден или истёк (401)")
        print("💡 Получите новый токен: https://oauth.yandex.ru/authorize?response_type=token&client_id=ВАШ_ID&scope=disk")
        sys.exit(1)
    elif info_resp.status_code == 403:
        print("❌ Доступ запрещён (403) — возможно, токен отозван")
        sys.exit(1)
    
    info_data = safe_json_response(info_resp, "Информация о токене")
    if info_data:
        scopes = info_data.get('scopes', [])
        print(f"✅ Выданные права: {', '.join(scopes) if scopes else 'не указаны'}")
        if 'disk' not in scopes and 'disk:write' not in scopes:
            print("⚠️ ВНИМАНИЕ: токен не имеет прав на запись!")
            print("💡 Получите новый токен с параметром &scope=disk")
    else:
        print("⚠️ Не удалось распарсить ответ /info")
        
except requests.exceptions.RequestException as e:
    print(f"❌ Ошибка запроса к /info: {e}")
    sys.exit(1)

# === Формирование пути ===
# app:/ — изолированная папка приложения (требует меньше прав)
if folder.startswith(('disk:/', 'app:/')):
    base_path = folder
else:
    base_path = f"disk:/{folder.lstrip('/')}"

# === Создание тестового файла ===
with tempfile.NamedTemporaryFile(mode='w', suffix='.jpg', delete=False) as f:
    f.write('FAKE_JPEG_' + str(time.time()) + '_' + 'x' * 100)  # ~150 байт
    tmp_path = f.name

remote_name = f"test_api_{int(time.time())}.jpg"
remote_path = f"{base_path.rstrip('/')}/{remote_name}"
headers = {'Authorization': f'OAuth {token}', 'Accept': 'application/json'}

try:
    # === Шаг 1: Получить URL для загрузки ===
    print(f"\n📤 Шаг 1: Запрос URL для {remote_path}")
    upload_url = 'https://cloud-api.yandex.net/v1/disk/resources/upload'
    params = {'path': remote_path, 'overwrite': 'true'}
    
    resp = requests.get(upload_url, headers=headers, params=params, timeout=30, allow_redirects=False)
    
    print(f"   Статус: {resp.status_code}")
    print(f"   Content-Type: {resp.headers.get('Content-Type')}")
    
    if resp.status_code == 401:
        print("❌ Неверный токен (401)")
        sys.exit(1)
    elif resp.status_code == 403:
        print("❌ Нет прав на запись (403)")
        print("💡 Убедитесь, что токен получен с &scope=disk в режиме инкогнито")
        print("💡 Или попробуйте YANDEX_DISK_FOLDER=app:/bot-screenshots")
        sys.exit(1)
    elif resp.status_code == 409:
        print(f"⚠️ Конфликт (409): {resp.text}")
        # Попробуем всё равно загрузить
    elif resp.status_code not in (200, 201):
        print(f"❌ Ошибка {resp.status_code}: {resp.text[:200]}")
        sys.exit(1)
    
    upload_data = safe_json_response(resp, "Получение upload URL")
    if not upload_data or 'href' not in upload_data:
        print("❌ Не получен href для загрузки")
        sys.exit(1)
    
    upload_link = upload_data['href']
    print(f"✅ Получен URL: {upload_link[:70]}...")
    
    # === Шаг 2: Загрузить файл ===
    print(f"📤 Шаг 2: Загрузка файла ({os.path.getsize(tmp_path)} байт)")
    with open(tmp_path, 'rb') as f:
        put_resp = requests.put(
            upload_link,
            data=f,
            headers={'Content-Type': 'application/octet-stream'},
            timeout=120
        )
    
    print(f"   PUT статус: {put_resp.status_code}")
    
    if put_resp.status_code in (200, 201):
        print("✅ Файл успешно загружен!")
        
        # === Шаг 3: Проверка, что файл на Диске ===
        print("🔍 Проверка наличия файла на Диске...")
        check_resp = requests.get(
            'https://cloud-api.yandex.net/v1/disk/resources',
            headers=headers,
            params={'path': remote_path},
            timeout=30
        )
        
        if check_resp.status_code == 200:
            check_data = safe_json_response(check_resp, "Информация о файле")
            if check_data:
                print(f"✅ Файл на Диске: {check_data.get('name')}")
                print(f"   Размер: {check_data.get('size')} байт")
                print(f"   Путь: {check_data.get('path')}")
        else:
            print(f"⚠️ Не удалось проверить файл (статус {check_resp.status_code})")
            print("   Но загрузка, скорее всего, прошла успешно!")
            
    elif put_resp.status_code == 403:
        print("❌ Нет прав на загрузку по полученному URL (403)")
        print("💡 Попробуйте получить токен заново с &scope=disk")
    else:
        print(f"❌ Ошибка загрузки: {put_resp.status_code} — {put_resp.text[:200]}")
        
except Exception as e:
    print(f"❌ Неожиданная ошибка: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()
    
finally:
    # Очистка локального файла
    if os.path.exists(tmp_path):
        os.unlink(tmp_path)
        print("\n🧹 Тестовый файл удалён")

print("\n" + "="*60)
print("🎉 Тест завершён!")
print("Если вы видите ✅ выше — бот готов к работе.")
print("Если есть ❌ — исправьте проблему и запустите тест снова.")
print("="*60)