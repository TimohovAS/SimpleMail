# Версия приложения

Для каждого готового обновления повышайте номер версии в `mailclient/__init__.py` по SemVer: исправления — patch, новые возможности — minor.
Синхронно обновляйте строковые и числовые версии в `tools/windows-version.txt` перед сборкой EXE.
Заголовок главного окна, окно «О программе» и `QApplication.applicationVersion()` должны использовать `mailclient.__version__`.
