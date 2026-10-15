# Индекс инструкций

| Изменение | Читать |
| --- | --- |
| Управляющий фасад, регистратор/DI, once/continuous и автономные модули | [Целевая архитектура](architecture.md), [текущие контракты](contracts.md) |
| API, датчики, история, команды и CLI | [Контракты](contracts.md), [справочник API](../reference/zont-api.md) |
| Монополия управления устройством и общие флаги ZONT | [Архитектура, §3](architecture.md#3-независимость-команды-и-полномочия), [исследование API](../reports/2026-10-14-zont-control-lock-api.md) |
| Условный выбор лидера, сроки аренды и координация исполнителей | [Контракт лидерства](../reference/leader-election.md), [проверка формул](../reports/2026-10-14-fischer-lease-review.md) |
| Допустимый риск и проверка надёжности оркестрации | [Критерий 99.9%](../reference/orchestration-risk.md), [модель R1/R2](../reports/2026-10-14-two-register-coordination-review.md) |
| Детектор, калибровка и модуль сброса | [Контракты](contracts.md), [детектор](../reference/anomaly-detection.md), [сброс котла](../reference/boiler-reset.md), [отчёт](../reports/2026-10-13-anomaly-calibration.md) |
| Тесты, сборка, CI, структура и версии | [Разработка](development.md), [технический README](../../scripts/README.md) |
| Облачный запуск, OIDC, Lockbox и деплой | [Деплой](deployment.md), [план](../plans/yandex-cloud.md) |

Текущее приложение — [README](../../README.md), краткие изменения —
[CHANGELOG](../../CHANGELOG.md), решения — [история](../history/README.md),
основания выпусков — [история версий](../history/versioning.md).
Общие правила — [AGENTS.md](../../AGENTS.md).

Переход к согласованной архитектуре —
[план TDD без деплоя](../plans/20261015005915-architecture-alignment-implementation-plan.md).
Прототип заменяется без обратной совместимости —
[решение и итог разбора четырёх замечаний плана](../history/2026-10-15-prototype-replacement.md).

architecture.md описывает согласуемую будущую конструкцию; код пока не
переработан. [Независимое ревью](../reviews/2026-10-14-autonomy-architecture.md)
и [реакция на замечания](../reviews/2026-10-14-autonomy-architecture-resolution.md)
содержат альтернативы и открытые решения.
