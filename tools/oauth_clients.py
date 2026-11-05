"""Встраивание ключей входа Google/Microsoft в программу: mailclient/oauth_clients.json.

  python tools/oauth_clients.py client_secret_XXXX.json   # файл, скачанный из Google Cloud (Download JSON)
  python tools/oauth_clients.py --from-env                # при сборке: переменные окружения ниже

Переменные: SIMPLEMAIL_GOOGLE_CLIENT_ID, SIMPLEMAIL_GOOGLE_CLIENT_SECRET, SIMPLEMAIL_MICROSOFT_CLIENT_ID.
Файл oauth_clients.json не хранится в git (.gitignore); SimpleMail.spec включает его в сборку.
"""
import json
import os
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from mailclient.oauth import CLIENTS_FILE, google_client_valid, parse_clients  # noqa: E402


def _existing():
    try:
        return parse_clients(json.loads(CLIENTS_FILE.read_text(encoding="utf-8-sig")))
    except (OSError, ValueError):
        return {}


def _write(update):
    clients = _existing()
    clients.update(update)
    google = clients.get("google")
    if google and not google_client_valid(google["client_id"], google["client_secret"]):
        raise SystemExit("Client ID Google должен оканчиваться на .apps.googleusercontent.com, "
                         "и нужен Client secret")
    CLIENTS_FILE.write_text(json.dumps(clients, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Записано: {CLIENTS_FILE} ({', '.join(sorted(update))})")


def main(argv):
    if argv == ["--from-env"]:
        update = {}
        google_id = os.environ.get("SIMPLEMAIL_GOOGLE_CLIENT_ID", "").strip()
        if google_id:
            update["google"] = {"client_id": google_id,
                                "client_secret": os.environ.get("SIMPLEMAIL_GOOGLE_CLIENT_SECRET", "").strip()}
        microsoft_id = os.environ.get("SIMPLEMAIL_MICROSOFT_CLIENT_ID", "").strip()
        if microsoft_id:
            update["microsoft"] = {"client_id": microsoft_id, "client_secret": ""}
        if update:
            _write(update)
        else:
            print("Ключи входа не заданы: сборка без встроенного входа Google/Microsoft")
        return
    if len(argv) != 1:
        raise SystemExit(__doc__)
    try:
        update = parse_clients(json.loads(Path(argv[0]).read_text(encoding="utf-8-sig")))
    except (OSError, ValueError) as error:
        raise SystemExit(f"Не удалось прочитать {argv[0]}: {error}")
    if not update:
        raise SystemExit("В файле нет ключей Google (раздел installed) или google/microsoft")
    _write(update)


if __name__ == "__main__":
    main(sys.argv[1:])
