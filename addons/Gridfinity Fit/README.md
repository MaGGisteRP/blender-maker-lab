# Gridfinity Fit

Дополнение для Blender, которое раскладывает детали Gridfinity по печатным столам и экспортирует 3MF для Bambu Studio или отдельные STL. Генератор Geometry Nodes находится в приложенном Blender-файле.

**Публичная версия 1.0.0. Проверено в Blender 5.2.2 LTS.**

## Установка

1. Скачайте [комплект Gridfinity Fit 1.0.0](https://github.com/MaGGisteRP/blender-maker-lab/releases/tag/gridfinity-fit-v1.0.0) и распакуйте его.
2. Откройте `Gridfinity Fit.blend` из папки `assets/Gridfinity Fit`.
3. В Blender перейдите в **Edit → Preferences → Add-ons → меню → Install from Disk**. Выберите `Gridfinity Fit.py` из папки `addons/Gridfinity Fit` и включите **Gridfinity Fit**.
4. В окне 3D-вида нажмите **N → Gridfinity Fit**.

Можно также установить отдельный архив дополнения **Gridfinity Fit Addon 1.0.0.zip** из выпуска. Blender-файл с генератором всё равно потребуется.

## Параметры

Введите внутреннюю ширину и глубину ящика в **Drawer Width X** и **Drawer Depth Y**. **Clearance** задаёт зазор; **Bed X**, **Bed Y** и **Bed Margin** — размер и отступы печатного поля. Включите **Split For Printer**, укажите **Gap between parts** и нажмите **Count plates**.

**Export 3MF for Bambu Studio** сохраняет проект с несколькими печатными столами. **Printer settings from** позволяет взять настройки из существующего проекта Bambu Studio. **Export STL per part** сохраняет каждую деталь отдельно.

По умолчанию экспорт называется `Gridfinity Fit.3mf`, папка STL — `Gridfinity Fit STL`. Имена STL содержат букву стола и адрес детали, например `Gridfinity Fit Plate A Base A A.stl`. Первый адрес базы обозначает столбец, второй — ряд. Буквы идут A, B, …, Z, AA. Фактические размеры проверяйте в модели и слайсере.

Сначала проверьте масштаб, профиль принтера и материала, затем напечатайте одну плитку и спейсер для примерки. Физическая печать не входит в программную проверку комплекта.

## Совместимость

Подготовленная модель сохранена и проверена в Blender 5.2.2 LTS. Минимум 4.2 в метаданных кода не гарантирует, что эта модель откроется во всех более ранних версиях. Дополнение распознаёт и новое имя группы **Gridfinity Fit**, и прежнее **Gridfinity Drawer**.

3MF предназначен для Bambu Studio; перенос всех его настроек в другие слайсеры не заявляется. Для других слайсеров используйте STL и заново проверьте раскладку.

[Читать статью](../../articles/Gridfinity%20Fit/README.md) · [Модель и примеры](../../assets/Gridfinity%20Fit/README.md)
