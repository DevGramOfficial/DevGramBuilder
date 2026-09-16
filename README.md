# DevGramBuilder

[![Tests](https://github.com/firedragoq/DevGramBuilder/actions/workflows/tests.yml/badge.svg)](https://github.com/firedragoq/DevGramBuilder/actions/workflows/tests.yml)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-22c55e.svg)](LICENSE)

Официальный CLI-сборщик плагинов DevGram. Он создаёт проекты, проверяет
исходники и собирает готовые установочные архивы `.dgplugin`.

Builder охватывает весь процесс разработки: создание проекта, сборку,
watch-режим, кэш компиляции, ignore-правила, статистику и загрузку через
Dev Server. Результат собирается в нативный формат DevGram.

## Установка

Из отдельного репозитория:

```bash
python -m pip install --upgrade "git+https://github.com/firedragoq/DevGramBuilder.git"
dgb --version
```

На Windows вместо `python` можно использовать `py -3`.

Для локальной разработки Builder:

```bash
git clone https://github.com/firedragoq/DevGramBuilder.git
cd DevGramBuilder
python -m pip install -e .
```

## Быстрый старт

```bash
mkdir my-plugin
cd my-plugin
dgb new
dgb build -a -v -nf
```

Готовый пакет появится в `builds/<id>-<version>.dgplugin`.

Компилированная сборка требует Python 3.11, совпадающий со встроенным runtime
DevGram:

```bash
dgb build -c 2 -v -nf
```

Остальные команды:

```bash
dgb watch 2 --args "-a -v -nf"
dgb cached
dgb add-ignore "assets/source.psd" --all
dgb del-ignore 0 --all
dgb stats files
dgb stats lines
dgb stats size
dgb stats builds
dgb upload --token "ТОКЕН_DEV_SERVER"
```

Проект хранит настройки в `devgram-builder.json`, исходники в `src/`, ресурсы
в `assets/`, переводы в `locales/`, а локальные wheel-зависимости в `wheels/`.
При сборке Builder создаёт `manifest.json` в корне архива и проверяет пакет по
тем же структурным правилам, которые использует DevGram.

## Структура проекта

```text
my-plugin/
├── devgram-builder.json
├── .devgrambuilder/config.json
├── src/main.py
├── assets/
├── locales/
└── wheels/
```

- `devgram-builder.json` — имя, ID, версия, автор, точка входа и зависимости.
- `src/` — Python-исходники плагина.
- `assets/` — изображения и другие ресурсы.
- `locales/` — JSON-файлы переводов.
- `wheels/` — локальные Python-зависимости в формате `.whl`.

Полный справочник по API плагинов находится в
[DevGram Plugin SDK](https://github.com/firedragoq/DevGram/blob/update-12.10.1/docs/DEVGRAM_PLUGIN_SDK.md).

## Разработка

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
```

Проект распространяется по лицензии [MIT](LICENSE).
