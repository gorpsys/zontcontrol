# Команды yc для облачного запуска zontcontrol

Справочник к [плану](20261020165020-yandex-cloud-boiler-reset-implementation-plan.md).
Проверено 20.10.2026 по официальной документации и локальному `yc 1.40.0 --help`.
Изменения из примеров **не выполнялись**: ресурсы создаются при реализации.
Каждый ручной запрос, включая проверку результата, отдельно согласуется:
показать владельцу полную команду с подставленными ID и объяснить её цель.
Подтверждение плана или предыдущего чтения этого не заменяет.

## Общие параметры

Массив Bash сокращает примеры; его объявление не вызывает облако:

```bash
YC_SCOPE=(
  --endpoint api.cloud.yandex.net:443
  --cloud-id b1gbga988v36vq19lmb7
  --folder-id b1gdgrp5bnth11phova8
  --retry 0
)
```

`ZC_DEPLOY_SA_ID`, `ZC_FUNCTION_ID`, `ZC_SECRET_ID`, `ZC_SECRET_VERSION_ID`,
`ZC_TRIGGER_ID` заполняются только из подтверждённых ответов. Это ID, не
секреты. Общие ресурсы: invoker/runtime SA `ajelpbq7as5bpe499962`, федерация
`ajej9j37pt8j7ns962vr`, bucket `autorepeater-deploy`.

Ручные запросы используют установленную авторизацию без чтения её файлов.
Автоматический helper использует отдельные временные `--config`/`--profile`,
явные endpoint/cloud/folder и IAM только через `YC_IAM_TOKEN` окружения
дочернего процесса, без credentials в config/argv. Не менять HOME.
Повторы CLI отключены. Неопределённый исход записи проверяется отдельным
согласованным чтением. Не печатать произвольные stdout/stderr при ошибках;
допустимы безопасная категория и проверенный request/trace ID, без debug.

## 1. Проверка отсутствия новых ресурсов

Каждый блок — отдельный запрос. Найденные ресурсы автоматически не заменять.

```bash
yc "${YC_SCOPE[@]}" iam service-account list --format json \
  --jq 'map(select(.name == "ci-cd-zontcontrol-sa") | {id,name})'
```

```bash
yc "${YC_SCOPE[@]}" serverless function list --format json \
  --jq 'map(select(.name == "zontcontrol") | {id,name,folder_id})'
```

```bash
yc "${YC_SCOPE[@]}" lockbox secret list --format json \
  --jq 'map(select(.name == "z_token") | {id,name})'
```

```bash
yc "${YC_SCOPE[@]}" serverless trigger v2 list --format json \
  --jq 'map(select(.name == "zontcontrol-15m") | {id,name,status})'
```

Старый `serverless trigger list` неприменим: таймер образца — v2.

## 2. Отдельный deploy SA и OIDC

Создать аккаунт, сохранить ID и выдать три подтверждённые роли образца.
Runtime SA эти роли не получает.

```bash
yc "${YC_SCOPE[@]}" iam service-account create --name ci-cd-zontcontrol-sa \
  --format json --jq '{id,name,folder_id}'
```

```bash
yc "${YC_SCOPE[@]}" resource-manager folder add-access-binding \
  --id b1gdgrp5bnth11phova8 --role functions.admin \
  --subject "serviceAccount:${ZC_DEPLOY_SA_ID}"
```

```bash
yc "${YC_SCOPE[@]}" resource-manager folder add-access-binding \
  --id b1gdgrp5bnth11phova8 --role storage.editor \
  --subject "serviceAccount:${ZC_DEPLOY_SA_ID}"
```

```bash
yc "${YC_SCOPE[@]}" iam service-account add-access-binding \
  --id ajelpbq7as5bpe499962 --role iam.serviceAccounts.user \
  --subject "serviceAccount:${ZC_DEPLOY_SA_ID}"
```

```bash
yc "${YC_SCOPE[@]}" iam workload-identity federated-credential create \
  --service-account-id "$ZC_DEPLOY_SA_ID" --federation-id ajej9j37pt8j7ns962vr \
  --external-subject-id 'repo:gorpsys@79201964/zontcontrol@1420696677:ref:refs/heads/master' \
  --format json --jq '{id,service_account_id,federation_id,external_subject_id}'
```

Subject исправлен после первого deploy: GitHub использует immutable prefix
с ID владельца и репозитория. Подтверждённый новый credential —
ajehfh3q51b5i5q6frcr; старый aje80bckog9ld78ondm9 с subject без ID удалён
отдельно согласованной командой. Проверка и удаление также требуют отдельных
разрешений; команды выше не являются разрешением их повторять.

Отдельно согласовать чтения для проверки выданных ролей и subject:

```bash
yc "${YC_SCOPE[@]}" resource-manager folder list-access-bindings \
  --id b1gdgrp5bnth11phova8 --format json \
  --jq "map(select(.subject.id == \"${ZC_DEPLOY_SA_ID}\"))"
```

```bash
yc "${YC_SCOPE[@]}" iam service-account list-access-bindings \
  --id ajelpbq7as5bpe499962 --format json \
  --jq "map(select(.subject.id == \"${ZC_DEPLOY_SA_ID}\"))"
```

```bash
yc "${YC_SCOPE[@]}" iam workload-identity federated-credential list \
  --service-account-id "$ZC_DEPLOY_SA_ID" --format json \
  --jq 'map({id,service_account_id,federation_id,external_subject_id})'
```

## 3. Закрытая функция и пустой секрет

Создать только ресурс функции, без версии и без публичных bindings:

```bash
yc "${YC_SCOPE[@]}" serverless function create --name zontcontrol \
  --format json --jq '{id,name,folder_id,status}'
```

У invoker уже есть `functions.functionInvoker` на folder.

```bash
yc "${YC_SCOPE[@]}" lockbox secret create --name z_token \
  --format json --jq '{id,name,folder_id,kms_key_id,current_version}'
```

Без payload/password specification Secret/create создаёт только метаданные,
без версии значения. Собственный KMS-ключ не задаётся, как у образца.
Фиктивный токен не добавлять: реальный ключ `z_token` вводит владелец.

```bash
yc "${YC_SCOPE[@]}" lockbox secret add-access-binding \
  --id "$ZC_SECRET_ID" --role lockbox.payloadViewer \
  --subject serviceAccount:ajelpbq7as5bpe499962
```

Проверки, каждая после отдельного подтверждения:

```bash
yc "${YC_SCOPE[@]}" serverless function get --id "$ZC_FUNCTION_ID" \
  --format json --jq '{id,name,folder_id,status}'
```

```bash
yc "${YC_SCOPE[@]}" lockbox secret list-access-bindings --id "$ZC_SECRET_ID" \
  --format json --jq 'map(select(.subject.id == "ajelpbq7as5bpe499962"))'
```

## 4. После заполнения секрета владельцем

```bash
yc "${YC_SCOPE[@]}" lockbox secret get --id "$ZC_SECRET_ID" --format json \
  --jq '{id,name,status,current_version:(.current_version | {id,status,payload_entry_keys})}'
```

Требуются активная версия и ключ `z_token`. ID версии закрепить в
`LOCKBOX_VERSION_ID`. Payload не получать. Последующая ротация требует
обновления ID и новой публикации функции.

## 5. Публикация workflow через yc

Это аргументы согласованного helper, а не ручной обход проверок SHA/OIDC.
Первый deploy — после merge и успешного CI master. Автоматическому workflow
не нужны отдельные подтверждения команд. `json-rest` даёт REST/camelCase,
в отличие от `json`/snake_case ручных чтений выше.
До успешной публикации и проверки версии таймера не существует.

Сначала `function get --id` с проверкой ID/folder, затем:

```bash
yc "${YC_SCOPE[@]}" serverless function version list \
  --function-id "$ZC_FUNCTION_ID" --limit 1 --format json-rest
```

Пустой успешный список разрешает bootstrap; ошибка чтения — отказ.
Непустой список требует get-by-tag `$latest` и сохранения поддерживаемых
настроек до upload. Неизвестные настройки — отказ.

```bash
yc "${YC_SCOPE[@]}" storage s3api put-object --bucket autorepeater-deploy \
  --key "$ZC_OBJECT_KEY" --body @build/yandex-function.zip --format json-rest
```

`ZC_OBJECT_KEY=functions/zontcontrol/<SHA>/<run-id>-<attempt>.zip`;
`ZC_ZIP_SHA256` — SHA-256 байтов ZIP, `ZC_DEPLOY_SHA` — проверенный master.
Для первой версии:

```bash
yc "${YC_SCOPE[@]}" serverless function version create \
  --function-id "$ZC_FUNCTION_ID" --runtime python312 \
  --entrypoint handler.handler --memory 256MB --execution-timeout 420s \
  --concurrency 1 \
  --service-account-id ajelpbq7as5bpe499962 \
  --log-folder-id b1gdgrp5bnth11phova8 \
  --package-bucket-name autorepeater-deploy \
  --package-object-name "$ZC_OBJECT_KEY" --package-sha256 "$ZC_ZIP_SHA256" \
  --description "zontcontrol source_sha=${ZC_DEPLOY_SHA}" \
  --secret "environment-variable=ZONT_TOKEN,id=${ZC_SECRET_ID},version-id=${ZC_SECRET_VERSION_ID},key=z_token" \
  --format json-rest
```

Для обычного deploy helper добавляет сохранённые env/прочие secret bindings
и поддерживаемые настройки. Перечень полей и перенос фиксируются тестами.
Команда выше — bootstrap, а не полная замена действующих настроек.

Проверка использует get-by-tag, не `version get --function-id`:

```bash
yc "${YC_SCOPE[@]}" serverless function version get-by-tag \
  --function-id "$ZC_FUNCTION_ID" --tag '$latest' --format json \
  --jq '{id,function_id,status,runtime,entrypoint,resources,execution_timeout,concurrency,service_account_id,log_options,secrets,description,environment_keys:(.environment // {} | keys)}'
```

Helper сверяет version ID/function ID, ACTIVE, параметры и происхождение SHA.
Значения env не печатает. Production invoke как диагностику не использовать:
обработчик имеет право на настоящий сброс.

## 6. Создание таймера после первого успешного деплоя

Предварительные условия: владелец заполнил секрет, первый deploy успешно
завершён, версия и её параметры проверены. Отдельно подтвердить создание
таймера, пояснив, что он создаётся активным и разрешает реальные сбросы
по confirmed_pattern. Подготовительная пауза и no-op версия не нужны.

Временный `zc-trigger-action.json`: заменить `NEW_FUNCTION_ID` полученным
ID. Это один объект action, без списка и без `rule.timer`:

```json
{
  "invoke_function": {
    "function_id": "NEW_FUNCTION_ID",
    "function_tag": "$latest",
    "service_account_id": "ajelpbq7as5bpe499962"
  },
  "retry_policy": {"interval": "10s", "retry_attempts": "1"}
}
```

Как у образца, `retry_attempts=1` разрешает один повтор неуспешного вызова
через `interval=10s`. Это повтор всего once, который может снова привести
к сбросу. У версии функции --async-max-retries явно не задаётся, как в
деплое образца; отсутствие флага не означает отключения повторов платформы.
Шесть полей cron: минуты/часы/день месяца/месяц/день недели/год, UTC.

```bash
yc "${YC_SCOPE[@]}" serverless trigger v2 create timer \
  --name zontcontrol-15m --cron-expression '0/15 * ? * * *' \
  --action @zc-trigger-action.json --format json --jq '{id,name,status}'
```

После отдельного подтверждения прочитать результат:

```bash
yc "${YC_SCOPE[@]}" serverless trigger v2 get --id "$ZC_TRIGGER_ID" \
  --format json --jq '{id,name,status,source,action}'
```

Сверить новый ID функции, cron, invoker, retry_attempts=1, interval=10s
и активный статус;
сохранить ID/конфигурацию/время проверки. Таймер образца
`a1sb17vl05nregkjh2si` не менять.

Отдельно согласовать чтение двух плановых запусков:

```bash
yc "${YC_SCOPE[@]}" logging read --group-name default \
  --resource-ids "$ZC_FUNCTION_ID" --since 40m --limit 100 --format json
```

Показывать только безопасные поля логгера нашей функции, без неизвестных
сообщений целиком. Проверить разные request_id и одно начало/завершение once
на вызов, результат и длительность. Искусственный сброс для приёмки не делать.
Pause при проблеме тоже требует отдельного подтверждения.

## 7. Пауза и возобновление уже работающего таймера

Это эксплуатационные команды, не шаги первоначального запуска. Каждая
отдельно согласуется; resume вновь включает плановые реальные сбросы.

```bash
yc "${YC_SCOPE[@]}" serverless trigger v2 pause --id "$ZC_TRIGGER_ID"
```

```bash
yc "${YC_SCOPE[@]}" serverless trigger v2 resume --id "$ZC_TRIGGER_ID"
```

## Официальные источники

- [Function create](https://yandex.cloud/ru/docs/cli/cli-ref/serverless/cli-ref/function/create),
  [version create](https://yandex.cloud/ru/docs/cli/cli-ref/serverless/cli-ref/function/version/create),
  [version list](https://yandex.cloud/ru/docs/cli/cli-ref/serverless/cli-ref/function/version/list),
  [get-by-tag](https://yandex.cloud/ru/docs/cli/cli-ref/serverless/cli-ref/function/version/get-by-tag).
- [Trigger v2 timer](https://yandex.cloud/ru/docs/cli/cli-ref/serverless/cli-ref/trigger/v2/create/timer),
  [get](https://yandex.cloud/ru/docs/cli/cli-ref/serverless/cli-ref/trigger/v2/get),
  [pause](https://yandex.cloud/ru/docs/cli/cli-ref/serverless/cli-ref/trigger/v2/pause),
  [resume](https://yandex.cloud/ru/docs/cli/cli-ref/serverless/cli-ref/trigger/v2/resume),
  [cron](https://yandex.cloud/ru/docs/functions/concepts/trigger/timer).
- [SA create](https://yandex.cloud/ru/docs/cli/cli-ref/iam/cli-ref/service-account/create),
  [SA IAM binding](https://yandex.cloud/ru/docs/cli/cli-ref/iam/cli-ref/service-account/add-access-binding),
  [folder IAM binding](https://yandex.cloud/ru/docs/cli/cli-ref/resource-manager/cli-ref/folder/add-access-binding),
  [federated credential](https://yandex.cloud/ru/docs/cli/cli-ref/iam/cli-ref/workload-identity/federated-credential/create).
- [Secret create](https://yandex.cloud/ru/docs/cli/cli-ref/lockbox/cli-ref/secret/create),
  [Secret/create API и отсутствие payload](https://yandex.cloud/ru/docs/lockbox/api-ref/Secret/create),
  [secret get](https://yandex.cloud/ru/docs/cli/cli-ref/lockbox/cli-ref/secret/get),
  [secret IAM binding](https://yandex.cloud/ru/docs/cli/cli-ref/lockbox/cli-ref/secret/add-access-binding).
- [Object upload](https://yandex.cloud/ru/docs/cli/cli-ref/storage/cli-ref/s3api/put-object),
  [logging read](https://yandex.cloud/ru/docs/cli/cli-ref/logging/cli-ref/read),
  [Python logging](https://yandex.cloud/ru/docs/functions/lang/python/logging).
