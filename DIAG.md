# Діагностика 500 на sendVideo

Гілка `diag-sendvideo-500` побудована на `0d99e59` (збірка, на якій збій підтверджено після відкату).
Логіка завантаження, надсилання й підрахунку не змінена. Додано тільки:

- **Збереження точних байтів.** Якщо файл завантажено, а надсилання в Telegram впало, бот копіює його в
  `/tmp/diag/last_failed.mp4` і пише `last_failed.json`: URL, параметри `sendVideo`, повний traceback,
  розмір, SHA-256, порядок MP4-атомів, профіль `ffprobe`, версії бібліотек.
- **Версії при старті.** Рядок `DIAG версії:` у журналі Render — фактично встановлені PTB, httpx, yt-dlp, ffmpeg.
- **`/diag` у боті** — контрольний тест з Render: `control.mp4` + `last_failed.mp4` (лише в чаті, де він упав).
  `/diag variants` додає перепаковану (`-c copy +faststart`) і перекодовану в H.264/AAC копії.
- **`diag.py`** — той самий код надсилання (httpx, однакові тайм-аути й параметри) для Mac.
  Кожна спроба відкриває файл заново; невдача повторюється через 5 с, після неї — контрольний `getMe`.
- **Окремий канал для файлу:** `GET /diag/{last_failed.mp4,last_failed.json,results.json}` із заголовком
  `X-Diag-Key: <WEBHOOK_SECRET з Render → Environment>`. Без ключа — 403.

Webhook тепер обслуговує власний tornado-сервер (той самий шлях `/webhook` і перевірка секрету) —
інакше на одному порту Render не додати маршрут `/diag`.

⚠️ На безкоштовному Render `/tmp` очищується при перезапуску й засинанні (15 хв без запитів).
Забирай файл і запускай `/diag` одразу після збою.

## Порядок тесту

1. Задеплоїти гілку (Render → Settings → Branch: `diag-sendvideo-500`), дочекатися Live.
2. Надіслати проблемний Reel (напр. `Da9BVPENsV9`), дочекатися помилки.
3. Одразу в боті: `/diag variants` → **клітинки A і C** (+ варіанти). Відповідь — зведення текстом.
4. На Mac у клоні цієї гілки (`pip install -r requirements.txt`, `BOT_TOKEN` у `.env`):

   ```bash
   python diag.py fetch --base https://reels-bot-8syg.onrender.com --key WEBHOOK_SECRET
   ```
   Має вивести `✅ Файл збігається байт у байт`. Інакше тест D недійсний.

   ```bash
   python diag.py matrix --chat-id ТВІЙ_CHAT_ID control.mp4 diag_files/last_failed.mp4 --variants
   ```
   Це **клітинки B і D** тим самим кодом. Звіт — `diag_files/mac_results.json`;
   звіт Render — `diag_files/render_results.json`.

`chat_id` є в `diag_files/last_failed.json`.

## Як читати

| control (A Render / B Mac) | Reel (C Render / D Mac) | Висновок |
|---|---|---|
| OK / OK | 500 / 500 | Файл або його обробка в Telegram → дивись варіанти й `ffprobe` |
| 500 / OK | 500 / OK | Вихідне завантаження з Render |
| OK / OK | 500 / OK | Поєднання цього файлу з передачею з Render (розмір, швидкість) |
| OK / OK | OK / OK | Тимчасовий збій Telegram або код надсилання бота, а не байти |

Варіанти: проходить `remux` → контейнер (атоми, edit lists, мітки часу); проходить лише `h264` → кодек/профіль;
падають усі → розмір або щось поза вмістом файлу. Порівняння `curl` vs httpx — окремим кроком після матриці.

Після діагностики повернути Render на `main`.
