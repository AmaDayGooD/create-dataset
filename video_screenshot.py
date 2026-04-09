#!/usr/bin/env python3
"""
Бот для периодических скриншотов видео с rtsp.ru
С динамическим интервалом: день (15 мин) / ночь (30 мин)
"""

import os
import sys
import asyncio
import logging
import base64
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from playwright.async_api import async_playwright, Page
from dotenv import load_dotenv

# Загрузка конфигурации
env_path = Path(__file__).parent / '.env'
if env_path.exists():
    load_dotenv(dotenv_path=env_path, encoding='utf-8-sig')
else:
    load_dotenv()

# Настройка логирования
log_level = os.getenv('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    level=getattr(logging, log_level),
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8', mode='a'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


class BrowserScreenshotter:
    def __init__(
        self,
        page_url: str,
        video_selector: str = "#video",
        output_dir: str = "./screenshots",
        viewport: dict = None,
    ):
        self.page_url = page_url
        self.video_selector = video_selector
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.viewport = viewport or {"width": 1280, "height": 720}
        
        # Настройки из .env
        self.day_interval = int(os.getenv('DAY_INTERVAL_SECONDS', 900))
        self.night_interval = int(os.getenv('NIGHT_INTERVAL_SECONDS', 1800))
        self.night_start = int(os.getenv('NIGHT_START_HOUR', 21))
        self.night_end = int(os.getenv('NIGHT_END_HOUR', 5))
        self.capture_max_retries = int(os.getenv('CAPTURE_MAX_RETRIES', 2))
        self.reload_every = int(os.getenv('RELOAD_EVERY_CAPTURES', 20))
        
        self.ya_token = os.getenv('YANDEX_DISK_TOKEN')
        self.ya_folder = os.getenv('YANDEX_DISK_FOLDER', '/bot-screenshots')
        self.ya_disk = None
        
        if self.ya_token:
            try:
                import yadisk
                self.ya_disk = yadisk.YaDisk(token=self.ya_token)
                logger.info("☁️ Яндекс.Диск инициализирован")
            except ImportError:
                logger.warning("⚠️ Библиотека yadisk не установлена. Выполните: pip install yadisk")
            except Exception as e:
                logger.error(f"❌ Ошибка инициализации Яндекс.Диска: {e}")
        

        logger.info(f"⚙️ Настройки: день={self.day_interval//60}мин, ночь={self.night_interval//60}мин, ночь={self.night_start:02d}:00-{self.night_end:02d}:00")

    def _file_hash(self, filepath: Path) -> str:
        """Вычисляет MD5 хеш файла"""
        with open(filepath, 'rb') as f:
            return hashlib.md5(f.read()).hexdigest()

    def _is_suspicious_screenshot(self, filepath: Path) -> bool:
        """Проверяет, не является ли скриншот подозрительным (чёрный/пустой)"""
        try:
            if filepath.stat().st_size < 5000:  # менее 5 КБ для 1280x720 JPEG — подозрительно
                return True
            return False
        except Exception:
            return False

    async def _upload_to_yadisk_async(self, local_path: str, remote_name: str):
        """Асинхронная загрузка в Яндекс.Диск через прямой API"""
        token = self.ya_token
        folder = self.ya_folder
        
        if not token:
            logger.debug("⚠️ YANDEX_DISK_TOKEN не указан, пропуск загрузки")
            return
        
        try:
            import aiohttp
            # Запускаем синхронные запросы в отдельном потоке
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                self._upload_via_api_sync,
                local_path, remote_name, token, folder
            )
            logger.info(f"☁️ Успешно загружено в Яндекс.Диск: {remote_name}")
        except ImportError:
            logger.error("❌ Установите aiohttp: pip install aiohttp")
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки в Яндекс.Диск: {type(e).__name__}: {e}")

    def _upload_via_api_sync(self, local_path: str, remote_name: str, token: str, folder: str):
        """Синхронная загрузка через прямой API Яндекс.Диска"""
        import requests
        import urllib.parse
        
        # Формируем путь: folder + filename
        # Поддерживаем форматы: /folder, disk:/folder, app:/folder
        if folder.startswith(('disk:/', 'app:/')):
            base_path = folder
        else:
            base_path = f"disk:{folder}" if folder.startswith('/') else f"disk:/{folder}"
        
        remote_path = f"{base_path.rstrip('/')}/{remote_name}"
        
        headers = {
            'Authorization': f'OAuth {token}',
            'Accept': 'application/json'
        }
        
        # Шаг 1: Получаем URL для загрузки
        upload_url = 'https://cloud-api.yandex.net/v1/disk/resources/upload'
        params = {
            'path': remote_path,
            'overwrite': 'true'
        }
        
        logger.debug(f"📤 Запрос URL загрузки: {remote_path}")
        response = requests.get(upload_url, headers=headers, params=params, timeout=30)
        
        if response.status_code == 401:
            raise Exception("Неверный токен (401)")
        elif response.status_code == 403:
            scopes_info = self._get_token_scopes(token)
            raise Exception(f"Нет прав на запись (403). Выданные права: {scopes_info}. Получите токен с &scope=disk")
        elif response.status_code != 200:
            raise Exception(f"Ошибка получения URL: {response.status_code} — {response.text}")
        
        upload_link = response.json().get('href')
        if not upload_link:
            raise Exception("Не получен href для загрузки из ответа API")
        
        # Шаг 2: Загружаем файл по полученному URL (метод PUT)
        logger.debug(f"📤 Загрузка файла: {local_path} → {upload_link}")
        with open(local_path, 'rb') as f:
            put_response = requests.put(
                upload_link,
                data=f,
                headers={'Content-Type': 'application/octet-stream'},
                timeout=120  # долгий таймаут для больших файлов
            )
        
        if put_response.status_code not in (200, 201):
            raise Exception(f"Ошибка PUT-загрузки: {put_response.status_code} — {put_response.text}")
        
        logger.debug(f"✅ Файл загружен, ответ: {put_response.status_code}")

    def _get_token_scopes(self, token: str) -> str:
        """Получает список прав (scopes) для токена"""
        import requests
        try:
            response = requests.get(
                'https://oauth.yandex.ru/info',
                headers={'Authorization': f'OAuth {token}'},
                timeout=10
            )
            if response.status_code == 200:
                scopes = response.json().get('scopes', [])
                return ', '.join(scopes) if scopes else 'неизвестно'
        except:
            pass
        return 'не удалось проверить'

    def _get_current_interval(self) -> int:
        """Возвращает текущий интервал в зависимости от времени суток"""
        current_hour = datetime.now().hour
        if self.night_start >= self.night_end:
            is_night = current_hour >= self.night_start or current_hour < self.night_end
        else:
            is_night = self.night_start <= current_hour < self.night_end
        return self.night_interval if is_night else self.day_interval

    def _get_next_mode_change(self) -> tuple[int, str]:
        """Возвращает секунды до смены режима и название следующего режима"""
        now = datetime.now()
        current_hour = now.hour
        is_currently_night = (
            current_hour >= self.night_start or current_hour < self.night_end
        ) if self.night_start >= self.night_end else (
            self.night_start <= current_hour < self.night_end
        )
        
        if is_currently_night:
            next_change = now.replace(hour=self.night_end, minute=0, second=0, microsecond=0)
            if next_change <= now:
                next_change += timedelta(days=1)
            return int((next_change - now).total_seconds()), "день (15 мин)"
        else:
            next_change = now.replace(hour=self.night_start, minute=0, second=0, microsecond=0)
            if next_change <= now:
                next_change += timedelta(days=1)
            return int((next_change - now).total_seconds()), "ночь (30 мин)"

    async def _wake_video(self, page: Page):
        """Пытается 'разбудить' видео, если оно на паузе"""
        try:
            await page.evaluate(f'''() => {{
                const video = document.querySelector('{self.video_selector}');
                if (video && video.paused) {{
                    video.play().catch((e) => console.log('Wake error:', e));
                }}
            }}''')
        except Exception as e:
            logger.debug(f"⚠️ Не удалось разбудить видео: {e}")

    async def capture(self, page: Page, max_retries: int = None) -> str | None:
        """Захват кадра из video-элемента через canvas с повторными попытками"""
        if max_retries is None:
            max_retries = self.capture_max_retries
        
        for attempt in range(max_retries + 1):
            try:
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                filename = f"screenshot_{timestamp}.jpg"
                filepath = self.output_dir / filename
                
                screenshot_base64 = await page.evaluate(f'''() => {{
                    return new Promise((resolve) => {{
                        const video = document.querySelector('{self.video_selector}');
                        if (!video || video.readyState < 2) {{
                            resolve(null);
                            return;
                        }}
                        
                        const canvas = document.createElement('canvas');
                        canvas.width = video.videoWidth || 1280;
                        canvas.height = video.videoHeight || 720;
                        const ctx = canvas.getContext('2d');
                        if (!ctx) {{ resolve(null); return; }}
                        
                        ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
                        
                        canvas.toBlob((blob) => {{
                            const reader = new FileReader();
                            reader.onloadend = () => resolve(reader.result);
                            reader.readAsDataURL(blob);
                        }}, 'image/jpeg', 0.85);
                    }});
                }}''')
                
                if screenshot_base64:
                    with open(filepath, 'wb') as f:
                        f.write(base64.b64decode(screenshot_base64.split(',')[1]))
                    
                    # Проверка на подозрительный файл
                    if self._is_suspicious_screenshot(filepath):
                        logger.warning(f"⚠️ Подозрительный файл ({filepath.stat().st_size} байт), пробуем ещё раз...")
                        filepath.unlink(missing_ok=True)
                        raise ValueError("Suspicious screenshot content")
                    
                    logger.info(f"✓ Скриншот: {filepath} ({filepath.stat().st_size // 1024} KB)")
                    return str(filepath)
                
                elif attempt < max_retries:
                    logger.warning(f"⚠️ Попытка {attempt+1}/{max_retries+1}: видео не готово, ждём 2 сек...")
                    await asyncio.sleep(2)
                    await self._wake_video(page)
                else:
                    logger.error("❌ Все попытки захвата исчерпаны")
                    return None
                    
            except Exception as e:
                logger.warning(f"⚠️ Ошибка захвата (попытка {attempt+1}/{max_retries+1}): {e}")
                if attempt < max_retries:
                    await asyncio.sleep(2)
                    await self._wake_video(page)
                else:
                    logger.error("❌ Критическая ошибка после всех попыток", exc_info=True)
                    return None
        
        return None

    async def run(self):
        """Основной цикл работы бота"""
        async with async_playwright() as p:
            # Запуск браузера с аргументами для стабильности
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    '--disable-background-timer-throttling',
                    '--disable-backgrounding-occluded-windows',
                    '--disable-renderer-backgrounding',
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                ]
            )
            context = await browser.new_context(viewport=self.viewport)
            page = await context.new_page()
            
            logger.info(f"🌐 Загрузка: {self.page_url}")
            
            # Функция инициализации страницы
            async def init_page():
                await page.goto(self.page_url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_selector(self.video_selector, timeout=20000)
                await page.evaluate(f'''async () => {{
                    const video = document.querySelector('{self.video_selector}');
                    if (video) {{
                        video.muted = true;
                        video.playsInline = true;
                        if (video.paused) {{
                            try {{ await video.play(); }} catch(e) {{ console.log('Play error:', e); }}
                        }}
                    }}
                }}''')
                await asyncio.sleep(2)
            
            await init_page()
            
            capture_count = 0
            duplicate_count = 0
            prev_hash = None
            
            try:
                while True:
                    start = asyncio.get_event_loop().time()
                    
                    # Проверка состояния видео
                    video_state = await page.evaluate(f'''() => {{
                        const video = document.querySelector('{self.video_selector}');
                        if (!video) return {{ error: 'not_found' }};
                        return {{
                            paused: video.paused,
                            readyState: video.readyState,
                            currentTime: video.currentTime,
                            error: video.error ? video.error.message : null,
                            videoWidth: video.videoWidth,
                            videoHeight: video.videoHeight
                        }};
                    }}''')
                    
                    if capture_count % 5 == 0:
                        logger.debug(f"📊 Video state: {video_state}")
                    
                    # Авто-восстановление если видео зависло
                    if video_state.get('error') or video_state.get('readyState', 0) < 2 or video_state.get('paused'):
                        logger.warning(f"⚠️ Видео зависло: {video_state}, перезагружаем страницу...")
                        await init_page()
                        await asyncio.sleep(3)
                        capture_count = 0
                        duplicate_count = 0
                        continue
                    
                    current_interval = self._get_current_interval()
                    mode = "🌙 ночь" if current_interval == self.night_interval else "🌅 день"
                    
                    logger.info(f"[{mode}] Делаю скриншот #{capture_count+1} (интервал: {current_interval//60} мин)")
                    result = await self.capture(page, max_retries=self.capture_max_retries)
                    
                    if result:
                        filepath = Path(result)
                        current_hash = self._file_hash(filepath)
                        
                        # Проверка на дубликаты
                        if prev_hash and current_hash == prev_hash:
                            duplicate_count += 1
                            logger.warning(f"⚠️ Дубликат #{duplicate_count}! Хеш: {current_hash[:8]}...")
                            if duplicate_count >= 3:
                                logger.warning("🔄 Слишком много дубликатов, перезагружаем страницу...")
                                await init_page()
                                duplicate_count = 0
                        else:
                            duplicate_count = 0
                            prev_hash = current_hash
                        
                        capture_count += 1
                        
                        # ✅ Загрузка в Яндекс.Диск (не блокирует основной цикл)
                        await self._upload_to_yadisk_async(str(filepath), filepath.name)
                        
                        # Периодическая перезагрузка страницы
                        if capture_count >= self.reload_every:
                            logger.info(f"🔄 Перезагружаем страницу после {self.reload_every} скриншотов")
                            await init_page()
                            capture_count = 0
                            duplicate_count = 0
                            await asyncio.sleep(2)
                    
                    elapsed = asyncio.get_event_loop().time() - start
                    seconds_to_change, next_mode = self._get_next_mode_change()
                    sleep_time = min(max(0, current_interval - elapsed), seconds_to_change)
                    
                    if sleep_time > 0:
                        next_ts = datetime.now().timestamp() + sleep_time
                        next_dt = datetime.fromtimestamp(next_ts).strftime('%H:%M:%S')
                        action = f"смена → {next_mode}" if sleep_time == seconds_to_change else "следующий скриншот"
                        sleep_min = int(sleep_time)
                        logger.info(f"⏳ {action} в {next_dt} (через {sleep_min//60}:{sleep_min%60:02d})")
                        await asyncio.sleep(sleep_time)
                    else:
                        await asyncio.sleep(1)
                        
            except KeyboardInterrupt:
                logger.info("🛑 Остановка по Ctrl+C")
            except Exception as e:
                logger.error(f"❌ Критическая ошибка в цикле: {e}", exc_info=True)
            finally:
                await browser.close()
                logger.info("🔌 Браузер закрыт")


async def main():
    page_url = os.getenv('PAGE_URL')
    if not page_url:
        logger.error("❌ Укажите PAGE_URL в файле .env")
        return
    
    screenshotter = BrowserScreenshotter(
        page_url=page_url,
        video_selector=os.getenv('VIDEO_SELECTOR', '#video'),
        output_dir=os.getenv('SCREENSHOT_DIR', './screenshots'),
        viewport={
            "width": int(os.getenv('VIEWPORT_WIDTH', 1280)),
            "height": int(os.getenv('VIEWPORT_HEIGHT', 720))
        }
    )
    await screenshotter.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Завершение работы...")