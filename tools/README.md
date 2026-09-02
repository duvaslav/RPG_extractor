# tools/

Console backends for WOLF RPG. They are **not** in the repository — put them
here yourself, then build. The build script picks up whatever is present and
warns about what is not.

| Файл | Зачем | Версия | SHA-256 |
| --- | --- | --- | --- |
| `UberWolfCli.exe` | распаковка архивов `.wolf` | v0.6.4 | `0C9645733AE9544DF11EE0C859A7F2CB51AA547D5D13F7935CB480BDAB96FB3A` |
| `WolfTL.exe` | извлечение и импорт текста | не закреплена | — |

Оба — MIT, автор Sinflower:
[UberWolf](https://github.com/Sinflower/UberWolf) ·
[WolfTL](https://github.com/Sinflower/WolfTL). Тексты лицензий лежат в
`licenses/` и попадают в сборку вместе с бинарниками — этого требует MIT.

## Проверка хеша

```powershell
Get-FileHash tools\UberWolfCli.exe -Algorithm SHA256
```

Хеш `UberWolfCli.exe` закреплён в `bundled_tools.py`. Если он не совпадает,
программа **не запустит** файл и скажет об этом. Для другой сборки задайте свой
хеш через переменную окружения:

```powershell
$env:UBERWOLF_CLI_SHA256 = "<ваш sha256>"
```

Для `WolfTL.exe` хеш пока не закреплён: пропишите его в `EXPECTED_SHA256` в
`bundled_tools.py`, когда проверите конкретную сборку.

## Где программа ищет эти файлы

По порядку:

1. `sys._MEIPASS/tools` — внутри one-file EXE;
2. `tools` рядом с EXE — portable / one-dir сборка;
3. `tools` рядом с исходниками — запуск из репозитория;
4. переменные окружения `UBERWOLF_CLI`, `WOLFTL_CLI`;
5. `PATH`.

Проверить, что видит конкретная сборка:

```
RPGMakerExtractor.exe  →  «Проверить игру» покажет строки про UberWolfCli/WolfTL
python rpg_maker_tool.py tools        # тот же отчёт из командной строки
python rpg_maker_tool.py tools --json # машиночитаемо
```
