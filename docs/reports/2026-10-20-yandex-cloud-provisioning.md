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
repository variables на тот момент ещё не были настроены. Их последующая
настройка описана ниже; первый deploy и активация ещё не выполнены.

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
В Task 5 repository variables не настраивались. В последующем premerge
checkpoint Task 6 все десять variables созданы или сверены с ожидаемыми:
CLOUD_ID, FOLDER_ID, SA_ID, FUNCTION_ID, FUNCTION_SA_ID, BUCKET,
LOCKBOX_SECRET_ID, LOCKBOX_VERSION_ID, YC_OIDC_AUDIENCE и ZONT_CONTACT.
Повторное чтение GitHub подтвердило точное совпадение каждого значения;
YC_OIDC_AUDIENCE и ZONT_CONTACT созданы и повторно прочитаны на этом шаге.
Значение контакта и остальные значения variables в вывод проверки не попадали.
Эта проверка не вызывала yc или ZONT и не публиковала версию функции.
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

## Попытки деплоя и исправление OIDC, 21.10.2026

Выпуск 0.5.0 слит через [PR 11](https://github.com/gorpsys/zontcontrol/pull/11)
в master 5003b9cf33d774c267f4c4ecb69757c7eab47f04. Его
[Project checks](https://github.com/gorpsys/zontcontrol/actions/runs/39517478702)
завершились успешно; Release tag автоматически опубликовал v0.5.0.

Первая попытка [deploy-prod](https://github.com/gorpsys/zontcontrol/actions/runs/39517570420)
остановилась в helper с безопасной ошибкой HTTP 401 до вызовов yc.
Текущая диагностика не называла HTTP-границу, поэтому конкретный endpoint
не был установлен по журналу. Чтение GitHub
`GET /repos/gorpsys/zontcontrol/actions/oidc/customization/sub` выявило
`use_default=true`, `use_immutable_subject=true` и prefix
`repo:gorpsys@79201964/zontcontrol@1420696677`.
Созданный ранее credential ожидал subject без числовых ID; это несовпадение
исправлено. Попытка PUT с `use_default=true, use_immutable_subject=false`
вернула успех, но отдельный GET показал прежний immutable prefix.

GitHub использует immutable default для новых репозиториев после 15.07.2026.
В этом формате ID владельца и репозитория нельзя убрать даже через
`include_claim_keys`: [immutable subject](https://docs.github.com/en/actions/reference/security/oidc#immutable-subject-claims),
[ограничение кастомизации](https://docs.github.com/en/actions/reference/security/oidc#customizing-the-subject-claims-for-an-organization-or-repository).
Общие настройки владельца и федерации не менялись.

Владелец отдельно подтвердил три ручных запроса yc: создание нового
credential, независимый list для проверки и удаление прежнего credential.
Подтверждённые поля нового credential:

```text
id=ajehfh3q51b5i5q6frcr
service_account_id=aje715nu0u832o93e0hd
federation_id=ajej9j37pt8j7ns962vr
external_subject_id=repo:gorpsys@79201964/zontcontrol@1420696677:ref:refs/heads/master
```

List подтвердил точное совпадение этих полей. Последующее удаление прежнего
aje80bckog9ld78ondm9 завершилось с exit=0; отдельного list после удаления
не выполнялось. Значение AUD и выданные роли не менялись.

После исправления credential разрешённый повтор только упавших jobs дал
[попытку 2](https://github.com/gorpsys/zontcontrol/actions/runs/39517570420/attempts/2).
Gate, получение credentials, чтение функции и пустого списка версий прошли.
Шаг публикации напечатал SHA и provenance ZIP:

```text
source_sha=5003b9cf33d774c267f4c4ecb69757c7eab47f04
sha256=44cd3d46d5cf605dd8026004c7196800b300f490e9fff0b9d8bc00c26d15762a
object=functions/zontcontrol/5003b9cf33d774c267f4c4ecb69757c7eab47f04/39517570420-2.zip
```

Затем helper остановился: `upload outcome cannot be proven; write was not retried`.
Вызван один put-object и один контрольный head-object; низкая причина upload
и результат HEAD были скрыты прежней диагностикой. Исход записи ZIP неизвестен;
создание версии функции после этого по управляющему потоку не вызывалось.
Повторная запись для диагностики не выполнялась. Локальные `yc --version`
и `yc storage s3api put-object --help` без облачных запросов подтвердили
точную версию 1.40.0 и документированное `--body @path/to/file`.
Менять body по отличию от образца оснований нет.

PATCH 0.5.1 добавляет фиксированные стадии HTTP, exit-код и allowlist категорий
yc; сохраняет безопасные причины upload/HEAD без сырых ответов провайдера.
Офлайн-регрессии проверяют приватность токенов/контактов и запрет повторов.
Эта диагностика не устанавливает ещё неизвестную причину отказа upload.

## Следующая граница: Task 6

Metadata активной версии и ключа после заполнения владельцем уже проверены.
Repository variables, выпуск 0.5.0, PR/CI и разрешённое слияние выполнены.
Далее нужен первый успешный автоматический deploy с version ID, ACTIVE,
provenance SHA/manifest и все параметры. До этого таймер отсутствует.
Создание и включение таймера требуют отдельного подтверждения владельца;
после них нужны два наблюдаемых плановых once с фактическими результатами
и длительностями. Эти внешние критерии не заменены офлайн-проверками.

Controller API v2, детектор/пороги, on/40 секунд/finally off и отсутствие
синхронизации не менялись. API-успех не доказывает физическое восстановление;
жёсткое завершение хоста остаётся вне гарантии finally.
