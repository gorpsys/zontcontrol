# Ресурсы Yandex Cloud для zontcontrol

Снимок по проверкам **20–21.10.2026**, составлен 21.10.2026. Источники —
[provisioning](../reports/2026-10-20-yandex-cloud-provisioning.md) и
[приёмка](../reports/2026-10-21-yandex-cloud-acceptance.md). Это сводка уже
подтверждённых metadata, а не новое чтение облака. ACTIVE, настройки и права
не заявляются проверенными после дат этих источников. Значения токенов,
контакта и окружения здесь не хранятся; имена ключей и env ниже — схема binding.

Настройка и перенос — [полный how-to](github-yandex-cloud-howto.md), обязательные
проверки и полномочия — [контракт деплоя](../agents/deployment.md).

## Общие ресурсы

| Ресурс | ID / имя | Источник и предел подтверждения |
| --- | --- | --- |
| Cloud | `b1gbga988v36vq19lmb7` | Provisioning: scope выполненных команд |
| Folder | `b1gdgrp5bnth11phova8` | Provisioning: размещение отдельных ресурсов и адресные IAM bindings |
| Bucket | `autorepeater-deploy` | Provisioning: общий bucket; префикс zontcontrol — `functions/zontcontrol/` |
| OIDC federation | `ajej9j37pt8j7ns962vr` | Provisioning: ID в проверенном federated credential |
| Runtime SA | `ajelpbq7as5bpe499962` | Provisioning: bindings на SA и Lockbox; acceptance: invoker в action таймера |
| Invoker SA | `ajelpbq7as5bpe499962` | Тот же аккаунт; acceptance: `action.invoke_function.service_account_id` |
| Журнал | Default log group folder выше | Provisioning: ожидаемое назначение; acceptance: успешный workflow с проверкой settings. Отдельный ID группы в этих отчётах не записан |

Общие ресурсы переиспользуются. Provisioning подтверждает согласованные
добавления ролей, а не полный аудит всех IAM policies. Исходные function,
deploy SA, secret и timer autorepeater не являются целями изменений;
их исторические ID и границы запросов сохранены в отчёте provisioning.

## Отдельные ресурсы и привязки

| Ресурс / параметр | ID / имя | Источник и дата |
| --- | --- | --- |
| Закрытая функция | `zontcontrol`, `d4e9enfbuhk2h59a0fj0` | Provisioning 20.10: создан ресурс; acceptance 21.10: цель action таймера |
| Deploy SA (`SA_ID`) | `ci-cd-zontcontrol-sa`, `aje715nu0u832o93e0hd` | Provisioning 20.10: создан и проверены адресные роли |
| Federated credential | `ajehfh3q51b5i5q6frcr` | Provisioning, дополнение 21.10: создан и проверен list с исправленным immutable subject |
| Lockbox secret | `z_token`, `e6q7k3r798m1od3jc647` | Provisioning 20.10: metadata секрета ACTIVE; payload не читался |
| Закреплённая версия секрета | `e6qi0n3ucqk2stmtdalb` | Provisioning 20.10: current_version ACTIVE, есть нужный ключ |
| Ключ / env binding | `z_token` → `ZONT_TOKEN` | Provisioning: схема ожидаемой публикации; acceptance: workflow проверяет settings и provenance |
| Таймер v2 | `zontcontrol-15m`, `a1sqsgvh3u31bhlj3n9a` | Acceptance 21.10: отдельно согласованное чтение подтвердило ACTIVE, source/action |
| Cron | `0/15 * ? * * *`, UTC | Acceptance 21.10: раз в 15 минут, шесть полей YC |
| Action таймера | Единственный `invoke_function`, функция выше, тег `$latest`, invoker выше | Acceptance 21.10: readback source/action |
| Retry policy таймера | `interval=10s`, `retry_attempts=1` | Acceptance 21.10: readback; повтор выполняет весь once заново |

Секрет имеет закреплённый ID версии; тег функции `$latest` — другой механизм.
Новый секрет сам по себе не переключает опубликованную версию функции.
Неизвестная запись не повторяется автоматически; успешный ответ API не доказывает
физический результат. Создание/включение таймера требует отдельного разрешения.

## OIDC и IAM

По дополнению provisioning от 21.10.2026 credential привязан к deploy SA,
федерации выше и subject
`repo:gorpsys@79201964/zontcontrol@1420696677:ref:refs/heads/master`.
GitHub audience — `https://github.com/gorpsys`; audience IAM exchange —
ID deploy SA. Это разные параметры. Subject принадлежит этому репозиторию
и не переносится в другой. Старый credential без immutable IDs описан только
как история в provisioning.

Provisioning 20.10 подтвердил для deploy SA роли `functions.admin` и
`storage.editor` на folder, `iam.serviceAccounts.user` на runtime SA;
для runtime SA — `lockbox.payloadViewer` на отдельный секрет. При этом чтении другие права
общего аккаунта не проверяли. Статические IAM-ключи
для GitHub не создавались.

## Исходники, публикация и наблюдаемые вызовы

По acceptance от 21.10.2026 опубликован **v0.7.0** из master
[`6dc71121cbc2feb290dbc5403bb1ef450bf379ea`](https://github.com/gorpsys/zontcontrol/tree/6dc71121cbc2feb290dbc5403bb1ef450bf379ea),
[deploy run 39596808805](https://github.com/gorpsys/zontcontrol/actions/runs/39596808805)
завершился успешно. Workflow проверил ACTIVE, SHA, settings и provenance.
ID опубликованной версии функции в этих двух отчётах не приведён; его нельзя
заменять ID функции или версией прежнего выпуска. Реестр не устанавливает
нынешний `$latest` новым запросом. Исходники документационного PR и новая
подготовленная версия приложения не являются подтверждённым cloud-деплоем.

Отдельное чтение подтвердило ACTIVE таймера. В notes наблюдены четыре
последовательных завершённых вызова 21.10 с 13:15:59 до 14:01:16 UTC:
normal, unknown, normal, normal, без сброса. Реальные времена, request IDs,
длительности и ротация трёх логов сохранены в acceptance. ACTIVE не доказывает
вызов; техническое завершение не превращает unknown в normal и не доказывает
физическое восстановление отопления.
