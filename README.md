# Universal HTTP for Roblox

HTTP-адаптер для [logger-luau](https://github.com/7environment/logger-luau).
Репозиторий включает Luau-клиент и Python-прокси в каталоге `proxy/`.

Мини-проект состоит из Luau-модуля `init.luau` и необязательного
FastAPI-прокси. Модуль предоставляет один метод `Request`, выбирает лучший
доступный HTTP-транспорт executor-а и приводит ответы к одному формату.

Прокси нужен для двух случаев:

- слабому транспорту доступен только `game:HttpPost`, но целевому запросу нужны
  произвольный метод, заголовки или тело;
- executor нестабильно обрабатывает ответы с HTTP-статусами `4xx` и `5xx`.

Прокси всегда отвечает клиенту реальным HTTP `200`. Статус целевого сервера и
ошибки находятся внутри JSON-ответа.

## Выбор транспорта

Модуль ищет реализации в следующем порядке:

1. переданный вручную `Transport`;
2. `request`;
3. `http_request`;
4. `syn.request`;
5. `http.request`;
6. `fluxus.request`;
7. `krnl.request`, `delta.request` или `electron.request`;
8. `game:HttpPost`;
9. `game:HttpGet`.

`HttpService:RequestAsync`, `GetAsync` и `PostAsync` не используются.

При прямом запросе `game:HttpGet` подходит только для GET без заголовков, а
`game:HttpPost` — только для POST без заголовков. При включённом прокси одного
`game:HttpPost` достаточно для любых поддерживаемых целевых запросов: их метод,
заголовки и тело передаются прокси внутри JSON.

## Использование модуля

Загрузите `init.luau` удобным для проекта способом и создайте клиент:

```lua
local UniversalHttp = loadstring(readfile("init.luau"))()

local http = UniversalHttp.new({
    DefaultTimeout = 30,
    Proxy = {
        Enabled = true,
        Url = "https://proxy.example.com",
        Token = "replace-with-your-token",
    },
})
```

Выполнение запроса:

```lua
local HttpService = game:GetService("HttpService")

local response = http:Request({
    Url = "https://example.com/api/items",
    Method = "POST",
    Headers = {
        ["Authorization"] = "Bearer target-api-token",
        ["Content-Type"] = "application/json",
    },
    Body = HttpService:JSONEncode({name = "Knife"}),
    Timeout = 20,
    FollowRedirects = false,
})

if response.Success then
    print(response.StatusCode, response.Body)
else
    warn(response.StatusCode, response.Error or response.StatusMessage)
end
```

`http:request(options)` является алиасом для `http:Request(options)`.

Для одного запроса можно переопределить общий режим:

```lua
http:Request({
    Url = "https://example.com/health",
    Method = "GET",
    UseProxy = false,
})
```

Можно передать собственную полную реализацию транспорта:

```lua
local http = UniversalHttp.new({
    Transport = myRequestFunction,
})
```

## Формат ответа

Модуль всегда возвращает таблицу:

```lua
{
    Success = true,          -- целевой статус находится в диапазоне 200–299
    StatusCode = 200,        -- статус целевого сервера
    StatusMessage = "OK",
    Headers = {},
    Body = "...",
    BodyEncoding = "text",  -- либо "base64" для бинарного ответа прокси
    Error = nil,             -- транспортная, proxy- или validation-ошибка
    Transport = "request",  -- фактически выбранная реализация
    Proxied = true,
}
```

Сетевые ошибки возвращаются через `Error`, а не выбрасываются из метода. Вызов
executor-транспорта дополнительно обёрнут в `pcall`, хотя это не может защитить
от executor-а, который аварийно завершает весь Luau-поток вне обычной модели
ошибок.

## Запуск прокси

Требуется Python 3.11 или новее.

```shell
cd proxy
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Proxy автоматически загружает файл `proxy/.env` независимо от текущей рабочей
директории. Переменные, уже заданные в окружении процесса, имеют приоритет над
значениями из файла. Минимальный запуск:

```shell
uvicorn main:app --host 0.0.0.0 --port 8000
```

Проверка состояния:

```shell
curl http://127.0.0.1:8000/health
```

### Настройки прокси

| Переменная | По умолчанию | Назначение |
| --- | ---: | --- |
| `PROXY_TOKEN` | пусто | Токен доступа. Пустое значение отключает авторизацию. |
| `ALLOW_PRIVATE_TARGETS` | `false` | Разрешает localhost и приватные IP. Используйте только в доверенной сети. |
| `MAX_TIMEOUT` | `60` | Максимальный timeout целевого запроса в секундах. |
| `MAX_REDIRECTS` | `5` | Максимум переходов при `FollowRedirects = true`. |
| `MAX_REQUEST_BYTES` | `2097152` | Максимальный размер JSON-запроса к прокси. |

Токен передаётся модулем внутри JSON-тела, поэтому авторизация работает даже
через `game:HttpPost`, который не умеет задавать произвольные заголовки. Прокси
также принимает стандартный `Authorization: Bearer ...` от других клиентов.

## Поведение и ограничения

- Endpoint `/proxy` возвращает HTTP `200` при ошибках авторизации, валидации,
  timeout, сетевых ошибках и любых статусах целевого сервера.
- Reverse proxy, CDN или сервер приложений всё ещё может вернуть собственный
  не-200 ответ, если запрос не дошёл до FastAPI.
- По умолчанию заблокированы localhost, приватные и другие non-global адреса.
  Это защищает публично размещённый прокси от базовых SSRF-запросов.
- Заголовки `Host`, `Content-Length` и hop-by-hop заголовки не пересылаются.
- Текстовые ответы возвращаются как UTF-8. Бинарные ответы кодируются в base64
  и помечаются `BodyEncoding = "base64"`; модуль их автоматически не декодирует.
- Runtime-ошибка выбранного транспорта не переключает запрос на другой
  транспорт, поскольку повтор может дважды выполнить POST/PATCH на целевом
  сервере.
- Пустое поле `Headers = {}` не отправляется в proxy payload, поскольку Roblox
  кодирует пустую Luau-таблицу как JSON `[]`. Proxy также принимает пустой массив
  `headers` как `{}` для совместимости со старыми версиями клиента.

## Тесты прокси

После установки зависимостей:

```shell
cd proxy
python -m unittest -q
```
