# kie.ai MCP — генерация изображений

MCP-сервер, который оборачивает **unified Jobs API** kie.ai (`createTask` + `recordInfo`)
и даёт Claude инструмент «сгенерируй картинку». Написан на голом Python —
**без единой зависимости**, только стандартная библиотека.

Работает в двух режимах:

- **Локально (stdio)** — для Claude Code на своей машине.
- **Удалённо (Streamable HTTP)** — публичный HTTPS-эндпоинт для Claude Custom
  Connectors и для `claude mcp add --transport http`. Есть готовый адаптер под Vercel.

## Структура

```
kie_server.py        # ядро: логика инструментов + JSON-RPC (stdio-режим)
kie_http_server.py   # автономный HTTP-сервер (VPS / Fly / Render / Docker)
api/mcp.py           # serverless-адаптер для Vercel (переиспользует kie_server.py)
vercel.json          # маршрутизация /mcp и /health на функцию api/mcp.py
README.md
```

Все три исполняемых файла используют один и тот же движок из `kie_server.py`, поэтому
поведение инструментов везде идентично.

## Инструменты

| Tool | Что делает |
|------|------------|
| `kie_generate_image` | Создаёт задачу генерации, ждёт результат, возвращает URL. Если задан `save_dir` — скачивает файл локально и возвращает путь. |
| `kie_get_task` | Проверяет статус/результат задачи по `task_id`. |

Параметры `kie_generate_image`: `prompt` (обяз.), `model` (по умолч. `nano-banana-2`),
`aspect_ratio` (`1:1`, `16:9`, `9:16`, `4:3`, `3:4` …), `resolution` (`1K`/`2K`/`4K`),
`output_format` (`png`/`jpg`), `image_input` (URL для image-to-image), `save_dir`, `timeout_sec`.

Список моделей и их id — на https://kie.ai/market.

## Переменные окружения

| Переменная | Обяз. | Назначение |
|------------|:----:|------------|
| `KIE_API_KEY` | да | Ключ kie.ai. Создать: https://kie.ai/api-key |
| `KIE_TIMEOUT_SEC` | нет | Дефолтный лимит ожидания генерации, сек (по умолч. 180). На Vercel держи ниже `maxDuration` из `vercel.json`. |
| `KIE_MCP_TOKEN` | нет | Bearer-токен: если задан, `POST /mcp` требует `Authorization: Bearer <token>`. См. раздел «Авторизация». |
| `KIE_SAVE_DIR` | нет | Папка по умолчанию для скачивания результата. |
| `PORT` / `KIE_MCP_PORT` | нет | Порт HTTP-сервера (по умолч. 8000; `PORT` в приоритете). Только для `kie_http_server.py`. |
| `KIE_MCP_HOST` | нет | Адрес привязки (по умолч. `0.0.0.0`). Только для `kie_http_server.py`. |
| `KIE_MCP_PATH` | нет | Путь MCP-эндпоинта (по умолч. `/mcp`). Только для `kie_http_server.py`. |

---

## Режим 1 — локально в Claude Code (stdio)

### 1. Получи ключ
Создай ключ на https://kie.ai/api-key.

### 2. Пропиши ключ (НЕ вставляй его в чат и не коммить)
Windows PowerShell, один раз:
```powershell
setx KIE_API_KEY "сюда_твой_ключ"
```
Затем **полностью перезапусти терминал и Claude Code** — `setx` действует только
для новых процессов.

macOS/Linux:
```bash
export KIE_API_KEY="сюда_твой_ключ"   # добавь в ~/.zshrc или ~/.bashrc, чтобы не терялось
```

### 3. Подключи сервер
Из папки репозитория:
```bash
claude mcp add --transport stdio kie -- python3 kie_server.py
```
На Windows вместо `python3` обычно `py`. Ключ `KIE_API_KEY` подхватится из окружения.

Проверь: команда `/mcp` в интерактивном Claude Code должна показать сервер **kie**
со статусом `connected` и двумя инструментами.

> Альтернатива — проектный `.mcp.json` в корне (его можно закоммитить и делить с командой):
> ```json
> {
>   "mcpServers": {
>     "kie": {
>       "type": "stdio",
>       "command": "python3",
>       "args": ["kie_server.py"],
>       "env": { "KIE_API_KEY": "${KIE_API_KEY}" }
>     }
>   }
> }
> ```

---

## Режим 2 — удалённо на Vercel (для Custom Connectors)

Claude Custom Connectors принимают только удалённые MCP-серверы по публичному
HTTPS-URL (транспорт *Streamable HTTP*). Для этого в репе есть `api/mcp.py` +
`vercel.json`.

### Деплой
1. Запушь репозиторий и импортируй его в Vercel (**New Project → выбери репо**).
   Root Directory не трогай — оставь корень, всё описано в `vercel.json`.
2. В **Settings → Environment Variables** добавь `KIE_API_KEY` (по желанию
   `KIE_TIMEOUT_SEC`, `KIE_MCP_TOKEN`). Ставить нечего — только stdlib.
3. **Deploy.** `vercel.json` делает rewrite `/mcp → /api/mcp`, поэтому URL для
   коннектора: `https://<проект>.vercel.app/mcp`.
4. Проверь живость: открой `https://<проект>.vercel.app/health` → `{"status":"ok"}`.

### Эндпоинты

| Метод + путь | Назначение |
|--------------|------------|
| `POST /mcp` | JSON-RPC 2.0 запрос → JSON-RPC ответ (`application/json`). |
| `GET /mcp` | Отдаёт liveness-payload (серверный SSE-стрим не предлагается). |
| `GET /health` | `{"status":"ok"}` — проверка живости. |

### ⚠️ Про таймауты (главный нюанс Vercel)
Генерация блокирует функцию, пока опрашивает kie.ai (десятки секунд). Vercel
ограничивает длительность функции (Hobby ~60 c, Pro до 300 c). Поэтому:
- держи `KIE_TIMEOUT_SEC` (или per-call `timeout_sec`) **ниже** `maxDuration`
  из `vercel.json` (сейчас там 60 — ставь, например, 50);
- на медленных моделях / `4K` не жди в одном запросе: запусти
  `kie_generate_image` с маленьким `timeout_sec`, забери `task_id`, а результат
  добери через `kie_get_task`.

### Подключение удалённого сервера

**В Claude Code:**
```bash
claude mcp add --transport http --scope user kie https://<проект>.vercel.app/mcp
```
Если задал `KIE_MCP_TOKEN`, добавь заголовок (Claude Code умеет статические
заголовки, в отличие от веб-UI коннекторов):
```bash
claude mcp add --transport http --scope user kie https://<проект>.vercel.app/mcp \
  -H "Authorization: Bearer <ТВОЙ_ТОКЕН>"
```

**В Claude (веб / десктоп):** Settings → Connectors → Add custom connector →
вставь `https://<проект>.vercel.app/mcp` → Add.

---

## Режим 3 — свой сервер (VPS / Fly / Render / Docker)

```bash
KIE_API_KEY=... KIE_MCP_HOST=0.0.0.0 PORT=8000 python3 kie_http_server.py
```
Поставь перед ним HTTPS (Caddy / Nginx / Cloudflare) — коннектору нужен именно
`https://`. URL для коннектора: `https://твой-домен/mcp`.

---

## Проверка вручную (без Claude)

Дымовой тест протокола (без ключа, только список инструментов):
```powershell
'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{}}}','{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | py kie_server.py
```
Должны прийти два JSON-ответа: `initialize` и список из двух инструментов.

---

## Авторизация (прочитай перед публикацией URL)

Ключ `KIE_API_KEY` живёт на сервере, поэтому **все, кто достучится до URL,**
тратят твои кредиты kie.ai. Веб-UI коннекторов Claude умеет только OAuth
(Client ID/Secret) и **не даёт вписать статический заголовок** — значит
`KIE_MCP_TOKEN` защищает эндпоинт при подключении из Claude Code (там заголовок
передаётся флагом `-H`), но не как способ авторизовать сам веб-коннектор.
Практичные варианты для личного использования:

- деплой по «неугадываемому» URL + (если возможно) allowlist IP-диапазонов
  Anthropic на файрволе/прокси;
- полноценный OAuth 2.1 перед эндпоинтом (это отдельная задача, в минимальный
  сервер не входит).

## Заметки

- Бесплатный/любой план kie.ai: соблюдай rate limit (≈20 запросов / 10 сек).
- Медиа на kie.ai хранятся ~14 дней — если нужен файл надолго, используй `save_dir`.
- Сервер пишет в stdout только JSON-RPC (логи — в stderr), чтобы не ломать протокол.
