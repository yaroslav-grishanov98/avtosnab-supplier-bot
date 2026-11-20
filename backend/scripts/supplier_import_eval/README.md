# Харнесс оценки импорта прайсов поставщиков

Скрипты прогоняют реальные прайс-файлы через настоящий API импорта кабинета поставщика
(DRF `APIClient` с JWT кабинета, Celery в eager-режиме, локальная БД с pgvector), как это делает веб-интерфейс.

- `run_eval.py <out_dir> <llm|nollm> <file...>` - `analyze` -> опрос статуса до `ready` -> выгрузка всех строк -> `commit`.
  На каждый файл пишет `<out_dir>/<имя файла>__<режим>.json` (строки, счётчики, результат commit, лог всех LLM-вызовов
  с payload, ответом и временем).
- `run_commit.py <out_json> <file>` - имитирует пользователя: переводит `review` в `new`, заполняет обязательные поля
  через `PATCH /api/v1/suppliers/products/import/{session_id}/items/defaults` (страна, минимальный заказ,
  кратность, наличие; записывает `updated_count`), построчно доправляет только `unit_id` / `net_value` / `net_unit_id`,
  затем нажимает "Применить". LLM выключен, результат детерминированный.
- `eval_settings.py` - настройки Django (все флаги импорта включены, Celery eager, БД и пути из окружения).

## Переменные окружения

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `POSTGRES_HOST` / `POSTGRES_PORT` | `127.0.0.1` / `5432` | БД |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` | `postgres` / `postgres` | доступ к БД |
| `POSTGRES_DB` | `autosnab` | имя БД (миграции должны быть применены: `python manage.py migrate`) |
| `PGSSLMODE` | `disable` | sslmode |
| `EVAL_ALLOW_REMOTE_DB` | не задана | при `POSTGRES_HOST` не из localhost/127.0.0.1/::1 настройки отказываются стартовать, пока не задано `1` |
| `EVAL_MEDIA_ROOT` | `<tmp>/autosnab-eval-media` | каталог для загруженных файлов |
| `EVAL_LLM_MODEL` | `gpt-4o-mini` | модель LLM |
| `EVAL_OPENAI_KEY` | пусто | ключ LLM, читается ТОЛЬКО из окружения |

Для режима `llm` нужен собственный ключ пользователя в `EVAL_OPENAI_KEY`; без него `run_eval.py` в режиме `llm`
завершится с ошибкой. Ключ нигде не сохраняется и не логируется - не записывайте его в файлы и историю команд репозитория.

## Запуск

Из корня репозитория (скрипты находят настройки через `PYTHONPATH`):

```bash
export PYTHONPATH="$PWD/scripts/supplier_import_eval:."
export DJANGO_SETTINGS_MODULE=eval_settings
export POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=5434 POSTGRES_USER=postgres POSTGRES_PASSWORD=... POSTGRES_DB=autosnab PGSSLMODE=disable

# без LLM
python scripts/supplier_import_eval/run_eval.py /tmp/eval-out nollm /path/to/price.xlsx [/path/to/other.xlsx ...]

# с LLM (свой ключ в окружении)
EVAL_OPENAI_KEY=... python scripts/supplier_import_eval/run_eval.py /tmp/eval-out llm /path/to/price.xlsx

# сценарий "пользователь заполнил обязательные поля и нажал Применить"
python scripts/supplier_import_eval/run_commit.py /tmp/eval-out/commit.json /path/to/price.xlsx
```

Пути с пробелами и кириллицей передавайте в кавычках (или сначала скопируйте файл в каталог с простым именем).

## Что печатается

`run_eval.py`, по строке на файл: тег файла, `analyze_status` (ожидается 202), `status` сессии (ожидается `ready`),
`rows` (число строк), `counts` (разбивка по действиям new/update/review/skip и др.), `commit_http`, `products_created`,
`llm_calls` (число вызовов LLM; 0 в режиме `nollm`), `analyze_s` (время анализа, секунды).

`run_commit.py`, одна строка: имя файла, `rows`, `defaults_updated` (`updated_count` из эндпоинта defaults),
`patched` (сколько строк пришлось доправлять построчно), `imported` (импортировано), `not_ready` (пропущено как
неготовые), `errors` (топ сообщений об ошибках commit). Полные данные - в JSON.

## Важно

Прайс-файлы поставщиков в репозиторий НЕ коммитятся (и не должны попадать в git): держите их вне репозитория, в
манифест `manifests/raw_sources.csv` вносятся только описания. Каждый прогон создаёт в БД новых поставщиков
`Eval ...` / `Commit ...` - используйте локальную БД, не рабочую.
