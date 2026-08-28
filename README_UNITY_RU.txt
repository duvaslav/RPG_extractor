ИНТЕГРАЦИЯ UNITY В RPG MAKER / WOLF GUI
========================================

Состав патча:
  rpg_maker_gui.py     — изменённый GUI
  unity_extractor.py   — модуль извлечения Unity-ассетов
  output_structure.py  — раскладка вывода (плоская / с папками)
  requirements_unity.txt

Установка:
  1. Положите rpg_maker_gui.py, unity_extractor.py и output_structure.py
     рядом с вашим существующим rpg_maker_tool.py.
  2. Установите зависимости:

       python -m pip install -r requirements_unity.txt

  3. Запустите:

       python rpg_maker_gui.py

Использование Unity:
  1. В поле «Движок» выберите Unity или оставьте Auto.
  2. Кнопкой «Папка...» выберите папку игры / папку *_Data.
     Кнопкой «Файл...» можно выбрать APK, XAPK, AAB, OBB,
     *.assets, *.bundle, *.unity3d, *.assetbundle или *.ab.
  3. Отметьте нужные типы: картинки, текст, аудио, шрифты.
  4. Нажмите «Извлечь всё».

Поле «Версия Unity» обычно оставляется пустым. Оно нужно, когда версия
удалена из заголовков ассетов, например: 2021.3.15f1.

Структура папок:
  Переключатель «Сохранять структуру папок» в окне программы решает,
  повторяется ли раскладка игры. Если он выключен, всё складывается
  в одну папку на каждый тип (images/, text/, audio/, fonts/) —
  пути ниже в этом случае схлопываются до одного уровня.

Результат Unity (со включённой структурой):
  images/Texture2D/       — полные текстуры
  images/Sprite/          — вырезанные спрайты
  images/Texture2DArray/  — слои массивов текстур
  text/TextAsset/         — встроенные текстовые/бинарные файлы
  text/MonoBehaviour/     — сериализованные объекты в JSON
  text/LooseFiles/        — внешние JSON/CSV/TXT/XML и т. п.
  audio/                  — AudioClip
  fonts/                  — TTF/OTF
  translation_strings.csv — найденные строки с контекстом
  unity_manifest.json     — статистика и ошибки

Ограничения:
  - Пользовательское шифрование и нестандартное сжатие автоматически
    не обходятся.
  - MonoBehaviour без typetree может не декодироваться. Укажите версию
    Unity и установите TypeTreeGeneratorAPI для расширенной поддержки.
  - Оригинальный rpg_maker_tool.py в загруженных файлах отсутствовал,
    поэтому RPG Maker/WOLF часть проверена только на совместимость интерфейса,
    без запуска её backend-функций.
