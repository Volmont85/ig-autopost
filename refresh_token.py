"""Продление долгоживущего access-токена Instagram. Печатает новый токен в stdout."""
import os
import sys

import requests

REFRESH_URL = "https://graph.instagram.com/refresh_access_token"


def refresh(token):
    response = requests.get(
        REFRESH_URL,
        params={"grant_type": "ig_refresh_token", "access_token": token},
        timeout=30,
    )
    return response.json()


def main():
    if len(sys.argv) < 2:
        print("Использование: python refresh_token.py <account>", file=sys.stderr)
        sys.exit(1)

    account = sys.argv[1].upper()
    token = os.environ.get(f"IG_{account}_TOKEN")
    if not token:
        print(f"Не задана переменная IG_{account}_TOKEN", file=sys.stderr)
        sys.exit(1)

    data = refresh(token)

    if "error" in data:
        message = data["error"].get("message", str(data["error"]))
        print(f"Токен не продлён (вероятно, ему ещё нет 24 часов): {message}", file=sys.stderr)
        sys.exit(0)

    new_token = data.get("access_token")
    if not new_token:
        print(f"Неожиданный ответ без access_token: {data}", file=sys.stderr)
        sys.exit(1)

    print(new_token)


if __name__ == "__main__":
    main()
