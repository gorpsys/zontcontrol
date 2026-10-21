# Контракт Yandex Cloud-деплоя

Настройка с нуля и перенос в другой репозиторий —
[пошаговое how-to](../reference/github-yandex-cloud-howto.md).

Этот документ задаёт обязательные проверки и границы полномочий.
ID и имена ресурсов с датой и пределом подтверждения —
[реестр](../reference/yandex-cloud-resources.md). Создание, попытки публикации
и исправление OIDC сохранены в [provisioning](../reports/2026-10-20-yandex-cloud-provisioning.md),
наблюдаемые вызовы и измерения — в [приёмке](../reports/2026-10-21-yandex-cloud-acceptance.md).
Датированные отчёты не подтверждают нынешнее состояние облака.

## Цель и границы

После успешных проверок текущего master GitHub Actions автоматически публикует
его ZIP в отдельную закрытую функцию zontcontrol. Таймер раз в 15 минут
запускает только boiler_reset once: история, существующий детектор и настоящий
40-секундный импульс при текущем confirmed_pattern. CLI, continuous, получение
токена и исследовательские программы в ZIP не входят. Runtime — stdlib.

Controller API v2, профиль и пороги детектора, on/40 секунд/finally off и
[локальный lifecycle](architecture.md) сохраняются. Каждый вызов создаёт
свежий Application с явным once-каталогом BOILER_RESET_SPEC, один раз вызывает
run_once('boiler_reset', timeout=300), в finally ждёт worker/cleanup, затем
закрывает транспорт. Event не выбирает устройство, алгоритм или режим.
Облачные config/*.json формируются из отдельного deploy/cloud-config профиля,
не из рабочих локальных конфигов. Контакт поступает при сборке из ZONT_CONTACT.

Фиксированные настройки: python312, handler.handler, 256MB, concurrency=1,
platform timeout=420s, once timeout=300s, HTTP timeout=15s. Запас для cleanup
не является доказанной верхней границей задержек ZONT. Жёсткое завершение
function/хоста вне гарантии finally. Координация приборов, watchdog и журнал
восстановления не добавляются; concurrency не даёт глобальной монополии.

## Раздельные и общие ресурсы

Функция, deploy SA, credential для master, секрет и таймер zontcontrol
отделены от исходных ресурсов autorepeater. Cloud/folder, bucket, OIDC
federation, runtime SA/invoker и default log group используются общие;
точные ID и имена — в [реестре](../reference/yandex-cloud-resources.md).
Функцию, deploy SA, секрет и таймер autorepeater не менять.
Публикация версии сама по себе не разрешает включать отключённый таймер.
Объекты ZIP имеют отдельный префикс functions/zontcontrol/ и уникальный ключ
по SHA/run ID/attempt. Проверять manifest, происхождение SHA и SHA256 ZIP.

Deploy SA нужны functions.admin и storage.editor на общем folder,
iam.serviceAccounts.user на runtime SA. Runtime SA нужен lockbox.payloadViewer
на отдельный секрет; invoker — право вызова закрытой функции.
Bootstrap назначает bindings адресно, не заменяя остальные policies.
Статические IAM-ключи не создаются. Владелец добавляет значение z_token
в Lockbox; агент читает только metadata, никогда payload.
Runtime получает его через secret binding в ZONT_TOKEN.

Обязательные repository variables: CLOUD_ID, FOLDER_ID, SA_ID, FUNCTION_ID,
FUNCTION_SA_ID, BUCKET, LOCKBOX_SECRET_ID, LOCKBOX_VERSION_ID,
YC_OIDC_AUDIENCE, ZONT_CONTACT. Runtime, entrypoint, 256MB/420s/concurrency1,
LOCKBOX_KEY=z_token и LOCKBOX_ENVIRONMENT_VARIABLE=ZONT_TOKEN фиксируются
в workflow. [Пример](../../deploy/yandex.example.json) отражает этот набор;
он не читается helper и не является вторым источником настройки. Контакт
в публикуемом примере null; при сборке требуется валидное значение.
Подстановка ID другого проекта запрещена. Адреса и pinned версия секрета
сверяются с [реестром](../reference/yandex-cloud-resources.md).
Префикс GITHUB_ запрещён GitHub для repository variables; workflow передаёт
YC_OIDC_AUDIENCE во внутренний env GITHUB_OIDC_AUDIENCE helper.
Общие cloud/folder/runtime SA/bucket и GitHub AUD проверяются по согласованным
значениям. Contact проверяется существующим валидатором контроллера.

## Gate и публикация

Деплой допускается только для текущего проверенного master gorpsys/zontcontrol:
workflow_run успешного Project checks push-master или workflow_dispatch master.
Требовать Lint и Offline checks от доверенного .github/workflows/checks.yml:
проверять GitHub Actions app ID/slug, head SHA, details URL и отдельный
workflow run (repo/head repo, path, push/master, SHA, success). Commit statuses
не могут заменить обязательные checks; права statuses:read не требуются.
PR/fork, другой branch и устаревший SHA отклонять до credentials. Повторить
gate непосредственно перед OIDC, в том числе внутри helper после локальной
проверки параметров и ZIP. Gate повторно читает master после checks.
Checkout закреплён на проверенном SHA. Очередь deploy-prod:
cancel-in-progress=false, queue=max. Release tag не запускает деплой сам.

OIDC subject должен совпадать с фактическим claim для master без GitHub
environment. Формат этого репозитория —
`repo:OWNER@OWNER_ID/REPOSITORY@REPOSITORY_ID:ref:refs/heads/master`;
точный subject и GitHub AUD приведены в [реестре](../reference/yandex-cloud-resources.md#oidc-и-iam).
Immutable ID владельца и репозитория нельзя убрать кастомизацией claims.
При переносе subject проверяют по реальным claims, как в how-to.
GitHub AUD — URL, IAM exchange audience — ID deploy SA.
Эти значения не взаимозаменяемы. Отсутствующие или неверные обязательные
параметры отклонять до запроса credentials.

Публиковать строго по FUNCTION_ID, проверив folder/functionId. Первая версия
создаётся только после успешного version list с пустым списком; ошибка чтения
не означает пустой список. Для существующих версий сохранить env, secret
bindings, runtime SA, logging и поддерживаемые настройки либо остановиться
при неизвестных полях. После публикации проверить $latest, ACTIVE, version ID,
SHA, runtime/entrypoint, ресурсы/timeout/concurrency, SA, log folder и secret
ID/version/key/env. ACTIVE само по себе не доказывает успешный запуск Python.
Создание функции, IAM и таймера в deploy workflow не входит.

Helper сохраняет env, прочие pinned secret bindings, tmpfs, network,
named service accounts, metadata endpoints и минимальный уровень логов;
runtime/entrypoint/memory/timeout/concurrency/runtime SA заданы явно.
Назначение логов — default log group общего folder. Иной журнал, отключённые
логи, mounts/async settings и неизвестные поля приводят к отказу до upload.
Непустой list с пустым/некорректным latest также запрещает bootstrap.
Description содержит SHA, SHA256 и уникальный ключ объекта. После неизвестного
исхода create этот provenance и новые ID/settings проверяются чтением latest;
успех возможен только при однозначном совпадении. После ошибки upload helper
немедленно останавливается, как в образце: без HEAD, повторов или создания версии.

## Учётные данные и диагностика

Все cloud операции выполняются через yc 1.40.0 с --retry 0; IAM OIDC exchange —
через HTTPS. Используются обычный путь --body без @,
dict(os.environ, YC_IAM_TOKEN=token), удаление YC_TOKEN из дочернего окружения,
явные --config/--profile/--folder-id и json-rest, timeout, --retry 0.
Endpoint/cloud/folder задаются в профиле; дублирующие CLI-флаги не нужны.
Явный временный config/profile содержит endpoint/cloud/folder,
без credentials; не переопределять HOME и не читать пользовательские конфиги.
IAM передаётся только в YC_IAM_TOKEN окружения дочернего процесса, не в argv,
config или logs. Временные файлы удаляются при успехе и ошибке. Не повторять
запись с неизвестным исходом: проверить чтением или завершить deploy ошибкой.
HTTP-ошибки различают фиксированные стадии GitHub checks API, GitHub OIDC и
Yandex IAM exchange. Ошибки yc содержат exit-код и категорию из allowlist,
без произвольного stderr/stdout. При отказе upload сохраняется его безопасная
причина; создание версии после неизвестного исхода загрузки запрещено.

Минимальный allowlist ZIP, manifest и офлайн once smoke обязательны:
[команды сборки](../../scripts/README.md#облачный-запуск-и-деплой).
Офлайн-тесты проверяют argv/env и чтение файла по --body подставным subprocess;
они не доказывают семантику настоящего yc или успешный deploy.

Локальный logger настраивается только application.main для обоих CLI-входов.
Импорт библиотеки и create_application не настраивают logger; cloud
handler сам вызывает configure_yc_logging. В облачном режиме — JSON stdout,
INFO, propagate=False, один собственный handler, сохранение чужих handlers.
Поля: timestamp (aware UTC), message/level/logger и безопасные event/request_id/algorithm_id/device_id/
state/reset_performed/elapsed_ms.
WARNING → WARN, CRITICAL → FATAL. Произвольные
extras и traceback не сериализуются. Сообщения событий фиксированные, без
текста исключений, payload event, конфигов, API-ответов и credentials.
Успех off API не объявляется физическим восстановлением котла.
Handler включает capture только для текущего invocation:
tee Python stdout/stderr передаёт исходный текст и в исходные потоки, и в общий
буфер, без отдельного отбора для ответа. Существующие StreamHandler этих
потоков временно перенаправляются в tee, включая root/private логгеры с
propagate=False; форматы и уровни не меняются, дополнительного root handler нет.
После worker/finally/off и close строка log передаётся в ответ; finally
восстанавливает sys/handler streams, включая созданные внутри вызова, и
закрывает буфер. Тёплые вызовы не делят память. Log может содержать JSONL и
обычный текст. Прямой FD/native вывод, файловые handlers и платформенные
START/END вне процесса не захватываются. Данные результата алгоритма
возвращаются целиком; сериализация не зависит от событий/причин его бизнеса.
При ошибке RuntimeError содержит безопасный JSON с категорией, завершённым
результатом либо null и логом; исключение и повторы платформы сохраняются.

Handler сохраняет этот же полный log в notes устройства
из load_config boiler_reset. Прежнюю историю читает через публичный
read_notes после stop(wait_timeout=None), до close основного Application.
Полный основной лог, включая shutdown, фиксируется до публикации. Для записи
используется отдельный create_controller без регистратора и алгоритмов;
write_notes отправляет только notes и не меняет name. Публикация не пишет
собственных success-логов: это отдельная служебная операция после основного лога.
JSON-массив содержит новый log первым и два предыдущих; логи не разбираются
по бизнесовым событиям, не фильтруются и не обрезаются. Чужой/повреждённый формат
notes не затирается; конкурентная ротация может потерять запись, без CAS/мьютекса.
notes_saved/notes_error сообщают безопасный исход сохранения в ответе либо
JSON ошибки. Сбой notes не меняет исход алгоритма и не вызывает дополнительного
сброса; неизвестная запись не повторяется. Подтверждение записи API не означает
сохранность от последующего вмешательства другого клиента.

## Ручные действия и приёмка

Каждая ручная команда yc, включая чтение результата и повтор после ошибки,
требует предварительного подтверждения владельца с точным действием и целью.
Согласованный автоматический workflow выполняется без подтверждения каждой
команды. Подтверждение плана не заменяет разрешения ручных запросов.

Таймер не создаётся до заполнения секрета, успешного первого деплоя и
отдельного подтверждения владельца на создание и включение плановых реальных
сбросов. No-op версия и предварительный pause/resume не нужны. Таймер v2
вызывает функцию $latest через invoker; расписание и retry policy сверяются
с [реестром](../reference/yandex-cloud-resources.md#отдельные-ресурсы-и-привязки).
При публикации версии флаг
--async-max-retries явно не задаётся; это не отключение повторов платформы.
Повтор таймера заново выполняет весь once и может инициировать новый сброс;
автоматических повторов отдельных команд котлу нет.

После включения нужны два наблюдаемых плановых запуска примерно через
15 минут: один once на вызов, завершение/cleanup, безопасные логи без CLI и
дублированных handlers. Не создавать искусственную аварию или ручной импульс
для диагностики. До выполнения внешних шагов цель не достигнута; их нельзя
отметить выполненными как skipped/manual-not-automatable. Подтверждённые
факты записываются в docs/reports; README описывает только готовый запуск.
