# Контракт Yandex Cloud-деплоя

Статус: once-композиция и stdlib local/YC logger реализованы. Облачный handler,
ZIP и deploy workflow пока не реализованы; опубликованного облачного запуска
zontcontrol нет. Далее описаны требования согласованного
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
его federated credential для master, секрет z_token с ключом z_token и таймер
zontcontrol-15m. Реальные IDs новых ресурсов фиксируются после создания.
Cloud/folder, bucket autorepeater-deploy, существующая OIDC federation,
runtime SA/invoker и default log group используются общие, согласно таблице
в плане. Функцию, deploy SA, секрет t_token и таймер autorepeater не менять.
Объекты ZIP имеют отдельный префикс functions/zontcontrol/ и уникальный ключ
по SHA/run ID/attempt. Проверять manifest, происхождение SHA и SHA256 ZIP.

Новый deploy SA получает functions.admin и storage.editor на общем folder,
iam.serviceAccounts.user на invoker. Runtime SA получает lockbox.payloadViewer
на новый секрет. Статические IAM-ключи не создаются. Владелец добавляет
значение z_token в Lockbox; агент читает только metadata, никогда payload.
Runtime получает его через secret binding в ZONT_TOKEN.

Планируемые repository variables: CLOUD_ID, FOLDER_ID, SA_ID, FUNCTION_ID,
FUNCTION_SA_ID, BUCKET, LOCKBOX_SECRET_ID, LOCKBOX_VERSION_ID,
GITHUB_OIDC_AUDIENCE, ZONT_CONTACT. Runtime, entrypoint, 256MB/420s/concurrency1,
LOCKBOX_KEY=z_token и LOCKBOX_ENVIRONMENT_VARIABLE=ZONT_TOKEN фиксируются
в workflow. [Пример](../../deploy/yandex.example.json) пока отражает прежний
эскиз и будет обновлён вместе с deploy workflow; он не источник настройки.

## Gate и публикация

Деплой допускается только для текущего проверенного master gorpsys/zontcontrol:
workflow_run успешного Project checks push-master или workflow_dispatch master.
Требовать Lint и Offline checks от доверенного .github/workflows/checks.yml,
проверять производителя checks/statuses, SHA и origin; PR/fork, другой branch
и устаревший SHA отклонять до credentials. Повторить gate непосредственно
перед OIDC. Checkout закреплён на проверенном SHA. Очередь deploy-prod:
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

## Учётные данные и диагностика

Все cloud операции выполняются через yc 1.40.0 с --retry 0; IAM OIDC exchange —
через HTTPS. Явный временный config/profile содержит endpoint/cloud/folder,
без credentials; не переопределять HOME и не читать пользовательские конфиги.
IAM передаётся только в YC_IAM_TOKEN окружения дочернего процесса, не в argv,
config или logs. Временные файлы удаляются при успехе и ошибке. Не повторять
запись с неизвестным исходом: проверить чтением или завершить deploy ошибкой.

Локальный logger настраивается только application.main для обоих CLI-входов.
Импорт библиотеки и create_application не настраивают logger; будущий cloud
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
