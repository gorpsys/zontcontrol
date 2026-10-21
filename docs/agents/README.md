# Индекс инструкций

Выберите область изменения; текущие правила и их обоснования разделены.
Общая карта чтения — [документация](../README.md).

| Область / файлы | Действующие правила | Обоснование / свидетельства |
| --- | --- | --- |
| Управляющий API: `zontcontrol/controller/` | [Управляющий API](contracts.md#управляющий-api), [данные и команды](architecture.md#2-данные-и-команды), [справочник ZONT](../reference/zont-api.md) | [Удаление синхронизации](../history/2026-10-20-remove-synchronization.md) |
| Предметные данные: `heating.py`, `heating_adapter.py`, `monitor.py` | [Предметный вход отопления](contracts.md#предметный-вход-отопления), [границы](architecture.md#1-состав-и-границы) | [Регрессии нормализации](../reports/2026-10-20-typed-heating-regressions.md) |
| Детектор и сброс: `anomaly.py`, `algorithms/boiler_reset.py` | [Детектор](../reference/anomaly-detection.md), [последовательность сброса](../reference/boiler-reset.md#назначение-и-последовательность), [остановка](../reference/boiler-reset.md#остановка-и-ошибки) | [Калибровка](../reports/2026-10-13-anomaly-calibration.md) |
| DI, регистратор и lifecycle: `application.py`, `autonomy/` | [Регистратор](architecture.md#3-регистратор-и-запуск), [автономные алгоритмы](contracts.md#автономные-алгоритмы), [завершение](contracts.md#завершение) | [Один процесс](../history/2026-10-15-single-process.md), [ответственность continuous](../history/2026-10-15-continuous-responsibility.md) |
| CLI и конфиги: `algorithms/boiler_cli.py`, `config/` | [CLI](../../README.md#cli), [конфигурация](../../config/README.md), [секреты](contracts.md#конфиги-и-секреты) | [Конфиги алгоритмов](../history/2026-10-15-algorithm-configuration.md) |
| Тесты, пакет, CI и версии: `tests/`, `scripts/`, `Makefile` | [Разработка](development.md), [инструменты](../../scripts/README.md), [выпуск](../../scripts/README.md#версии-и-выпуск) | [История версий](../history/versioning.md) |
| Yandex Cloud, OIDC и Lockbox: `deploy/`, `.github/workflows/`, `cloud.py` | [Полный how-to](../reference/github-yandex-cloud-howto.md), [контракт деплоя](deployment.md), [облачный вход](contracts.md#облачный-вход), [реестр ресурсов](../reference/yandex-cloud-resources.md) | [Provisioning](../reports/2026-10-20-yandex-cloud-provisioning.md), [приёмка](../reports/2026-10-21-yandex-cloud-acceptance.md) |
| Документация: `*.md`, `docs/` | [Правила ведения](documentation.md), [карта](../README.md) | [История решений](../history/README.md), [план упорядочения](../plans/20261021174419-documentation-order-implementation-plan.md) |

Синхронизация управления удалена по
[решению от 20.10.2026](../history/2026-10-20-remove-synchronization.md).
Исторические планы, ревью и отчёты о coordination/монополии не задают текущие
требования. Текущее приложение описано в [README](../../README.md),
причины решений — в [истории](../history/README.md).
