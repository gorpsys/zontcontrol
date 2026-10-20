# Создание отдельных ресурсов Yandex Cloud для zontcontrol

Дата: 20.10.2026. Task 5 согласованного
[плана](../plans/20261020165020-yandex-cloud-boiler-reset-implementation-plan.md).
Ветка: release/yandex-cloud-boiler-reset. Запись фактов начата
2026-10-20 16:24:33 UTC (19:24:33 Europe/Moscow).

Отдельные deploy SA, OIDC credential, закрытый ресурс функции и metadata
Lockbox созданы. Четыре последующих IAM/OIDC readback совпали с ожиданиями.
Функция не имеет опубликованной версии кода. Статус ACTIVE самого ресурса
не доказывает наличие или успешную работу handler. Таймер не создавался.
Цель автоматического запуска ещё не достигнута; Task 6 не выполнен.

## Разрешения и источник доказательств

Владелец отдельно подтвердил каждую ручную команду yc с её действием и целью:
проверки имён, создание, каждый binding и последующее чтение. Подтверждение
плана не использовалось вместо этих разрешений. Все операции выполнял
основной агент; исполнитель этого отчёта облако и ZONT не запрашивал.

Ниже записаны операции и безопасные поля ответов, подтверждённые в сеансе.
Команды соответствовали [справочнику yc](../plans/20261020165020-yandex-cloud-yc-commands.md)
с реальными ID из этого отчёта. Общий prefix всех запросов:

```text
yc --endpoint api.cloud.yandex.net:443 --cloud-id b1gbga988v36vq19lmb7 --folder-id b1gdgrp5bnth11phova8 --retry 0
```

В таблицах опущен этот prefix; metadata list/get/create использовали
`--format json` и ограничивающий `--jq` из справочника. Binding add выполнялся
без этих флагов. Это перечень проверенных действий, а не полный transcript.
Произвольные stdout/stderr ошибок не публиковались. Файлы авторизации,
payload Lockbox и значения env не читались. Статические IAM-ключи не создавались.

Время ниже — время записи контрольной точки в локальный progress,
Europe/Moscow (UTC+3), а не точное начало каждого запроса. Первое чтение
service-account list было остановлено sandbox до облачного запроса из-за
блокировки домена. После отдельного подтверждения повтор выполнен с нужным
escalation; чтения успешны. Неопределённых исходов записей не было,
записи не повторялись.

## Проверки отсутствия и создание

| Операция yc | Подтверждённый результат | Контрольная точка |
| --- | --- | --- |
| iam service-account list, фильтр ci-cd-zontcontrol-sa | [] | 19:10:03 |
| serverless function list, фильтр zontcontrol | [] | 19:10:03 |
| lockbox secret list, фильтр z_token | [] | 19:10:03 |
| iam service-account create --name ci-cd-zontcontrol-sa | id=aje715nu0u832o93e0hd | 19:13:15 |
| serverless function create --name zontcontrol | id=d4e9enfbuhk2h59a0fj0, name=zontcontrol, status=ACTIVE | 19:13:15 |
| lockbox secret create --name z_token | id=e6q7k3r798m1od3jc647, name=z_token, current_version=null, kms_key_id=null | 19:13:15 |

Все три созданных ресурса находятся в folder b1gdgrp5bnth11phova8.
Одноимённых ресурсов до создания не обнаружено; найденные ресурсы не
перезаписывались. Secret create не содержал payload, password specification
или собственного KMS-ключа: создана только metadata без версии значения.
`current_version=null` — наблюдение при создании, не утверждение о состоянии
после возможных действий владельца. Позже владелец сообщил, что добавил
версию с ключом z_token. Последующее отдельное согласованное чтение metadata
основным агентом подтвердило секрет ACTIVE и current_version:
id=e6qi0n3ucqk2stmtdalb, status=ACTIVE, payload_entry_keys=[z_token].
Операция — `lockbox secret get --id e6q7k3r798m1od3jc647` с metadata-only
фильтром из раздела 4 справочника. Значение секрета не читалось.
Дополнение записано 2026-10-20 16:30:41 UTC (19:30:41 Europe/Moscow);
это время записи, не точное время запроса. ID версии закреплён в примере;
repository variables, первый deploy и активация ещё не выполнены.

## IAM, OIDC и проверка результата

В каждой строке роль добавлялась адресно; старые bindings не заменялись.
Две записи policy общего folder выполнялись последовательно.

| Операция yc | Роль / subject | Контрольная точка |
| --- | --- | --- |
| resource-manager folder add-access-binding --id b1gdgrp5bnth11phova8 | functions.admin / serviceAccount:aje715nu0u832o93e0hd | 19:16:58 |
| resource-manager folder add-access-binding --id b1gdgrp5bnth11phova8 | storage.editor / serviceAccount:aje715nu0u832o93e0hd | 19:16:58 |
| iam service-account add-access-binding --id ajelpbq7as5bpe499962 | iam.serviceAccounts.user / serviceAccount:aje715nu0u832o93e0hd | 19:16:58 |
| lockbox secret add-access-binding --id e6q7k3r798m1od3jc647 | lockbox.payloadViewer / serviceAccount:ajelpbq7as5bpe499962 | 19:20:32 |

Отдельно согласованный `iam workload-identity federated-credential create`:
service_account_id=aje715nu0u832o93e0hd,
federation_id=ajej9j37pt8j7ns962vr,
external_subject_id=repo:gorpsys/zontcontrol:ref:refs/heads/master.
Получен credential ID aje80bckog9ld78ondm9, результат записан в 19:20:32.
GitHub OIDC AUD остаётся https://github.com/gorpsys; IAM audience при обмене
будет ID нового deploy SA. Эти audience не взаимозаменяемы.

Отдельно согласованный `serverless function get --id d4e9enfbuhk2h59a0fj0`
подтвердил ID, name=zontcontrol, folder=b1gdgrp5bnth11phova8 и ACTIVE ресурса
(контрольная точка 19:20:32). Новая версия функции не публиковалась.

Четыре свежих отдельно согласованных чтения завершились совпадением;
общая контрольная точка — **20.10.2026 19:23:00 Europe/Moscow
(16:23:00 UTC)**:

| Чтение yc | Фильтр / подтверждённые поля |
| --- | --- |
| resource-manager folder list-access-bindings --id b1gdgrp5bnth11phova8 | subject.id=aje715nu0u832o93e0hd, subject.type=serviceAccount: ровно functions.admin и storage.editor |
| iam service-account list-access-bindings --id ajelpbq7as5bpe499962 | subject.id=aje715nu0u832o93e0hd, subject.type=serviceAccount: iam.serviceAccounts.user |
| lockbox secret list-access-bindings --id e6q7k3r798m1od3jc647 | subject.id=ajelpbq7as5bpe499962, subject.type=serviceAccount: lockbox.payloadViewer |
| iam workload-identity federated-credential list --service-account-id aje715nu0u832o93e0hd | Единственный credential aje80bckog9ld78ondm9, тот же SA/federation/subject, что при создании |

Фильтр IAM — `map(select(.subject.id == "<указанный ID>"))`, вывод OIDC —
`map({id,service_account_id,federation_id,external_subject_id})`.
Проверки подтверждают адресные новые роли; они не являются аудитом всех
остальных субъектов общего folder или всех ресурсов облака.

## Общие ресурсы и границы изменений

| Ресурс | Использование / граница |
| --- | --- |
| Cloud b1gbga988v36vq19lmb7, folder b1gdgrp5bnth11phova8 | Переиспользуются; в folder добавлены две роли нового deploy SA |
| Bucket autorepeater-deploy | Переиспользуется; upload не выполнялся, будущий префикс functions/zontcontrol/ |
| Runtime SA/invoker ajelpbq7as5bpe499962 | Переиспользуется; добавлен iam.serviceAccounts.user для нового deploy SA |
| Federation ajej9j37pt8j7ns962vr | Переиспользуется новым отдельным credential |
| Default log group общего folder | Будущее назначение логов; функция ещё не публиковалась |
| Функция образца d4e6d0l53hkivgrmfano | Не была целью записей; раннее чтение видело python-function, владелец сообщил переименование в t-invest |
| Deploy SA образца ajela8pd8l3bjeupa04s | Не был целью записей |
| Секрет t_token e6qc2ghhip6925lllhm7 | Не был целью записей; payload не читался |
| Таймер образца a1sb17vl05nregkjh2si | Не был целью записей |

Свежих get исходных ресурсов для доказательства их полного неизменного
состояния в Task 5 не выполнялось. Утверждение здесь ограничено адресами
выполненных команд; общий folder и policy runtime SA получили согласованные
добавления. Переименование функции образца известно из сообщения владельца,
новым облачным чтением не перепроверялось.

## Офлайн-параметры будущей публикации и таймера

[Пример](../../deploy/yandex.example.json) содержит реальные ID ресурсов,
но остаётся примером: helper его не читает. LOCKBOX_VERSION_ID закреплён на
e6qi0n3ucqk2stmtdalb после metadata readback; ZONT_CONTACT остаётся null.
Repository variables на этом шаге не настраивались.
Значение ZONT_TOKEN в пример и отчёт не включается.

Ожидаемые настройки версии: python312, handler.handler, 256MB, timeout=420s,
concurrency=1, runtime SA=ajelpbq7as5bpe499962,
log folder=b1gdgrp5bnth11phova8. Secret binding после подтверждения версии:
e6q7k3r798m1od3jc647 / e6qi0n3ucqk2stmtdalb / key=z_token / env=ZONT_TOKEN.
Это офлайн-ожидания, а не наблюдаемые настройки уже развёрнутого handler.
При публикации --async-max-retries явно не задаётся, как в согласованном плане.

Ожидаемый action v2 будущего timer zontcontrol-15m, cron `0/15 * ? * * *` (UTC):

```json
{
  "invoke_function": {
    "function_id": "d4e9enfbuhk2h59a0fj0",
    "function_tag": "$latest",
    "service_account_id": "ajelpbq7as5bpe499962"
  },
  "retry_policy": {"interval": "10s", "retry_attempts": "1"}
}
```

Один повтор после ошибки через 10 секунд заново выполняет весь once и может
снова привести к сбросу. Отдельные команды котлу автоматически не повторяются.
Офлайн сверены JSON action, ID ресурсов, фиксированные settings и workflow;
`make check` завершился успешно: 428 тестов, 6 прежних installed-only skips.
Первый прогон обнаружил устаревшее ожидание null новых ID в существующем
тесте примера; ожидания ресурсов и затем версии заменены на подтверждённые ID,
итоговый прогон зелёный.
Состав пакета и runtime не менялись; дополнительные проверки wheel/ZIP в
этом документальном шаге не требуются. Облачные запросы, ZONT и сетевой
вызов handler в этих проверках отсутствовали.

## Следующая граница: Task 6

Metadata активной версии и ключа после заполнения владельцем уже проверены.
Далее нужны repository variables, выпуск/PR/CI и отдельно разрешённое слияние.
Первый автоматический deploy должен подтвердить version ID, ACTIVE,
provenance SHA/manifest и все параметры. До этого таймер отсутствует.
Создание и включение таймера требуют отдельного подтверждения владельца;
после них нужны два наблюдаемых плановых once с фактическими результатами
и длительностями. Эти внешние критерии не заменены офлайн-проверками.

Controller API v2, детектор/пороги, on/40 секунд/finally off и отсутствие
синхронизации не менялись. API-успех не доказывает физическое восстановление;
жёсткое завершение хоста остаётся вне гарантии finally.
