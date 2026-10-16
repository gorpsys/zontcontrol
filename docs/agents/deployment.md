# Контракт Yandex Cloud-деплоя

Статус: требования и план. Облачный handler, упаковка функции и workflow
деплоя ещё не реализованы; опубликованного облачного запуска zontcontrol нет.
Порядок подготовки — [план](../plans/yandex-cloud.md).
Жизненный цикл once/continuous и завершение импульса описаны
в [реализованной локальной архитектуре](architecture.md). Continuous требует другого runtime,
а boiler_reset использует finally при штатной остановке/исключениях.
Приложение работает одним процессом с потоками, остановка кооперативная.
Жёсткое завершение приложения/function вне выбранной гарантии; независимый
исполнитель или журнал для этого не требуются.

## Что переносится из autorepeater

Yandex Cloud Functions, сборка ZIP из проверенного SHA, GitHub Actions,
короткоживущий IAM через GitHub OIDC и секреты runtime из Lockbox.
Production-деплой — из текущего master целевого репозитория после проверок
этого SHA; PR/fork, другой branch и устаревший SHA не допускаются.
Первое включение выполняется вручную после настройки и проверки ресурсов.
После готовности можно добавить автоматический запуск по успешному CI master.

Проверять происхождение checks, актуальный SHA и доверенный workflow path;
повторить gate непосредственно перед credentials. Собирать и выкатывать
один и тот же SHA. Общая очередь deploy-prod, cancel-in-progress=false,
queue=max; не терять промежуточные ожидающие задания. Checkout закреплён
на проверенном SHA, повторный gate выполняется непосредственно перед OIDC.
Ключ объекта архива уникален по SHA/run ID/attempt; проверять SHA-256.
Аннотированный release-тег не запускает деплой сам по себе.
Workflow Release tag — отдельный механизм версии исходников с собственной
очередью и минимальными правами; его успех не заменяет gate/приёмку деплоя.

## Раздельные параметры и полномочия

Все ресурсы zontcontrol задаются заново: repository, cloud/folder IDs,
deployment SA, runtime SA, federation ID, bucket, функция и Lockbox secret.
Шаблон — [deploy/yandex.example.json](../../deploy/yandex.example.json).
null означает не настроено; файл пока описывает план, а не CLI-конфиг.
Не подставлять ID ресурсов autorepeater ради успешного запуска.

Планируемые repository variables: CLOUD_ID, FOLDER_ID, SA_ID (deployment),
RUNTIME_SA_ID, FUNCTION_NAME, FUNCTION_RUNTIME, FUNCTION_ENTRYPOINT,
FUNCTION_MEMORY, FUNCTION_TIMEOUT, DEPLOY_BUCKET, LOCKBOX_SECRET_ID,
LOCKBOX_SECRET_VERSION_ID, LOCKBOX_SECRET_KEY, GITHUB_OIDC_AUDIENCE.
Federation ID требуется при настройке доверия, не является секретом.
Секрет ZONT_TOKEN хранится только в Lockbox; env runtime ссылается на него.
ZONT_CONTACT и идентификаторы ZONT задаются отдельно от облачных ресурсов.

OIDC subject: repo:<целевой owner/repo>:ref:refs/heads/master.
Не копировать subject autorepeater. План не использует GitHub environment:
его добавление изменяет subject и требует явного изменения федерации.
GITHUB_OIDC_AUDIENCE должен соответствовать AUD федерации; IAM exchange
audience — deployment SA_ID. Эти значения не взаимозаменяемы; отсутствие
AUD должно завершать работу до запроса credentials.

Deployment SA: ограниченные права публикации функции/объекта и право
использовать runtime SA (iam.serviceAccounts.user или эквивалент).
Runtime SA: доступ к нужному секрету Lockbox, без прав deployment SA.
Не удалять secret и не заменять runtime SA при ошибке IAM. Постоянный
облачный ключ в GitHub Secrets не требуется для планируемого OIDC.

## Сохранение состояния функции

Python 3.12, начальные ресурсы 256MB/60s; точные параметры проверить на
read-only приёмке и изменить при необходимости. Будущая точка входа —
handler.handler. Scheduler, если понадобится, вызывает разовую оценку.
Первый handler только читает данные и возвращает решение; привилегии
автоматического управления и циклы сброса в этот этап не входят.

До upload прочитать действующие настройки, сохранить env, runtime SA,
Lockbox, сеть и поддерживаемые параметры либо остановиться при неизвестных
полях. Read-only проверка после публикации: ACTIVE, version ID, SHA,
runtime, entrypoint, память, таймаут, SA и ссылка на нужный secret.
ACTIVE не доказывает успешный Python cold start; import-only приёмка
проверяется отдельно. Production handler не используют как диагностику,
когда он получает полномочия управлять отоплением.

## Учётные данные и ошибки

Будущая обвязка yc использует временный профиль/config, явные endpoint и
cloud/folder. IAM передаётся только через YC_IAM_TOKEN окружения дочернего
процесса; не через argv, не записывается в config и не логируется. Версию yc
нужно закрепить и проверить; autorepeater использует 1.40.0, но здесь CLI
ещё не установлен/проверен. Удалять временные файлы при успехе и ошибке.
Не читать пользовательские конфиги yc или другие credentials.

Показывать безопасную категорию ошибки и проверенные request/trace UUID;
произвольные stdout/stderr CLI и ответы провайдера не выводить. Успех или
ошибку выкладки фиксировать в docs/history; README описывает облачный запуск
лишь после подтверждённой реализации. Резерв SDK/vendor autorepeater не нужен:
у zontcontrol runtime не имеет сторонних зависимостей.
