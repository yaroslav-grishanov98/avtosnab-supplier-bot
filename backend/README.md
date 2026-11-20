# АвтоСнаб Backend MVP

Backend-сервис для каталога поставщиков АвтоСнаб. Принимает прайсы через Telegram-бот, n8n и веб-кабинет, нормализует строки (deterministic parser + AI fallback через Celery), ведёт товары поставщиков с историей цен, обслуживает каталог для заведений с фильтрами и отдаёт snapshot для Google Sheets.

**Основной принцип:** PostgreSQL — source of truth. Backend владеет нормализацией и бизнес-логикой. Telegram-бот и веб-интерфейс — равноправные клиенты одной логики.

**Репозиторий:** GitLab — `https://gitlab.testant.online/antipov-backend/auto-snab`

## Документация API

После запуска сервера доступны интерактивные docs:

| URL | Описание |
|-----|---------|
| `/api/schema/swagger-ui/` | Swagger UI (интерактивный, кнопка Authorize для токена) |
| `/api/schema/redoc/` | ReDoc (удобный для чтения) |
| `/api/schema/` | Сырая OpenAPI 3.0 схема (JSON/YAML) |

## Что умеет сервис

### Авторизация

Подробнее про способы и JWT — раздел [«Аутентификация»](#аутентификация) ниже.

- `POST /api/v1/auth/supplier/register` — регистрация поставщика (email+пароль), письмо для подтверждения
- `POST /api/v1/auth/venue/register` — самостоятельная регистрация заведения/покупателя (email+пароль, как у Ozon-покупателя), письмо для подтверждения
- `POST /api/v1/auth/email-verification/{token}/confirm` — подтвердить email, получить JWT (общий для обеих регистраций)
- `POST /api/v1/auth/supplier/login` — вход поставщика (email+пароль)
- `POST /api/v1/auth/venue/login` — вход заведения/закупщика (email+пароль, единственное поле для входа — и для admin-provisioned сотрудников тоже)
- `POST /api/v1/auth/venue/bootstrap` — разовый бутстрап первого admin (`SERVICE_API_TOKEN`)
- `POST /api/v1/auth/forgot-password` / `POST /api/v1/auth/password-reset/{token}/confirm` — сброс пароля
- `GET /api/v1/auth/yandex/login?intent=supplier|venue` / `GET /api/v1/auth/yandex/callback` — вход через Яндекс
- `GET /api/v1/auth/vk/login?intent=supplier|venue` / `GET /api/v1/auth/vk/callback` — вход через VK ID (OAuth 2.1 + PKCE)
- `GET /api/v1/auth/me` — профиль текущего пользователя (JWT, любая роль): `user_id`/`email`/`full_name`/`role`/`status`/`venue_id`/`venue_name`/`has_supplier_company`. Вызывать сразу после логина/OAuth-колбэка — `venue_id` нужен дальше для `/catalog/venues/{venue_id}/...`. Для `role=supplier`: `has_supplier_company=false` значит у пользователя ещё нет ни одной компании — предложить создать через `POST /suppliers/mine` (не блокируя вход) вместо того, чтобы сразу дёргать `session-from-login`, который на пустом списке даёт `404`
- `POST /api/v1/token/refresh` — обновить JWT access-токен по refresh

### Прайсы поставщиков

- `POST /api/v1/suppliers/uploads` — загрузить файл прайса (xlsx/csv/pdf/docx/txt)
- `POST /api/v1/suppliers/uploads/text` — загрузить текстовый прайс
- `POST /api/v1/suppliers/uploads/{id}/normalize` — запустить нормализацию (sync или async через Celery)
- `POST /api/v1/suppliers/uploads/{id}/apply-ai-result` — применить AI JSON напрямую (legacy, от n8n AI Agent)
- `POST /api/v1/suppliers/uploads/{id}/ai-cleanup` — запустить AI-cleanup фазу (исправление ошибок)
- `GET /api/v1/suppliers/uploads/{id}` — статус загрузки и счётчики строк
- `GET /api/v1/suppliers/uploads/{id}/items` — строки загрузки с пагинацией
- `GET /api/v1/suppliers/uploads/{id}/preview-excel` — скачать превью в Excel

### Товары поставщика

- `GET /api/v1/suppliers/products` — список товаров с текущей и предыдущей ценой, фильтры
- `PATCH /api/v1/suppliers/products/{id}` — обновить товар, включая доступность (`availability: in_stock/out_of_stock`); при изменении цены создаётся новая active price, старая → superseded
- `POST /api/v1/suppliers/products/{id}/archive` — архивировать товар и закрыть active prices

### Компании-поставщики

- `GET /api/v1/suppliers/` — полный список всех компаний (JWT admin)
- `POST /api/v1/suppliers/` — admin добавляет компанию (временный owner до передачи реальному поставщику)
- `GET /api/v1/suppliers/mine` — **список** компаний текущего JWT-пользователя, от последней созданной к первой (может быть несколько — 2026-07-24, `[]`, не 404, если ни одной)
- `POST /api/v1/suppliers/mine` — self-service: сам поставщик (JWT, role=supplier) заводит ещё одну свою компанию и сразу становится её владельцем; можно вызывать многократно, ограничения на число компаний нет — только дедуп по реквизитам (см. ниже). Принимает опциональные `addresses`/`contacts`/`bank_accounts`/`schedule_rules` — массивы объектов той же формы, что и в суб-ресурсных ручках ниже, создаются одним запросом вместе с компанией (2026-07-28 — форма в макете один экран с одной кнопкой «Сохранить», фронту не нужно дёргать 4 отдельных эндпоинта сразу после создания)
- `GET /api/v1/suppliers/inn-lookup` — проверка компании по ИНН через dadata (реквизиты, статус в реестре, ОКВЭД/ОКПО/дата регистрации/ФИО директора — 2026-07-27, поля из Figma-разбора `docs/wiki/supplier-cabinet-figma-design-2026-07.md`). JWT `admin` **или** `supplier` (2026-07-28 — раньше был жёстко admin-only, из-за чего supplier получал `403` при попытке автозаполнить форму создания своей компании)
- `POST /api/v1/suppliers/{id}/account-transfer` — передать компанию реальному поставщику по email
- `POST /api/v1/suppliers/account-transfer/{token}/claim` — принять компанию по ссылке из письма
- `POST /api/v1/suppliers/{id}/restore` — восстановить удалённую (`archived`) компанию, отменяет `DELETE /suppliers/cabinet/me` (`status → active`, `is_published → true`). **Не через cabinet-JWT** (тот заблокирован при `status != active`) — обычный JWT `core.User`, доступ только активному `owner` этой компании или staff-аккаунту (`CanRestoreSupplier`, тот же уровень доступа, что у `account-transfer`). `400`, если компания не `archived` — восстанавливать нечего (2026-07-29)
- `GET /api/v1/suppliers/search-by-inn` — найти уже существующую **активную** компанию в нашей системе по ИНН (не путать с `inn-lookup` — там внешний реестр dadata). `404`, если не найдена. Первый шаг флоу «присоединиться к существующей компании»
- `POST /api/v1/suppliers/{id}/join-requests` — запросить доступ к найденной компании (JWT `core.User`, любая роль). Поля: `message` (описание ситуации), `phone`/`email` (контакты для этой заявки — могут отличаться от данных аккаунта), `documents` (подтверждающие файлы: PDF/JPEG/PNG/WebP, до 5 шт., до 10 МБ каждый — **multipart**; без файлов можно и JSON, как раньше). Владелец компании одобряет/отклоняет из своего ЛК (`GET/POST /suppliers/{id}/join-requests/pending`, `.../approve`, `.../reject`), заявитель видит свои заявки в `GET /suppliers/join-requests/mine` (2026-07-29)

### Кабинет поставщика (веб + JWT API)

- `POST /api/v1/suppliers/cabinet/token` — сгенерировать одноразовый токен для входа (из бота)
- `POST /api/v1/suppliers/cabinet/session` — обменять одноразовый токен (от бота) на JWT-пару кабинета (`access`/`refresh`, claim `supplier_id`)
- `POST /api/v1/suppliers/cabinet/session-from-login` — обменять обычный JWT `core.User` (email+пароль/Яндекс/VK — claim `user_id`) на JWT-пару кабинета для одной конкретной компании. Требует `Authorization: Bearer <user_id-JWT>`. Тело `{"supplier_id": "..."}` — **обязательно указывать явно, если у пользователя несколько компаний** (список — `GET /suppliers/mine`); без него берётся последняя созданная (для обратной совместимости, когда компания одна). `404`, если `supplier_id` не принадлежит пользователю, либо у него вообще нет ни одной компании. **Возвращает отдельный, самостоятельный токен — не замена исходному.** Держите на фронте оба: исходный `user_id`-JWT для `/auth/me` и любых `core.User`-ручек, новый `supplier_id`-JWT только для `/suppliers/cabinet/*` этой конкретной компании. Попытка вызвать `/auth/me` с `supplier_id`-JWT даёт `401 {"detail": "Token contained no recognizable user identification"}` — это ожидаемо, не баг: два независимых типа токена, см. «Аутентификация» ниже
- `GET/PATCH/DELETE /api/v1/suppliers/cabinet/me` — снимок кабинета / обновление карточки компании / **удалить компанию** (`DELETE`, 2026-07-29 — мягкое удаление: `status → archived`, `is_published → false`, не физическое стирание строки — у компании могут быть прайсы/товары/заказы, `SupplierPriceUpload`/`SupplierProduct` ссылаются через `on_delete=RESTRICT`, реальный `DELETE` упал бы с ошибкой БД. Тот же cabinet-токен сразу перестаёт работать — `SupplierCabinetJWTAuthentication` пускает только `status=active`. Товары/компания сразу пропадают из каталога (`catalog_products_queryset`/`catalog_suppliers_queryset` фильтруют по `supplier__status=active`/`is_published=true` — этот фильтр раньше отсутствовал вообще, добавлен тем же изменением). Компания остаётся видна владельцу в `GET /suppliers/mine` со статусом `archived` — не пропадает молча. Восстановить может сам владелец через `POST /suppliers/{id}/restore` (см. ниже) — не через кабинет, тот заблокирован) (JWT `supplier_id`, скоупится ровно на ту компанию, для которой был выдан токен в `session-from-login`/`session`). Редактируемые поля включают `kpp`/`ogrn`/`legal_address`/`okved`/`okpo`/`registration_date`/`general_director`/`short_name` (сокращённое юр. название, например «ООО «Ромашка»» — отдельно от `legal_name`, полного, и `display_name`, публичного; добавлено 2026-07-28) — можно подтянуть автоматически через `GET /suppliers/inn-lookup` по ИНН и сохранить как есть, либо ввести/поправить вручную (2026-07-27). Плюс с 2026-07-28: `logo_url` (устанавливается через `POST /suppliers/cabinet/logo`, ниже — не редактируется тут напрямую как текст), `description`, `fulfillment_method` (`supplier_delivery`/`pickup`/`carrier`), `shipping_point_name`/`shipping_point_address`, `standard_lead_time`, `pickup_available`/`pickup_conditions`/`pickup_address`, `entity_type` (`organization`/`individual_entrepreneur`/`individual`), `delivery_min_order_amount`/`delivery_currency` и `pickup_min_order_amount`/`pickup_currency` (минимальная сумма заказа и валюта — раздельно для курьерской доставки и самовывоза, т.к. в макете это два независимых значения; код валюты по умолчанию `RUB`, 2026-07-28). Ответ (`GET`, и в `SupplierDetailSerializer` вообще везде — `mine`, `cabinet/me`, staff-список) включает вложенные списки `addresses`/`contacts`/`bank_accounts`/`schedule_rules` — см. ниже. **Опциональные `addresses`/`contacts`/`bank_accounts`/`schedule_rules` в теле PATCH — если поле передано, ПОЛНОСТЬЮ заменяет текущий список** (весь экран пересохраняется одной кнопкой), если не передано — коллекция не трогается; передать `[]` — значит очистить
- `POST /api/v1/suppliers/cabinet/logo` — загрузить логотип компании (`multipart/form-data`, поле `logo`: JPEG/PNG/WebP/GIF, до 5 МБ). Сохраняет файл и сразу устанавливает `Supplier.logo_url` — второй `PATCH cabinet/me` не нужен. До 2026-07-28 `logo_url` был только текстовым полем (вставить готовую ссылку), реальной загрузки файла в этом API не было — Витя сообщил, что «загрузка логотипа не работает»
- **`is_published` (bool, по умолчанию `true`) — опубликована ли компания в каталоге, видна ли покупателям.** Редактируется через `is_published` в `POST /suppliers/mine` и `PATCH /suppliers/cabinet/me`, как и любое другое поле. Реально влияет на видимость в каталоге с 2026-07-29 — `catalog_products_queryset`/`catalog_suppliers_queryset` фильтруют по `supplier__is_published=true` (до этого поле только хранилось и отдавалось в API, ни на что не влияло — упущение обнаружено при реализации удаления компании). `status` (`active`/`inactive`/`archived`) — это **отдельное, системное** поле (создание/модерация/архивация платформой), не редактируется поставщиком напрямую: `SupplierCabinetJWTAuthentication` отклоняет вход в кабинет при `status != active`, поэтому публикация намеренно НЕ переиспользует `status` — self-service "снять с публикации" не должно попутно лишать поставщика доступа к собственному кабинету (2026-07-28, найдено на живой проверке до того, как это ушло в прод)
- **Форма создания/редактирования компании в макете — один экран, одна кнопка «Сохранить»**, включающая адреса/контакты/банковские реквизиты/график. Чтобы фронту не пришлось дёргать 5 разных эндпоинтов ради одного сохранения формы, `POST /suppliers/mine` и `PATCH /suppliers/cabinet/me` принимают `addresses`/`contacts`/`bank_accounts`/`schedule_rules` напрямую в теле (см. выше). Ручки ниже — для точечного управления одним элементом коллекции (добавить/поправить/удалить один адрес, не трогая остальные), не обязательный шаг после создания компании
- `GET/POST /api/v1/suppliers/cabinet/addresses`, `PATCH/DELETE /api/v1/suppliers/cabinet/addresses/{id}` — несколько типизированных адресов компании (`address_type`: `legal`/`actual`/`home`; `address` — полная строка адреса одним полем, основной ввод/отображение; `country`/`region`/`district`/`locality`/`street`/`postal_code`/`apartment_office` — необязательная развёрнутая детализация, не замена `address`). JWT `supplier_id`, строго скоупится на свою компанию — чужой `id` даёт `404` (2026-07-28)
- `GET/POST /api/v1/suppliers/cabinet/contacts`, `PATCH/DELETE /api/v1/suppliers/cabinet/contacts/{id}` — контакты компании (`contact_type`: `email`/`phone`/`messenger`/`link`, для мессенджера ещё `messenger_type` вроде `"MAX"`/`"Telegram"`; `link` — для сайта/соцсетей, повторяемое поле в макете). Это единственный источник контактов в API — `phone`/`email`/`website` как отдельные поля компании убраны из создания/редактирования/детализации (2026-07-28: в макете их нет вне раздела "Контакты", дублировали `contacts`)
- `GET/POST /api/v1/suppliers/cabinet/bank-accounts`, `PATCH/DELETE /api/v1/suppliers/cabinet/bank-accounts/{id}` — несколько расчётных счетов (`bank_name`/`account_number`/`bik`/`correspondent_account`/`is_primary`). Состав полей — стандартный для РФ, в самом Figma-макете форма была помечена как «не додумана», конкретных полей там не было (2026-07-28)
- `GET/POST /api/v1/suppliers/cabinet/schedule-rules`, `PATCH/DELETE /api/v1/suppliers/cabinet/schedule-rules/{id}` — строки недельного графика (`schedule_type`: `delivery`/`pickup`, `weekday` 0=Пн…6=Вс, `accepts_from`/`accepts_until`, опционально `fulfillment_weekday`, `is_active` — включено/выключено правило без удаления строки, по умолчанию `true`, добавлено 2026-07-29). Хранится как данные; сама целесообразность детального графика vs. простого `standard_lead_time` — открытый бизнес-вопрос, см. `docs/wiki/supplier-cabinet-figma-design-2026-07.md` (2026-07-28)
- `POST /api/v1/suppliers/{id}/bot-link-token` — кнопка «Присоединиться к Telegram-боту»: одноразовый токен + готовый deep-link (`https://t.me/<бот>?start=<token>`, TTL `SUPPLIER_BOT_LINK_TTL_MINUTES`, по умолчанию 30 мин). Открытие ссылки автоматически привязывает Telegram-аккаунт к этой компании — ручной ввод кода в чате убран (2026-07-15)
- `POST /api/v1/suppliers/{id}/invites` — пригласить сотрудника в ЛК компании по email (роль manager)
- `POST /api/v1/suppliers/invites/{token}/accept` — принять приглашение, задать пароль
- `GET /cabinet/?token=...` — веб-кабинет: карточка компании, история загрузок, статистика
- `GET /cabinet/supplier/{id}/` — кабинет по ID поставщика (по сессии)
- `POST /cabinet/supplier/{id}/update/` — обновить данные компании
- `POST /supplier/{id}/api/uploads/file/` — загрузить файл прайса через веб (drag-and-drop)
- `GET /supplier/{id}/api/uploads/{upload_id}/status/` — текущий статус обработки загрузки

### Каталог для заведений

**Просмотр каталога — публичный, без авторизации** (покупатель смотрит товары до входа, JWT нужен только для персонализации и с момента добавления в корзину/заказа):

- `GET /api/v1/catalog/products` — активные товары с ценами; поиск, мультиселект-фильтры, `is_favorite`, `wishlist_id`. JWT опционален — если передан, `is_favorite`/`wishlists` персонализируются под пользователя, без JWT просто `null`/`[]`
- `POST /api/v1/catalog/products/photo-search` — поиск по фото: AI распознаёт короткую поисковую фразу с изображения, дальше та же логика что у `q=`
- `GET /api/v1/catalog/brands` — список брендов
- `GET /api/v1/catalog/categories` — дерево категорий (группа → подгруппа → вид)
- `GET /api/v1/catalog/suppliers` — список поставщиков (с поиском по названию, ИНН)
- `GET /api/v1/catalog/countries` — список стран происхождения товаров

**Корзина/заказ и избранное — требуют JWT заведения или `SERVICE_API_TOKEN`** (это и есть граница «войти нужно, чтобы добавить в корзину»). `role=venue_user` может работать только со своим `venue_id` (сверяется с `core.User.venue`, 403 на чужой) — `admin` не ограничен:

- `GET/POST /api/v1/catalog/venues/{id}/favorites` — список / добавить в избранное
- `DELETE /api/v1/catalog/venues/{id}/favorites/{fav_id}` — убрать из избранного
- `GET/POST /api/v1/catalog/venues/{id}/orders` — история / создать черновик заказа
- `GET /api/v1/catalog/venues/{id}/orders/{order_id}` — детали заказа с позициями
- `POST /api/v1/catalog/venues/{id}/orders/{order_id}/items` — добавить позицию
- `PATCH/DELETE /api/v1/catalog/venues/{id}/orders/{order_id}/items/{item_id}` — изменить / удалить позицию
- `POST /api/v1/catalog/venues/{id}/orders/{order_id}/submit` — отправить заявку (draft → submitted)

#### Фильтры каталога

Все фильтры поддерживают **мультиселект** (передавать можно несколько значений):

| Параметр | Тип | Описание |
|----------|-----|----------|
| `q` | string | Полнотекстовый поиск |
| `brand_id` | uuid[] | Фильтр по брендам |
| `category_id` | uuid[] | Фильтр по категориям (включая подкатегории) |
| `supplier_id` | uuid[] | Фильтр по поставщикам |
| `country` | string[] | Страна происхождения |
| `availability` | string[] | `in_stock` / `out_of_stock` |
| `offers` | string[] | `promo` / `discount` / `new` |
| `price_min` | decimal | Минимальная цена |
| `price_max` | decimal | Максимальная цена |
| `sort` | string | `default`, `price_asc`, `price_desc`, `name_asc`, `name_desc`, `new`, `with_offers`, `in_stock_first` |
| `is_favorite` | bool | Только избранное (требует `venue_id`) |
| `venue_id` | uuid | Для аннотации `is_favorite` |
| `wishlist_id` | uuid | Только товары из вишлиста текущего пользователя (требует JWT) |

### Вишлисты

Именованные списки товаров, привязанные к JWT-пользователю заведения:

- `GET/POST /api/v1/catalog/wishlists` — список / создать
- `PATCH/DELETE /api/v1/catalog/wishlists/{id}` — переименовать / удалить
- `GET/POST /api/v1/catalog/wishlists/{id}/items` — товары в списке / добавить
- `DELETE /api/v1/catalog/wishlists/{id}/items/{item_id}` — убрать из списка

### Google Sheets

- `GET /api/v1/catalog/export/google-sheet` — snapshot для листа «Товары от поставщиков»
- `POST /api/v1/catalog/google-sheet-syncs` — записать лог синхронизации из n8n

### Telegram-бот (n8n)

- `GET /api/v1/bot-user-bindings/resolve` — найти поставщика по chat_id или telegram_user_id
- `POST /api/v1/bot-user-bindings/register` — зарегистрировать поставщика по `link_token` (из deep-link кнопки в ЛК, `/start <token>` в боте)
- `GET/POST /api/v1/bot-user-bindings/pending-actions` — ожидающие действия (загрузка прайса до регистрации)
- `PATCH/DELETE /api/v1/bot-user-bindings/pending-actions/{id}` — обновить / удалить pending action
- `GET/PUT/DELETE /api/v1/bot-user-states/current` — состояние бота для чата

### Страница товаров поставщика

- `GET /supplier/{id}/` — веб-страница с таблицей товаров (CRUD, пагинация, поиск)
- Столбцы: артикул, фото, наименование, бренд, группа, подгруппа, вид, цена, пред. цена, ед.изм., упаковка
- Подсветка изменённых цен (жёлтый фон, стрелки ↑↓, дата изменения)

### Служебное

- `GET /api/v1/health` — healthcheck (без авторизации)

---

## Хранение медиа-файлов (логотипы, документы заявок)

Загружаемые файлы (`POST /suppliers/cabinet/logo`, `documents` в `POST .../join-requests`) идут
через стандартную абстракцию Django (`default_storage`) — бэкенд переключается переменной
окружения, код менять не нужно:

- **`AWS_STORAGE_BUCKET_NAME` не задан** (по умолчанию, локальная разработка/тесты) — файлы
  пишутся на локальную ФС (`var/media`). В текущем прод-деплое (`docker/docker-compose.yml`) это
  не работает публично — нет persistent volume в контейнере `app`, nginx не проксирует `/media/`
  (см. `docs/wiki/logs/2026-07-29-logo-url-404-no-media-storage.md`).
- **`AWS_STORAGE_BUCKET_NAME` задан** — S3-совместимое object storage (по умолчанию рассчитано на
  Яндекс Object Storage: `AWS_S3_ENDPOINT_URL=https://storage.yandexcloud.net`,
  `AWS_S3_REGION_NAME=ru-central1`; можно указать любой другой S3-совместимый провайдер). Файлы
  получают постоянный публичный URL от самого хранилища — persistent volume/nginx-проксирование
  `/media/` для этого не нужны вообще.

Переменные — см. `.env.example` (`AWS_STORAGE_BUCKET_NAME`, `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`, `AWS_S3_ENDPOINT_URL`, `AWS_S3_REGION_NAME`, опционально
`AWS_S3_CUSTOM_DOMAIN`). Бакет должен быть настроен на публичное чтение объектов (bucket policy) —
`AWS_QUERYSTRING_AUTH=False` в коде намеренно отдаёт обычные постоянные ссылки, не подписанные
presigned-URL с истечением. Создание бакета и ключей доступа — вне этого репозитория.

---

## Быстрый запуск (Docker — рекомендуемый способ)

Требования: Docker, Docker Compose.

**1. Получи токен ngrok** (бесплатно): https://dashboard.ngrok.com/get-started/your-authtoken

**2. Настрой окружение:**

```bash
cp .env.example .env
# Заполни в .env:
#   NGROK_AUTHTOKEN=<твой токен ngrok>
#   SERVICE_API_TOKEN=<любая строка — ключ доступа к API>
#   AI_NORMALIZATION_API_KEY=<ключ AI-провайдера если нужна нормализация>
#   DEMO_TELEGRAM_CHAT_ID=<твой Telegram chat_id для демо>
```

**3. Запусти всё одной командой:**

```bash
docker compose up
```

Поднимается автоматически: PostgreSQL, Redis, Django (gunicorn), Celery-воркер, ngrok-туннель.  
При первом старте entrypoint выполняет `migrate` и `seed_demo_data`.

### Локальная привязка Telegram-чата уведомлений

Это отдельный бот уведомлений; его процесс запускается только по запросу. Укажи токен этого бота в
`NOTIFICATION_TELEGRAM_BOT_TOKEN` локального `.env` (не используй токен бота поставщика). Затем собери
образ и запусти сервис:

```bash
docker build -f docker/Dockerfile.bot -t autosnab-notifications-bot:local .
docker compose --profile notifications up -d --no-deps --no-build notifications-bot
```

В Swagger создай одноразовый токен через `POST /api/v1/notifications/link-tokens`, затем открой
`https://t.me/<имя_бота>?start=<токен>` в личном чате. Для группы создай токен с `destination_type=group`,
добавь бота в группу и открой ссылку с параметром `startgroup`. Успешный ответ бота означает, что
реальный chat ID сохранён в локальном backend. Для новой привязки создай новый токен. Отключить процесс:
`docker compose --profile notifications stop notifications-bot`.

Чтобы кнопка «Принять частично» открывала короткую форму, опубликуй фронтенд по публичному HTTPS-адресу
и настрой в BotFather Main Mini App этого бота на страницу
`https://<адрес-фронтенда>/mini-app/supplier-request`. В `.env` backend задай
`NOTIFICATION_TELEGRAM_MINI_APP_LINK=https://t.me/<имя_бота>?startapp=` и пересобери/перезапусти backend
и бота. Бот должен быть администратором группового чата поставщика, чтобы проверять, что ответ отправляет
участник именно этого чата. Без публичной HTTPS-страницы Telegram не сможет открыть Mini App.

### Уведомления Telegram и MAX на сервере

Уведомления работают отдельными процессами и используют общую логику backend:

- `notifications-worker` — отправляет уведомления и повторяет неудачные доставки;
- `beat` — запускает периодические задачи, включая ежедневный чек-лист поставок;
- `notifications-bot` — принимает подключения и события Telegram;
- `max-notifications-bot` — принимает webhook MAX, подключает личные и групповые чаты и обрабатывает кнопки MAX.

В production Compose эти сервисы описаны в `docker/docker-compose.yml`. При деплое они входят в
`DEPLOY_SERVICES` в `.gitlab-ci.yml`. Все процессы обращаются к backend внутри сети Compose по адресу
`http://app:8000/api/v1`; отдельный публичный URL backend для ботов не нужен.

Для Telegram укажи в серверном `.env` токен отдельного бота уведомлений:

```dotenv
NOTIFICATION_TELEGRAM_BOT_TOKEN=<токен Telegram-бота уведомлений>
```

Для MAX укажи токен бота для входящих событий и отправки сообщений:

```dotenv
MAX_BOT_TOKEN=<токен MAX-бота>
NOTIFICATION_MAX_BOT_TOKEN=<токен MAX-бота>
MAX_BOT_API_BASE=https://platform-api2.max.ru
MAX_NOTIFICATIONS_WEBHOOK_PATH=webhooks/max/notifications
MAX_WEBHOOK_SECRET=<необязательный секрет webhook>
MAX_DEPLOY_PORT=8090
```

Публичный reverse proxy должен направлять HTTPS-адрес MAX на порт `8090` контейнера:

```text
https://<домен>/webhooks/max/notifications -> http://127.0.0.1:8090/webhooks/max/notifications
```

После публикации адреса зарегистрируй webhook MAX из каталога `autosnab_bots`:

```bash
MAX_PUBLIC_WEBHOOK_URL=https://<домен>/webhooks/max/notifications \
python autosnab_bots/register_max_webhook.py
```

При запуске через deployment Compose сервисы можно проверить так:

```bash
docker compose -f docker/docker-compose.yml ps
docker compose -f docker/docker-compose.yml logs --tail=100 notifications-worker
docker compose -f docker/docker-compose.yml logs --tail=100 max-notifications-bot
```

Подключение личного или группового чата выполняется из кабинета уведомлений. Для Telegram группе нужен
числовой ID вида `-100...`; для MAX события приходят через зарегистрированный webhook. История действий и
настройки событий сохраняются в backend.

**4. Получи публичный URL для n8n:**

Открой http://localhost:4040 — там ngrok dashboard с публичным HTTPS URL вида `https://xxxx.ngrok-free.app`.

**5. Настрой n8n workflow:**

- Импортируй `AutoSnab Supplier Price Upload MVP - Chunked AI v.3.json` в свой аккаунт n8n
- В credentials n8n задай `BACKEND_URL`, `TELEGRAM_BOT_TOKEN`, `SERVICE_API_TOKEN`
- Активируй workflow

**6. Проверь:**

```bash
curl -s http://localhost:8000/api/v1/health
# {"status":"ok"}
```

---

## Локальный запуск без Docker

Требования: Python 3.12, Poetry, Docker (для PostgreSQL и Redis).

```bash
cp .env.example .env
docker compose up -d db redis
poetry install
poetry run python manage.py migrate
poetry run python manage.py seed_demo_data
poetry run python manage.py runserver 0.0.0.0:8000
```

Для локального просмотра кабинетов, товарной матрицы, корзины и тендера запусти
демонстрационный набор (команда доступна только при `DEBUG=True`):

```bash
poetry run python manage.py seed_local_demo_data
```

Команда выведет локальные учётные данные для `/venue/login` и `/supplier/login`.
Все созданные товары и цены синтетические; повторный запуск обновляет этот набор.

Celery-воркер (нужен для async AI-нормализации):

```bash
poetry run celery -A config worker -Q ai_normalization -l info
```

---

## Аутентификация

В системе три независимых способа авторизации — используются вместе, не взаимоисключающие:

**1. `SERVICE_API_TOKEN`** — для сервисных вызовов (n8n, бот, демо-скрипты):

```http
Authorization: Bearer <SERVICE_API_TOKEN>
```

Если `SERVICE_API_TOKEN` не задан в env — все service-only endpoints закрыты.

**2. JWT (`rest_framework_simplejwt`)** — для реальных пользователей, два независимых типа токенов:

- **`core.User`** (claim `user_id`) — единая модель и для поставщика, и для заведения/закупщика, отличаются только `role`:
  - Поставщик — `POST /api/v1/auth/supplier/register` + `/supplier/login` (email+пароль, с подтверждением по email).
  - Заведение/закупщик — `POST /api/v1/auth/venue/register` + `/venue/login` (email+пароль, самостоятельная регистрация, как у Ozon-покупателя), **или** аккаунт создаёт администратор напрямую через Django admin (классический вариант для сотрудников) — вход всё равно по email через тот же `/venue/login` (у `core.User` email обязателен и уникален для всех ролей). Первый admin — разовый `POST /api/v1/auth/venue/bootstrap` (под `SERVICE_API_TOKEN`).
  - Используется вишлистами, избранным, заявками заведения, списком/созданием компаний-поставщиков; для каталога (`GET /catalog/products` и справочники) — опционален, только для персонализации (см. «Каталог для заведений» выше — сам просмотр публичный).
  - `core.User.venue` (nullable FK, one user = one venue) — какому заведению принадлежит `role=venue_user` аккаунт; `admin` не привязан, работает с любым `venue_id`. Назначается пока только вручную через Django admin. `GET /api/v1/auth/me` возвращает `venue_id`/`venue_name` (оба `null`, если ещё не назначено).
- **`suppliers.Supplier`** (кабинет поставщика, claim `supplier_id`) — выдаётся двумя путями: `POST /api/v1/suppliers/cabinet/session` (обмен одноразового токена от бота) или `POST /api/v1/suppliers/cabinet/session-from-login` (обмен уже имеющегося `user_id`-JWT — веб-логин/OAuth). Используется **только** `GET/PATCH /api/v1/suppliers/cabinet/me` и другими `/suppliers/cabinet/*` — этот токен **не подходит** для `/auth/me` и остальных `core.User`-ручек (`InvalidToken`, нет claim `user_id`), и наоборот, `user_id`-JWT не подходит для `/suppliers/cabinet/*`. Это два независимых токена, а не апгрейд/замена друг друга — фронт должен хранить оба одновременно, если нужны обе группы ручек в рамках одной сессии.
- Обновление токена (оба типа): `POST /api/v1/token/refresh` — но обновляет только тот тип, чей `refresh` ему передан (обновлённый `user_id`-токен не станет `supplier_id`-токеном и наоборот).

```http
Authorization: Bearer <access>
```

**3. «Войти через Яндекс»** (`GET /api/v1/auth/yandex/login?intent=supplier|venue` → `GET /api/v1/auth/yandex/callback`) — дополнительный, не заменяющий способ входа поверх пункта 2: при первом входе self-service создаёт активный аккаунт (email подтверждён Яндексом), при повторном — логинит уже привязанный. Требует `YANDEX_OAUTH_CLIENT_ID`/`YANDEX_OAUTH_CLIENT_SECRET` в env (см. `.env.example`). Callback редиректит браузер на `YANDEX_OAUTH_FRONTEND_CALLBACK_URL` с `access`/`refresh` (или `error`) в query.

**4. «Войти через VK ID»** (`GET /api/v1/auth/vk/login?intent=supplier|venue` → `GET /api/v1/auth/vk/callback`) — тот же паттерн и intent'ы, что и Яндекс (п.3), аддитивно к обоим. VK ID — OAuth 2.1, требует PKCE даже на сервере: `code_verifier` генерируется на шаге login и хранится в той же подписанной httponly-куке, что и anti-CSRF nonce (без внешнего кэша/Redis). Требует `VK_OAUTH_CLIENT_ID`/`VK_OAUTH_CLIENT_SECRET` в env (см. `.env.example`) — редирект-URI должен быть зарегистрирован в кабинете VK ID 1-в-1 (полный путь `.../api/v1/auth/vk/callback`, не просто домен). Email обязателен (VK ID отдаёт его в `id_token`) — без разрешённого доступа к почте вход завершится ошибкой, как и у Яндекса.

---

## Нормализация прайсов

Backend поддерживает два режима нормализации:

**Основной (backend-owned):** `POST /uploads/{id}/normalize`

```json
{
  "use_ai": true,
  "ai_mode": "ambiguous",
  "run_async": true
}
```

- `ai_mode: "ambiguous"` — AI обрабатывает только неоднозначные строки (быстрее, дешевле)
- `ai_mode: "full"` — AI обрабатывает все строки (ограничение: 200 строк без `confirm_full: true`)
- `run_async: true` — задача уходит в Celery (ответ 202)

**Поддерживаемые форматы файлов:** XLSX, CSV, PDF (pdfplumber), DOCX, TXT, JPG/PNG (OCR)

---

## Demo flow

```bash
export BASE_URL="http://localhost:8000"
export SERVICE_API_TOKEN="change-me"

# ID демо-поставщика
export SUPPLIER_ID="$(poetry run python manage.py shell -c \
  "from suppliers.models import Supplier; print(Supplier.objects.get(display_name='Демо Поставщик').id)")"

# Загрузить текстовый прайс
export UPLOAD_ID=$(curl -s -X POST "$BASE_URL/api/v1/suppliers/uploads/text" \
  -H "Authorization: Bearer $SERVICE_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"supplier_id\":\"$SUPPLIER_ID\",\"source_channel\":\"api\",\"text\":\"Молоко кокосовое AROY-D 400мл 185.50 руб шт\"}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

# Запустить нормализацию (async)
curl -s -X POST "$BASE_URL/api/v1/suppliers/uploads/$UPLOAD_ID/normalize" \
  -H "Authorization: Bearer $SERVICE_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"use_ai": true, "run_async": true}'

# Статус
curl -s "$BASE_URL/api/v1/suppliers/uploads/$UPLOAD_ID" \
  -H "Authorization: Bearer $SERVICE_API_TOKEN"

# Каталог с фильтрами
curl -s "$BASE_URL/api/v1/catalog/products?availability=in_stock&sort=price_asc" \
  -H "Authorization: Bearer $SERVICE_API_TOKEN"
```

---

## Диагностика

| Код | Причина |
|-----|---------|
| `403` | Отсутствует или неверен `SERVICE_API_TOKEN` |
| `400` | Неверный запрос: UUID, decimal, тип файла, схема payload |
| `404` | Не найден объект по ID |
| `409` | Конфликт состояния (например, добавление позиции в неdraft заказ) |
| `needs_review` | Низкая уверенность, неполные данные или неизвестная единица |

```bash
# Проверка перед изменениями
poetry run python manage.py check
poetry run python manage.py test
poetry run python manage.py makemigrations --check --dry-run
poetry run python manage.py spectacular --validate
```

Подробный внутренний контракт для n8n: `docs/wiki/integrations/n8n-backend-usage-guide.md`
