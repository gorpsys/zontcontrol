# Контракт Yandex Cloud-деплоя

Статус: once-композиция, stdlib local/YC logger, облачный handler, профиль,
минимальный ZIP и deploy workflow реализованы и проверяются офлайн.
Отдельные deploy SA, OIDC credential, функция и metadata секрета созданы;
IAM/OIDC проверены отдельными согласованными чтениями 20.10.2026.
Факты и ID — в [отчёте provisioning](../reports/2026-10-20-yandex-cloud-provisioning.md).
После сообщения владельца metadata секрета проверены отдельным согласованным
чтением: секрет ACTIVE, версия e6qi0n3ucqk2stmtdalb ACTIVE, ключ z_token есть.
Repository variables не заполнены; версии кода, таймера и облачной приёмки пока нет.
Далее — контракт согласованного
[плана](../plans/20261020165020-yandex-cloud-boiler-reset-implementation-plan.md),
заменившего прежний [эскиз](../plans/yandex-cloud.md).

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

Плановые настройки: python312, handler.handler, 256MB, concurrency=1,
platform timeout=420s, once timeout=300s, HTTP timeout=15s. Запас для cleanup
не является доказанной верхней границей задержек ZONT. Жёсткое завершение
function/хоста вне гарантии finally. Координация приборов, watchdog и журнал
восстановления не добавляются; concurrency не даёт глобальной монополии.

## Раздельные и общие ресурсы

Отдельные ресурсы: закрытая функция zontcontrol, deploy SA ci-cd-zontcontrol-sa,
его federated credential для master и metadata секрета z_token созданы.
Deploy SA — aje715nu0u832o93e0hd, credential — aje80bckog9ld78ondm9,
функция — d4e9enfbuhk2h59a0fj0, секрет — e6q7k3r798m1od3jc647.
Metadata версии e6qi0n3ucqk2stmtdalb с ключом z_token подтверждены; payload не читался.
Таймер zontcontrol-15m пока не создаётся.
Cloud/folder, bucket autorepeater-deploy, существующая OIDC federation,
runtime SA/invoker и default log group используются общие, согласно таблице
в плане. Функцию, deploy SA, секрет t_token и таймер autorepeater не менять.
Объекты ZIP имеют отдельный префикс functions/zontcontrol/ и уникальный ключ
по SHA/run ID/attempt. Проверять manifest, происхождение SHA и SHA256 ZIP.

Новый deploy SA получил functions.admin и storage.editor на общем folder,
iam.serviceAccounts.user на invoker; на общем runtime SA добавлен binding
для нового deploy SA. Runtime SA получил lockbox.payloadViewer на новый
секрет. Все четыре роли проверены readback. Статические IAM-ключи не создаются.
Владелец добавляет
значение z_token в Lockbox; агент читает только metadata, никогда payload.
Runtime получает его через secret binding в ZONT_TOKEN.

Обязательные repository variables: CLOUD_ID, FOLDER_ID, SA_ID, FUNCTION_ID,
FUNCTION_SA_ID, BUCKET, LOCKBOX_SECRET_ID, LOCKBOX_VERSION_ID,
GITHUB_OIDC_AUDIENCE, ZONT_CONTACT. Runtime, entrypoint, 256MB/420s/concurrency1,
LOCKBOX_KEY=z_token и LOCKBOX_ENVIRONMENT_VARIABLE=ZONT_TOKEN фиксируются
в workflow. [Пример](../../deploy/yandex.example.json) отражает этот набор;
он не читается helper и не является вторым источником настройки. Подтверждённые
ID ресурсов и версии секрета внесены в пример; ZONT_CONTACT остаётся null,
repository variables ещё не настроены. Подстановка ID образца запрещена.
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

OIDC subject: repo:gorpsys/zontcontrol:ref:refs/heads/master, без GitHub
environment. GitHub AUD — https://github.com/gorpsys; IAM exchange audience —
новый deployment SA_ID. Эти значения не взаимозаменяемы. Отсутствующие или
неверные обязательные параметры отклонять до запроса credentials.

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
успех возможен только при однозначном совпадении. После неизвестного upload
helper читает head-object и останавливается: HEAD не доказывает хеш всех байтов.

## Учётные данные и диагностика

Все cloud операции выполняются через yc 1.40.0 с --retry 0; IAM OIDC exchange —
через HTTPS. Явный временный config/profile содержит endpoint/cloud/folder,
без credentials; не переопределять HOME и не читать пользовательские конфиги.
IAM передаётся только в YC_IAM_TOKEN окружения дочернего процесса, не в argv,
config или logs. Временные файлы удаляются при успехе и ошибке. Не повторять
запись с неизвестным исходом: проверить чтением или завершить deploy ошибкой.

Локальный logger настраивается только application.main для обоих CLI-входов.
Импорт библиотеки и create_application не настраивают logger; cloud
handler сам вызывает configure_yc_logging. В облачном режиме — JSON stdout,
INFO, propagate=False, один собственный handler, сохранение чужих handlers.
Поля: message/level/logger и безопасные event/request_id/algorithm_id/device_id/
state/reset_performed/elapsed_ms. WARNING → WARN, CRITICAL → FATAL. Произвольные
extras и traceback не сериализуются. Сообщения событий фиксированные, без
текста исключений, payload event, конфигов, API-ответов и credentials.
Успех off API не объявляется физическим восстановлением котла.

## Ручные действия и приёмка

Каждая ручная команда yc, включая чтение результата и повтор после ошибки,
требует предварительного подтверждения владельца с точным действием и целью.
Согласованный автоматический workflow выполняется без подтверждения каждой
команды. Подтверждение плана не заменяет разрешения ручных запросов.

Таймер не создаётся до заполнения секрета, успешного первого деплоя и
отдельного подтверждения владельца на создание и включение плановых реальных
сбросов. No-op версия и предварительный pause/resume не нужны. Таймер v2
вызывает новую функцию $latest через invoker: cron 0/15 * ? * * *,
retry_attempts=1, interval=10s, как у образца. При публикации версии флаг
--async-max-retries явно не задаётся; это не отключение повторов платформы.
Повтор таймера заново выполняет весь once и может инициировать новый сброс;
автоматических повторов отдельных команд котлу нет.

После включения нужны два наблюдаемых плановых запуска примерно через
15 минут: один once на вызов, завершение/cleanup, безопасные логи без CLI и
дублированных handlers. Не создавать искусственную аварию или ручной импульс
для диагностики. До выполнения внешних шагов цель не достигнута; их нельзя
отметить выполненными как skipped/manual-not-automatable. Подтверждённые
факты записываются в docs/reports; README описывает только готовый запуск.
