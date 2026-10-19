# Индекс инструкций

| Изменение | Читать |
| --- | --- |
| Управляющий фасад, регистратор/DI, once/continuous и автономные модули | [Целевая архитектура](architecture.md), [текущие контракты](contracts.md) |
| API, датчики, история, команды и CLI | [Контракты](contracts.md), [справочник API](../reference/zont-api.md) |
| Монополия управления устройством и общие флаги ZONT | [Архитектура, §3](architecture.md#3-независимость-команды-и-полномочия), [исследование API](../reports/2026-10-14-zont-control-lock-api.md) |
| Выбор лидера и grants v2: R1=name, R2=notes, сроки и профили | [Текущий контракт](../reference/leader-election.md), [решения v2](../history/2026-10-19-monopoly-protocol-v2.md), [калибровка](../reports/2026-10-19-coordination-v2-calibration.md) |
| Граница coordination, общие типы, порты и сборка runtime | [Архитектура, §1](architecture.md#1-состав-и-границы), [контракты пакета](contracts.md#граница-пакета-coordination) |
| Допустимый риск и проверка надёжности оркестрации | [Реестр сценариев и запрещённых регрессий](../reference/coordination-failure-scenarios.md), [учёт риска и статистика](../reference/orchestration-risk.md), [модель R1/R2](../reports/2026-10-14-two-register-coordination-review.md) |
| Детектор, калибровка и модуль сброса | [Контракты](contracts.md), [детектор](../reference/anomaly-detection.md), [сброс котла](../reference/boiler-reset.md), [отчёт](../reports/2026-10-13-anomaly-calibration.md) |
| Тесты, сборка, CI, структура и версии | [Разработка](development.md), [технический README](../../scripts/README.md) |
| Облачный запуск, OIDC, Lockbox и деплой | [Деплой](deployment.md), [план](../plans/yandex-cloud.md) |

Текущее приложение — [README](../../README.md), краткие изменения —
[CHANGELOG](../../CHANGELOG.md), решения — [история](../history/README.md),
основания выпусков — [история версий](../history/versioning.md).
Общие правила — [AGENTS.md](../../AGENTS.md).

Выполненный переход к согласованной архитектуре —
[план TDD без деплоя](../plans/completed/20261015005915-architecture-alignment-implementation-plan.md).
Прототип заменяется без обратной совместимости —
[решение и итог разбора четырёх замечаний плана](../history/2026-10-15-prototype-replacement.md).

architecture.md описывает реализованную локальную конструкцию 0.3.0 на 19.10.2026;
[итог и границы проверки](../history/2026-10-16-architecture-implementation.md).
Структурная изоляция координации —
[выполненный план](../plans/completed/20261017220801-coordination-isolation-implementation-plan.md).
Замена внутреннего wire на v2 с сохранением статической границы —
[выполненный план](../plans/completed/20261019002421-monopoly-protocol-v2-implementation-plan.md).
Исторические отчёты v1 не заменяют текущие роли регистров и формулы.
Запрос монополии API v1 — [текущий договор](contracts.md#запрос-монополии-api-v1),
[выполненный план issue #4](../plans/completed/20261016204209-control-request-api-implementation-plan.md).
[Независимое ревью первоначального проекта](../reviews/2026-10-14-autonomy-architecture.md)
и [реакция на замечания](../reviews/2026-10-14-autonomy-architecture-resolution.md)
содержат исторические альтернативы и принятые решения; они не заменяют текущий контракт.
