# Конфигурация

Файлы читаются относительно корня приложения. Рабочие controller.json,
registry.json, boiler_reset.json и boiler_cli.json игнорируются Git;
versioned *.example.json — шаблоны. Не помещать токен в JSON.
Неизвестные поля, неверные типы, повторные ключи и нечисловые числа отклоняются.
Общих --config/ZONT_CONFIG и module_configs у регистратора нет.

## controller.json

```json
{
  "contact": "owner@example.invalid",
  "http_timeout_seconds": 15
}
```

contact — контакт для X-ZONT-Client, http_timeout_seconds — конечное
положительное число до 120 с. ZONT_TOKEN поступает только из окружения.
Поля coordination/statistics удалены: нет параметров лидера, аренды,
R1/R2, частоты координации и файла журнала оркестрации.

## registry.json

- disabled_modules: список известных ID, которые не регистрируются.
- autostart_dual_mode: false по умолчанию; true запускает continuous у dual-mode.
- once_timeouts: minimum_seconds/recommended_seconds для каждого once-модуля.

Эффективный минимум — максимум настройки регистратора и minimum_seconds
модуля. Для boiler_reset минимум модуля 120 с, шаблон рекомендует 180 с:
40 с импульса плюс инженерный бюджет на историю, HTTP и cleanup.
Это не обещание верхней границы сетевых задержек. Continuous не получает
таймаутов отдельных проверок от регистратора.
registry.headless.example.json отключает терминальный модуль.

## boiler_reset.json

Файл читает сам BoilerReset. Обязательные поля:
device_id, circuit_id, air_sensor_id, coolant_sensor_id, reset_control_id,
check_interval_seconds. ID — положительные целые, датчики воздуха и воды
разные. check_interval_seconds — положительное конечное число.
Необязательный detection_profile поддерживает только smart_2_heating.

Проверки используют 45 минут истории, импульс 40 с и паузу 300 с после off
в continuous. Эти величины не настраиваются регистратором. Grant wait/poll
и lease_seconds удалены. [Поведение](../docs/reference/boiler-reset.md).

## boiler_cli.json

Обязательны devices, controls, graphs. devices — непустой список уникальных ID.
controls — allowlist с device_id/kind/element_id/actions. graphs — список
graph_id/title/series; ряд содержит key(device_id/kind/element_id/field) и
необязательную label. Один график относится к одному прибору и одной единице.
Пример полной схемы — boiler_cli.example.json.

Необязательны output_directory (charts по умолчанию) и display_timezone
(UTC по умолчанию). Lease/grant параметры удалены. Обновление данных — раз
в 60 с. Команда без --execute показывает preview, с ним сразу вызывает
execute_command после проверки allowlist и возможностей устройства.

## Переход

Удалить coordination/statistics из controller.json, lease_seconds и
grant_wait_seconds из boiler_cli.json, grant_wait_seconds/grant_poll_seconds
из boiler_reset.json. Локальные рабочие файлы проекта уже приведены к схеме.
Идентификаторы приборов, датчиков, графики и остальные пользовательские
настройки сохраняются. Алгоритмы не переименовывают и не очищают name/notes.
