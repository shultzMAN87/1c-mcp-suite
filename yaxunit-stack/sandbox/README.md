# sandbox/ — учебная конфигурация для проверки YAxUnit-пайплайна

Минимальный «hello world», на котором проверяется весь цикл
**deploy → run → parse JUnit → вернуть агенту**.

## Принцип

Конфигурация и тестовое расширение хранятся **только как XML/BSL исходники**.
Никаких бинарных `.cf` / `.cfe` собирать вручную не нужно: платформа 1С 8.3
загружает их прямо из каталога через `DESIGNER /LoadConfigFromFiles`.
Исключение — сам движок YAxUnit, который потребляется как готовый `.cfe` из
официального релиза `bia-technologies/yaxunit` (Dockerfile скачивает его на
этапе build в `/opt/yaxunit/yaxunit.cfe`).

Это означает, что после `docker compose up` пайплайн готов к работе сразу,
без шага «зайти в конфигуратор и собрать .cf».

## Структура

```
sandbox/
├── README.md
├── demo-config/                       # основная конфигурация (XML/BSL)
│   ├── Configuration.xml
│   └── CommonModules/
│       └── Калькулятор/
│           ├── Калькулятор.xml
│           └── Ext/
│               └── Module.bsl         # Функция Сложить(А, Б) Экспорт
└── demo-tests/                        # тестовое расширение (XML/BSL)
    ├── Configuration.xml              # имя расширения = Tests
    └── CommonModules/
        └── Тест_Калькулятор/
            ├── Тест_Калькулятор.xml
            └── Ext/
                └── Module.bsl         # ИсполняемыеСценарии + один тест
```

## Что грузится в базу при `yaxunit_deploy`

`scripts/deploy_test_db.sh` выполняет три загрузки последовательно:

1. **Основная конфигурация:**
   `DESIGNER /LoadConfigFromFiles /sandbox/demo-config` → `/UpdateDBCfg`
2. **Расширение «движок YAxUnit»:**
   `DESIGNER /LoadCfg /opt/yaxunit/yaxunit.cfe -Extension YAxUnit`
   → `/UpdateDBCfg -Extension YAxUnit`
   (это бинарь из релиза — единственное исключение из правила «всё в исходниках»)
3. **Расширение с тестами:**
   `DESIGNER /LoadConfigFromFiles /sandbox/demo-tests -Extension Tests`
   → `/UpdateDBCfg -Extension Tests`

После этого `RunUnitTests` находит в базе оба расширения, движок поднимает
тесты из `Tests` и пишет JUnit XML.

## Содержимое тестового модуля

`demo-tests/CommonModules/Тест_Калькулятор/Ext/Module.bsl`:

```bsl
// Регистрация в движке YAxUnit
Процедура ИсполняемыеСценарии() Экспорт
    ЮТТесты
        .ДобавитьТест("Сложить_ДваПлюсДва_Возвращает4");
КонецПроцедуры

Процедура Сложить_ДваПлюсДва_Возвращает4() Экспорт
    Результат = Калькулятор.Сложить(2, 2);
    ЮТест.ОжидаетЧто(Результат).Равно(4);
КонецПроцедуры
```

Чтобы убедиться, что красный путь тоже работает: временно поменяйте в
`demo-config/CommonModules/Калькулятор/Ext/Module.bsl` тело на `Возврат А - Б;`,
повторите `yaxunit_deploy` + `yaxunit_run` — должны увидеть `failed: 1`
с понятным сообщением от ассерта.
